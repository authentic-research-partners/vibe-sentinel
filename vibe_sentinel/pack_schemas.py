"""Version-one declarative packs and ecosystem inventory boundary shapes.

These types declare evidence, not policy decisions. Existing Python findings
and pins keep their existing scope; none are implicitly converted into these
cross-ecosystem identities.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

PackId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


def _present_text(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("Identity text must be nonblank and contain no NUL")
    return value


IdentityText = Annotated[
    str, Field(min_length=1, max_length=500), AfterValidator(_present_text)
]
PackageText = Annotated[
    str, Field(min_length=1, max_length=500), AfterValidator(_present_text)
]


class PackModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackIdentity(PackModel):
    id: PackId
    version: Annotated[
        str,
        Field(
            pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$",
            max_length=100,
        ),
    ]
    protocol: Literal[1]
    ecosystems: tuple[PackId, ...] = Field(min_length=1, max_length=32)
    supported_inputs: tuple[IdentityText, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_capabilities(self) -> PackIdentity:
        if len(set(self.ecosystems)) != len(self.ecosystems) or len(
            set(self.supported_inputs)
        ) != len(self.supported_inputs):
            raise ValueError("Pack capabilities must be distinct")
        return self


class ExecutableRequirement(PackModel):
    id: PackId
    command: Annotated[str, Field(min_length=1, max_length=4096)]


class PackManifest(PackIdentity):
    executable: tuple[ExecutableRequirement, ...] = Field(default=(), max_length=64)
    probe: tuple[dict[str, Any], ...] = Field(default=(), max_length=256)
    reader: tuple[dict[str, Any], ...] = Field(default=(), max_length=64)


class InventoryWorkspace(PackModel):
    id: IdentityText
    root: IdentityText


class InventoryEnvironment(PackModel):
    id: IdentityText
    kind: IdentityText
    # Reader-reported runtime identity, never inferred from Sentinel's process.
    runtime: IdentityText


class InventorySelection(PackModel):
    id: IdentityText
    basis: Literal["declared", "locked", "installed"]
    groups: tuple[IdentityText, ...]
    extras: tuple[IdentityText, ...]

    @model_validator(mode="after")
    def distinct_selection(self) -> InventorySelection:
        if len(set(self.groups)) != len(self.groups) or len(set(self.extras)) != len(
            self.extras
        ):
            raise ValueError("Selection entries must be distinct")
        return self


class InventoryGap(PackModel):
    code: Literal[
        "unsupported_input",
        "missing_input",
        "unavailable_environment",
        "dynamic_resolution",
        "reader_error",
        "not_measured",
    ]
    scope: IdentityText


class InventoryCoverage(PackModel):
    status: Literal["complete", "incomplete"]
    # Facets absent here are not assessed, even when the declared scope is complete.
    facets: tuple[Literal["packages", "dependencies", "licenses", "origin"], ...] = (
        Field(min_length=1)
    )
    gaps: tuple[InventoryGap, ...]

    @model_validator(mode="after")
    def honest_coverage(self) -> InventoryCoverage:
        if (self.status == "complete") == bool(self.gaps):
            raise ValueError(
                "Incomplete coverage requires gaps; complete coverage permits none"
            )
        if "packages" not in self.facets or len(set(self.facets)) != len(self.facets):
            raise ValueError("Coverage must include packages and distinct facets")
        return self


class InventoryPackage(PackModel):
    instance: IdentityText
    name: PackageText
    version: PackageText | None
    requirement: PackageText | None
    locator: IdentityText
    dependencies: tuple[IdentityText, ...]
    license_expression: PackageText | None
    origin: Literal["registry", "local", "vcs", "unknown"]


class PackInventory(PackModel):
    protocol: Literal[1]
    pack_id: PackId
    pack_version: IdentityText
    reader_id: PackId
    ecosystem: PackId
    workspace: InventoryWorkspace
    environment: InventoryEnvironment
    selection: InventorySelection
    coverage: InventoryCoverage
    packages: tuple[InventoryPackage, ...] = Field(max_length=20000)

    @model_validator(mode="after")
    def coherent_instances(self) -> PackInventory:
        instances = {package.instance for package in self.packages}
        if len(instances) != len(self.packages):
            raise ValueError("Inventory repeats an installation instance")
        identities: set[tuple[str, str | None, str]] = set()
        for package in self.packages:
            key = (package.name, package.version, package.locator)
            if key in identities:
                raise ValueError("Inventory repeats a package subject identity")
            identities.add(key)
            if self.selection.basis == "declared":
                if package.version is not None or package.requirement is None:
                    raise ValueError(
                        "Declared evidence requires a requirement, not a resolved version"
                    )
            elif package.version is None:
                raise ValueError("Resolved evidence requires a concrete version")
            if len(set(package.dependencies)) != len(package.dependencies) or not set(
                package.dependencies
            ).issubset(instances):
                raise ValueError(
                    "Dependency references must identify distinct known instances"
                )
            if "dependencies" not in self.coverage.facets and package.dependencies:
                raise ValueError(
                    "Dependency evidence requires the dependencies coverage facet"
                )
            if (
                "licenses" not in self.coverage.facets
                and package.license_expression is not None
            ):
                raise ValueError(
                    "Licence evidence requires the licenses coverage facet"
                )
            if "origin" not in self.coverage.facets and package.origin != "unknown":
                raise ValueError("Origin evidence requires the origin coverage facet")
        return self


class InventorySubject(PackModel):
    ecosystem: PackId
    workspace: InventoryWorkspace
    environment: InventoryEnvironment
    selection: InventorySelection
    instance: IdentityText
    name: PackageText
    version: PackageText | None
    requirement: PackageText | None
    locator: IdentityText


class InventoryFindingIdentity(PackModel):
    gate: PackId
    kind: PackId
    subject: InventorySubject


class InventoryPin(PackModel):
    accept: Literal[True]
    verified: date
    gate: PackId
    kind: PackId
    scope: Literal["exact-instance", "ecosystem-package"]
    ecosystem: PackId
    name: PackageText
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    subject: InventorySubject | None

    @field_validator("accept", mode="before")
    @classmethod
    def explicit_acceptance(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("A pin must explicitly accept its finding scope")
        return value

    @model_validator(mode="after")
    def explicit_scope(self) -> InventoryPin:
        if not self.reason.strip():
            raise ValueError("A pin requires a recorded reason")
        if self.scope == "exact-instance":
            if (
                self.subject is None
                or self.subject.ecosystem != self.ecosystem
                or self.subject.name != self.name
            ):
                raise ValueError(
                    "An exact-instance pin requires its matching full subject"
                )
        elif self.subject is not None:
            raise ValueError(
                "An ecosystem-package pin must explicitly omit instance scope"
            )
        return self
