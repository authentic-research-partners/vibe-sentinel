"""Eval cases a project declares beside its own lenses.

A shipped corpus can only measure shipped rules. The moment somebody adds
a `[[probe]]` of their own and a `[[lens]]` to read it, the question that
matters is whether *their* local model answers *their* question about
*their* measurement — and nothing in `evals/corpus/` knows anything about
it.

So a case is declared the same way everything else here is declared, and
in the same file, nested under the lens it tests:

    [[lens]]
    id = "thin-probes-directory"
    watch = ["module-organization"]
    question = \"\"\"One file per probe is the design. Is probes/ growing
    past that — a module in there that is not a probe?\"\"\"

    [[lens.case]]
    id = "one-more-probe"
    why = "A sixth probe is the design working, not drift."
    [[lens.case.change]]
    probe = "module-organization"
    key = "vibe_sentinel/probes"
    kind = "grew"
    before = 5
    after = 6
    label = "modules in vibe_sentinel/probes"
    accept = ["info", "low"]

The question and the cases that keep it honest sit next to each other,
which is the point: a prompt nobody can test is a prompt that drifts, and
one kept in a second file is one that gets edited alone.

``vibe_sentinel``'s own loader reads `[[lens]]` and ignores `case`
entirely, so declaring cases costs a scan nothing and cannot break one.

**Everything here fails loudly.** A case for a lens that was disabled, a
change naming a probe nobody declared, a case whose changes do not trip
its own lens's ``watch`` — each is a case that can never be measured, and
each would otherwise show up as a corpus with nothing wrong in it. Same
rule ``unknown_watches`` keeps for the scan.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from evals.cases import Band, DriftCase
from vibe_sentinel.analyze import Lens, load_lenses, watched
from vibe_sentinel.paths import CONFIG_FILENAME
from vibe_sentinel.rulesets import rule_file_paths
from vibe_sentinel.schemas import (
    Change,
    ChangeKind,
    DriftReport,
    Observation,
    ProbeResult,
    Severity,
    Snapshot,
    TrendFit,
)
from vibe_sentinel.templates import load_probes

_KINDS = frozenset(ChangeKind.__args__)  # type: ignore[attr-defined]
_SEVERITIES = frozenset(Severity.__args__)  # type: ignore[attr-defined]
_DIRECTIONS = frozenset({"rising", "falling", "flat"})


def _table(raw: object, where: str, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: {what} is not a table.")
    return raw


def _change(
    raw: dict[str, Any], where: str, probe_ids: set[str]
) -> tuple[Change, Band | None]:
    """One ``[[lens.case.change]]``, as the pair the eval needs.

    The change is what the model is shown; the band is what the answer is
    scored against. They are declared together because they are one
    thought — this moved, and this is what a person would call it.
    """
    probe = str(raw.get("probe", "")).strip()
    key = str(raw.get("key", "")).strip()
    if not probe or not key:
        raise ValueError(
            f"{where} needs probe and key. Together they name the change the "
            f"model has to rate, which is the same pair the changes table "
            f"stores — a key alone is ambiguous the moment two probes measure "
            f"the same directory."
        )
    if probe not in probe_ids:
        raise ValueError(
            f"{where} names probe {probe!r}, which this project does not "
            f"declare. A change from a probe nobody runs cannot appear in a "
            f"real report, so the case measures nothing. Declared probes: "
            f"{', '.join(sorted(probe_ids))}."
        )
    kind = str(raw.get("kind", "")).strip()
    if kind not in _KINDS:
        raise ValueError(
            f"{where} has kind={kind!r}. Use one of: {', '.join(sorted(_KINDS))}."
        )
    accept = raw.get("accept", [])
    if not isinstance(accept, list):
        raise ValueError(
            f"{where} has accept={accept!r}. It is the severities a person "
            f'would accept for this change — accept = ["info", "low"] — '
            f"and several of them, because severity is a judgement with a "
            f"real spread and scoring one of two defensible answers wrong "
            f"measures the case author's taste. Leave it off entirely for "
            f"a change that is context: in the report, not scored."
        )
    bad = [str(s) for s in accept if str(s) not in _SEVERITIES]
    if bad:
        raise ValueError(
            f"{where} accepts {', '.join(bad)}, which are not severities. "
            f"Use: {', '.join(sorted(_SEVERITIES))}."
        )
    # No band: in the report and deliberately not scored. A lens is asked
    # one narrow question, so most of what it is shown is context — and a
    # case that banded every change would assert the lens must rate what
    # its own question excludes, which is the opposite of what this
    # codebase asks a lens to do. The shipped corpus has always said this
    # by banding a subset of its changes; this is the same thing in TOML.
    banded = bool(accept)
    states = kind == "changed"
    before_state = str(raw.get("before_state", ""))
    after_state = str(raw.get("after_state", ""))
    if states and not (before_state and after_state):
        raise ValueError(
            f"{where} is kind=changed, so it needs before_state and "
            f"after_state — the two ends are strings there (a version, an "
            f"origin, a licence), and the numeric pair says nothing."
        )
    if not states and raw.get("after") is None:
        raise ValueError(
            f"{where} is kind={kind}, so it needs `after` — the value as this "
            f"run measured it. Add `before` too for anything but appeared."
        )
    attrs = raw.get("attrs", {})
    if not isinstance(attrs, dict):
        raise ValueError(
            f"{where} has attrs={attrs!r}. It is a table of the probe's own "
            f'short facts about this key — attrs = {{ share = "0.235" }} — '
            f"carried onto the change so a question can be asked about them."
        )
    change = Change(
        probe_id=probe,
        key=key,
        attrs={str(k): str(v) for k, v in attrs.items()},
        kind=kind,  # type: ignore[arg-type]
        before=None if raw.get("before") is None else float(raw["before"]),
        after=None if raw.get("after") is None else float(raw["after"]),
        before_state=before_state,
        after_state=after_state,
        label=str(raw.get("label", "")),
    )
    band = (
        Band(
            probe_id=probe,
            key=key,
            accept=tuple(str(s) for s in accept),
            why=str(raw.get("why", "")),
        )
        if banded
        else None
    )
    return change, band


def _trend(raw: dict[str, Any], where: str, probe_ids: set[str]) -> TrendFit:
    """One ``[[lens.case.trend]]`` — the whole series, as the prompt shows it.

    Every number here is one the case author wrote down. ``slope`` is the
    single exception and it is arithmetic on two of them, not an estimate:
    a fitted statistic invented by this loader would put a number in front
    of the model that no fit ever produced.
    """
    probe = str(raw.get("probe", "")).strip()
    key = str(raw.get("key", "")).strip()
    if probe not in probe_ids or not key:
        raise ValueError(
            f"{where} needs probe and key, and the probe must be one this "
            f"project declares."
        )
    direction = str(raw.get("direction", "flat"))
    if direction not in _DIRECTIONS:
        raise ValueError(
            f"{where} has direction={direction!r}. Use rising, falling or flat."
        )
    runs = int(raw.get("runs", 0))
    if runs < 2:
        raise ValueError(
            f"{where} has runs={runs}. A fit needs a series; this project's "
            f"own threshold for reporting one is 20 runs, and under 2 there "
            f"is nothing to fit at all."
        )
    first = float(raw.get("first_value", 0.0))
    last = float(raw.get("last_value", 0.0))
    return TrendFit(
        probe_id=probe,
        key=key,
        label=str(raw.get("label", "")),
        runs=runs,
        first_value=first,
        last_value=last,
        slope=float(raw.get("slope", (last - first) / (runs - 1))),
        fitted_change=float(raw.get("fitted_change", last - first)),
        tau=float(raw.get("tau", 0.0)),
        p_value=float(raw.get("p_value", 1.0)),
        significant=bool(raw.get("significant", False)),
        direction=direction,  # type: ignore[arg-type]
    )


def _snapshot(summaries: dict[str, Any], probes: set[str]) -> Snapshot:
    """The probe summaries the prompt quotes under `<CURRENT_STRUCTURE>`.

    A case that declares none gets one line per probe it mentions, saying
    so. Silence would be a different prompt from the one a scan builds,
    and the difference would be invisible.
    """
    lines = {
        probe: str(summaries.get(probe, "(this case declared no summary)"))
        for probe in sorted(probes)
    }
    return Snapshot(
        root="(declared case)",
        model="declared",
        probes={
            probe: ProbeResult(
                probe_id=probe,
                summary=summary,
                observations=[Observation(key="(elided)", value=None)],
            )
            for probe, summary in lines.items()
        },
    )


def _case(
    raw: dict[str, Any],
    lens: Lens,
    where: str,
    probe_ids: set[str],
    default_guidance: str,
) -> DriftCase:
    case_id = str(raw.get("id", "")).strip()
    if not case_id:
        raise ValueError(f"{where} has no id. It is what a failing case is called.")
    why = str(raw.get("why", "")).strip()
    if not why:
        raise ValueError(
            f"{where} ({case_id}) has no why. It is the argument for the "
            f"labels below it, for whoever disagrees with them later — the "
            f"same thing `reason` is on a pin. A label with nothing behind it "
            f"is the one that quietly rots."
        )
    raw_changes = raw.get("change", [])
    if not isinstance(raw_changes, list) or not raw_changes:
        raise ValueError(
            f"{where} ({case_id}) declares no [[lens.case.change]]. A case is "
            f"a report put to the lens, and a report with nothing in it asks "
            f"the model to rate nothing."
        )
    pairs = [
        _change(
            _table(entry, where, "change"),
            f"{where} ({case_id}) change #{i}",
            probe_ids,
        )
        for i, entry in enumerate(raw_changes, start=1)
    ]
    trends = [
        _trend(
            _table(entry, where, "trend"), f"{where} ({case_id}) trend #{i}", probe_ids
        )
        for i, entry in enumerate(raw.get("trend", []), start=1)
    ]
    if not any(band is not None for _, band in pairs):
        raise ValueError(
            f"{where} ({case_id}) bands none of its changes, so nothing about "
            f"it can be right or wrong. Give at least one an `accept`; the "
            f"rest are context, which is what leaving `accept` off means."
        )
    report = DriftReport(
        baseline_at="(the run this case compares against)",
        current_at="(this case)",
        probes_run=len({c.probe_id for c, _ in pairs}),
        changes=[change for change, _ in pairs],
        trends=trends,
    )
    if not watched(lens, report):
        raise ValueError(
            f"{where} ({case_id}) declares nothing lens {lens.id!r} watches, "
            f"so a real scan would never ask about it — and a lens that is "
            f"never asked reads exactly like one that found nothing. Its "
            f"watch is {', '.join(lens.watch) or '(every probe)'}; this case "
            f"moves {', '.join(sorted({c.probe_id for c, _ in pairs}))}."
        )
    summaries = _table(raw.get("summaries", {}), where, "summaries")
    return DriftCase(
        id=f"{lens.id}/{case_id}",
        why=why,
        lens=lens,
        guidance=(str(raw["guidance"]) if "guidance" in raw else default_guidance),
        report=report,
        snapshot=_snapshot(
            summaries, {c.probe_id for c, _ in pairs} | {t.probe_id for t in trends}
        ),
        bands=tuple(band for _, band in pairs if band is not None),
    )


def load_cases(root: Path | None, default_guidance: str = "") -> tuple[DriftCase, ...]:
    """Every ``[[lens.case]]`` this project declares, validated.

    ``default_guidance`` is the project's ``[drift] guidance``, because
    that is what a real scan puts in front of the question. A case
    declaring its own — including an empty one — overrides it, which is
    how you measure whether the standing note is doing any work.
    """
    if root is None:
        return ()
    path = root / CONFIG_FILENAME
    if not path.is_file():
        return ()
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    active = {lens.id: lens for lens in load_lenses(root)}
    probe_ids = {probe.id for probe in load_probes(project_root=root)}
    cases: list[DriftCase] = []
    seen: set[str] = set()

    for source, tables in _lens_tables(root, path, data):
        for index, table in enumerate(tables, start=1):
            if not isinstance(table, dict):
                continue
            raw_cases = table.get("case", [])
            if not raw_cases:
                continue
            lens_id = str(table.get("id", "")).strip()
            where = f"{source}: [[lens]] #{index} ({lens_id})"
            lens = active.get(lens_id)
            if lens is None:
                raise ValueError(
                    f"{where} declares cases for a lens that is not in the "
                    f"active set — [drift] use or [drift] disable removed it. "
                    f"A case for a lens nobody asks is never measured. "
                    f"Re-enable the lens or remove its cases."
                )
            for entry in raw_cases:
                case = _case(
                    _table(entry, where, "case"),
                    lens,
                    where,
                    probe_ids,
                    default_guidance,
                )
                if case.id in seen:
                    raise ValueError(
                        f"{where} declares two cases called {case.id!r}. Ids "
                        f"name a failing case in the report, so they have to "
                        f"be unique — across every file that declares one."
                    )
                seen.add(case.id)
                cases.append(case)
    return tuple(cases)


def _lens_tables(
    root: Path, path: Path, data: dict[str, Any]
) -> list[tuple[Path, list[Any]]]:
    """Every file that declares a ``[[lens]]``, and its tables.

    The same files :func:`~vibe_sentinel.analyze.load_lenses` reads —
    ``[drift] rule_files`` first, the project config last — because a case
    is only worth writing where the lens it tests is declared, and a lens
    moved into ``rules/`` took its cases with it.

    Cases were read from ``.vibe-sentinel.toml`` alone until rule files
    existed, which made a case in one of them silently unmeasured. That is
    the exact shape this corpus is built to refuse: a label nobody scores
    reports as no failures rather than as no data.
    """
    settings = data.get("drift")
    patterns = settings.get("rule_files", []) if isinstance(settings, dict) else []

    sources: list[tuple[Path, list[Any]]] = []
    for rule_path in rule_file_paths(root, patterns, setting="[drift]", where=path):
        rule_data = tomllib.loads(rule_path.read_text(encoding="utf-8"))
        tables = rule_data.get("lens", [])
        if isinstance(tables, list) and tables:
            sources.append((rule_path, tables))

    tables = data.get("lens", [])
    if isinstance(tables, list) and tables:
        sources.append((path, tables))
    return sources
