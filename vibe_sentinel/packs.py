"""Explicit local pack loading and inventory validation, never discovery.

Only configured manifests are read. Loading checks executable availability but
executes nothing, imports no pack code and installs nothing. Probe execution
continues through the existing templates/runner boundary; inventory readers
are declarations for future consumers and do not enter existing state gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vibe_sentinel.pack_schemas import (
    InventoryFindingIdentity,
    InventoryPin,
    InventorySubject,
    PackIdentity,
    PackInventory,
    PackManifest,
    PackModel,
)

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
_LOCAL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class PackError(ValueError):
    """Pack configuration or evidence cannot be used safely as declared."""


class ResolvedExecutable(PackModel):
    id: str
    command: str
    path: str


class LoadedPack(PackModel):
    identity: PackIdentity
    manifest_path: Path
    manifest_sha256: str
    executables: tuple[ResolvedExecutable, ...]
    probes: tuple[dict[str, Any], ...]
    readers: tuple[dict[str, Any], ...]


def _executable(command: str, manifest: Path) -> str:
    if not command or "\x00" in command or "{" in command or "}" in command:
        raise PackError(
            "Pack executable commands must be literal paths or executable names"
        )
    path = Path(command)
    if path.is_absolute():
        selected = path
    elif "/" in command or "\\" in command:
        selected = manifest.parent / path
    else:
        found = shutil.which(command)
        if found is None:
            raise PackError(
                "A required pack executable is unavailable. Install it manually or configure its explicit executable path; no pack was installed or run."
            )
        selected = Path(found)
    # Preserve an interpreter symlink, which can define its environment.
    selected = Path(os.path.abspath(selected))
    if not selected.is_file() or not os.access(selected, os.X_OK):
        raise PackError(
            "A required pack executable path is unavailable or not executable. Install it manually or correct the manifest executable path."
        )
    return str(selected)


def _declarations(
    raw: tuple[dict[str, Any], ...],
    identity: PackIdentity,
    executables: dict[str, str],
    *,
    readers: bool,
) -> tuple[dict[str, Any], ...]:
    declared = []
    seen: set[str] = set()
    for table in raw:
        local_id = table.get("id")
        command = table.get("command")
        if (
            not isinstance(local_id, str)
            or not _LOCAL_ID.fullmatch(local_id)
            or local_id in seen
        ):
            raise PackError(
                "Pack declaration IDs must be distinct lowercase local slugs; correct the manifest."
            )
        seen.add(local_id)
        if (
            not isinstance(command, list)
            or not command
            or not all(
                isinstance(part, str) and part and "\x00" not in part
                for part in command
            )
        ):
            raise PackError(
                "Pack commands must be nonempty fixed argv lists; shell command strings are unsupported."
            )
        if command[0] not in executables:
            raise PackError(
                "Pack command argv[0] must name a declared executable alias; add the corresponding executable requirement."
            )
        if any(
            key in table for key in ("source", "pack", "pack_identity", "provenance")
        ):
            raise PackError(
                "Pack provenance is loader-owned; remove provenance fields from declarations."
            )
        copied = dict(table)
        if readers:
            if table.get("ecosystem") not in identity.ecosystems:
                raise PackError(
                    "Inventory reader ecosystem must be supported by its pack."
                )
            inputs = table.get("supported_inputs")
            if (
                not isinstance(inputs, list)
                or not inputs
                or not all(
                    isinstance(value, str) and value in identity.supported_inputs
                    for value in inputs
                )
                or len(set(inputs)) != len(inputs)
            ):
                raise PackError(
                    "Inventory reader supported_inputs must name distinct declared pack inputs."
                )
        else:
            copied["id"] = f"pack:{identity.id}:{local_id}"
        copied["command"] = [executables[command[0]], *command[1:]]
        copied["source"] = f"pack:{identity.id}@{identity.version}"
        declared.append(copied)
    return tuple(declared)


def load_packs(root: Path, manifests: Sequence[str]) -> tuple[LoadedPack, ...]:
    """Load only configured local manifests, with no executable invocation."""
    if isinstance(manifests, (str, bytes)) or len(manifests) > 64:
        raise PackError("[packs] manifests must be a list of at most 64 local paths")
    loaded = []
    ids: set[str] = set()
    paths: set[Path] = set()
    for configured in manifests:
        if (
            not isinstance(configured, str)
            or not configured
            or "\x00" in configured
            or "://" in configured
        ):
            raise PackError(
                "[packs] manifests accepts explicit local file paths, not URLs or empty entries"
            )
        candidate = Path(configured)
        path = Path(
            os.path.abspath(candidate if candidate.is_absolute() else root / candidate)
        )
        if path.resolve() in paths:
            raise PackError(
                "The same pack manifest is configured more than once; remove the duplicate"
            )
        paths.add(path.resolve())
        try:
            if not path.is_file():
                raise OSError("Manifest is not a regular file")
            with path.open("rb") as handle:
                content = handle.read(MAX_MANIFEST_BYTES + 1)
            if len(content) > MAX_MANIFEST_BYTES:
                raise PackError(
                    "Pack manifest exceeds the 1 MiB limit; split or reduce the declaration"
                )
            raw = tomllib.loads(content.decode("utf-8"))
            if type(raw.get("protocol")) is not int or raw["protocol"] != 1:
                raise PackError(
                    "Unsupported pack protocol. Use a protocol=1 manifest or upgrade Sentinel for this pack."
                )
            manifest = PackManifest.model_validate(raw)
        except PackError:
            raise
        except (OSError, UnicodeError, ValueError):
            # Validation errors may quote executable arguments or other private data.
            raise PackError(
                f"Cannot read a valid pack manifest at {path}. Correct the local path or manifest schema; no pack was run."
            ) from None
        identity = PackIdentity.model_validate(
            manifest.model_dump(include=set(PackIdentity.model_fields))
        )
        if identity.id in ids:
            raise PackError(
                "Configured pack IDs must be unique; remove or rename the conflicting pack"
            )
        ids.add(identity.id)
        if not manifest.probe and not manifest.reader:
            raise PackError(
                "A pack must declare at least one probe or inventory reader"
            )
        executable_ids = [item.id for item in manifest.executable]
        if len(executable_ids) != len(set(executable_ids)):
            raise PackError("Pack executable aliases must be distinct")
        selected_executables = []
        for item in manifest.executable:
            try:
                resolved_path = _executable(item.command, path)
            except PackError as error:
                raise PackError(
                    f"Pack {identity.id}, executable alias {item.id}: {error}"
                ) from None
            selected_executables.append(
                ResolvedExecutable(id=item.id, command=item.command, path=resolved_path)
            )
        executables = tuple(selected_executables)
        resolved = {item.id: item.path for item in executables}
        loaded.append(
            LoadedPack(
                identity=identity,
                manifest_path=path,
                manifest_sha256=hashlib.sha256(content).hexdigest(),
                executables=executables,
                probes=_declarations(manifest.probe, identity, resolved, readers=False),
                readers=_declarations(
                    manifest.reader, identity, resolved, readers=True
                ),
            )
        )
    return tuple(loaded)


def parse_inventory(payload: object, pack: LoadedPack, reader_id: str) -> PackInventory:
    """Validate explicit evidence, never synthesize a complete empty inventory."""
    try:
        reader = next((item for item in pack.readers if item["id"] == reader_id), None)
        if (
            reader is None
            or not isinstance(payload, dict)
            or type(payload.get("protocol")) is not int
            or payload["protocol"] != 1
        ):
            raise ValueError
        if len(json.dumps(payload).encode("utf-8")) > MAX_INVENTORY_BYTES:
            raise ValueError
        inventory = PackInventory.model_validate(payload)
        if (
            inventory.pack_id != pack.identity.id
            or inventory.pack_version != pack.identity.version
            or inventory.reader_id != reader_id
            or inventory.ecosystem != reader["ecosystem"]
        ):
            raise ValueError
        return inventory
    except (TypeError, ValueError, RecursionError, ValidationError):
        raise PackError(
            "Invalid or incompatible pack inventory. Match protocol=1 and the loaded pack/reader identity; supply explicit scope, coverage and unique package instances."
        ) from None


def inventory_subject(inventory: PackInventory, instance: str) -> InventorySubject:
    """Name one instance without applying another ecosystem's normalization."""
    package = next(
        (item for item in inventory.packages if item.instance == instance), None
    )
    if package is None:
        raise PackError("The requested package instance is absent from the inventory")
    return InventorySubject(
        ecosystem=inventory.ecosystem,
        workspace=inventory.workspace,
        environment=inventory.environment,
        selection=inventory.selection,
        instance=package.instance,
        name=package.name,
        version=package.version,
        requirement=package.requirement,
        locator=package.locator,
    )


def subject_key(subject: InventorySubject) -> str:
    """Versioned, collision-resistant subject key; policy retains full scope."""
    payload = subject.model_dump(mode="json")
    # Groups/extras are sets semantically; input order cannot change identity.
    payload["selection"]["groups"] = sorted(payload["selection"]["groups"])
    payload["selection"]["extras"] = sorted(payload["selection"]["extras"])
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"inventory:1:{subject.ecosystem}:{digest}"


def finding_key(finding: InventoryFindingIdentity) -> str:
    """A decision domain and finding kind cannot share another domain's pin."""
    return f"finding:1:{finding.gate}:{finding.kind}:{subject_key(finding.subject)}"


def pin_matches(pin: InventoryPin, finding: InventoryFindingIdentity) -> bool:
    """Match only the explicit finding domain and scope; never legacy pins."""
    if pin.gate != finding.gate or pin.kind != finding.kind:
        return False
    subject = finding.subject
    if pin.ecosystem != subject.ecosystem or pin.name != subject.name:
        return False
    if pin.scope == "ecosystem-package":
        return True
    return pin.subject is not None and subject_key(pin.subject) == subject_key(subject)
