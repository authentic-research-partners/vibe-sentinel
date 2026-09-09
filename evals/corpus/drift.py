"""Drift reports with known answers, and the severities a person accepts.

The model's whole job here is to rate changes ``compare()`` already
found. So the corpus measures two things that pull apart:

**Does it stay inside the change set.** ``analyze._apply`` drops a rating
naming a change nobody reported, which keeps the drift set reproducible —
and hides how often the model tried. Asked directly, the eval counts it,
because the rate is the argument for the boundary existing.

**Does a severity mean anything.** A model that rates everything ``high``
and one that rates everything ``info`` both score badly on exactly one of
the two report cases below, and a corpus with only alarming changes in it
would not notice either. So the ordinary-editing report is here in full:
small movements, spread evenly, nothing anyone should act on.

Bands rather than single severities, for the reason ``Band`` gives.

Two cases are the same report twice, with and without the project's
standing note. ``[drift] guidance`` claims that a fact about the project
in front of every question changes the answers; nothing measured that
until here.
"""

from __future__ import annotations

from evals.cases import Band, DriftCase
from vibe_sentinel.analyze import BUILTIN_LENSES
from vibe_sentinel.schemas import (
    Change,
    DriftReport,
    HorizonDrift,
    Observation,
    ProbeResult,
    Snapshot,
    TrendFit,
)

ORGANIZATION = BUILTIN_LENSES[0]

#: The house note this repository actually carries, trimmed to the part
#: the paired cases turn on.
PROBE_GROWTH_GUIDANCE = """\
This repository is the tool that produced this report. Its own source is
vibe_sentinel/; tests, docs and scripts sit beside it and are measured as
separate categories. vibe_sentinel/probes/ is meant to grow — one file per
probe is the design.
"""


def _snapshot(summaries: dict[str, str]) -> Snapshot:
    """A snapshot carrying probe summaries and nothing else.

    The context quotes the summaries and the change list; the
    observations behind them are what produced the changes and are not
    re-read, so a corpus that filled them in would be describing work the
    prompt never sees.
    """
    return Snapshot(
        root="/scene",
        model="corpus",
        probes={
            probe_id: ProbeResult(
                probe_id=probe_id,
                summary=summary,
                observations=[Observation(key="(elided)", value=None)],
            )
            for probe_id, summary in summaries.items()
        },
    )


REORGANISATION = DriftCase(
    id="reorganisation",
    why=(
        "Code appearing in a directory that held none, a small directory "
        "tripling, and a module's coupling doubling in one run. Three of the "
        "signals the lens names, at once — if these rate ordinary, the "
        "severity is not carrying information."
    ),
    lens=ORGANIZATION,
    snapshot=_snapshot(
        {
            "module-organization": (
                "42 modules across 9 directories; mean internal coupling 3.1"
            ),
            "commentary-ratio": "0.29 comment lines per code line across the package",
            "file-length": "code 4,880 lines; tests 3,100 lines; docs 1,240 lines",
        }
    ),
    report=DriftReport(
        baseline_at="2026-08-01T09:00:00+00:00",
        current_at="2026-09-08T09:00:00+00:00",
        probes_run=3,
        changes=[
            Change(
                probe_id="module-organization",
                key="dir:vibe_sentinel/handlers",
                kind="appeared",
                after=7,
                label="vibe_sentinel/handlers: 7 module(s), 1,240 lines",
            ),
            Change(
                probe_id="module-organization",
                key="dir:vibe_sentinel/utils",
                kind="grew",
                before=2,
                after=11,
                label="vibe_sentinel/utils: 11 module(s), 2,090 lines",
            ),
            Change(
                probe_id="module-organization",
                key="imports:vibe_sentinel/engine.py",
                kind="grew",
                before=4,
                after=9,
                label="vibe_sentinel/engine.py: 9 internal import(s), 410 lines",
            ),
            Change(
                probe_id="commentary-ratio",
                key="vibe_sentinel",
                kind="shrank",
                before=0.31,
                after=0.29,
                label="vibe_sentinel: 1,223 comment lines / 9,910 code lines across 28 file(s)",
            ),
        ],
        horizons=[
            HorizonDrift(
                horizon="1m",
                run_id=41,
                at="2026-08-08T09:00:00+00:00",
                age_days=31.0,
                changes=[
                    Change(
                        probe_id="module-organization",
                        key="dir:vibe_sentinel/utils",
                        kind="grew",
                        before=1,
                        after=11,
                        label="vibe_sentinel/utils: 11 module(s), 2,090 lines",
                        severity="medium",
                    )
                ],
            )
        ],
        trends=[
            TrendFit(
                probe_id="module-organization",
                key="dir:vibe_sentinel/utils",
                label="vibe_sentinel/utils: 11 module(s), 2,090 lines",
                runs=24,
                first_run=18,
                last_run=42,
                first_value=1,
                last_value=11,
                slope=0.42,
                fitted_change=10.1,
                tau=0.88,
                p_value=0.0001,
                significant=True,
                direction="rising",
            )
        ],
    ),
    bands=(
        Band(
            probe_id="module-organization",
            key="dir:vibe_sentinel/handlers",
            accept=("medium", "high"),
            why="Code in a directory that held none is the lens's first signal.",
        ),
        Band(
            probe_id="module-organization",
            key="dir:vibe_sentinel/utils",
            accept=("medium", "high"),
            why=(
                "Two modules to eleven, and the fitted series says it has been "
                "climbing for twenty-four runs. A grab-bag directory becoming "
                "the biggest one is the thing this tool was built to notice."
            ),
        ),
        Band(
            probe_id="module-organization",
            key="imports:vibe_sentinel/engine.py",
            accept=("low", "medium", "high"),
            why=(
                "Coupling more than doubling in one module is a hub forming, "
                "though four to nine imports is small in absolute terms — the "
                "band is wide because both readings are defensible."
            ),
        ),
        Band(
            probe_id="commentary-ratio",
            key="vibe_sentinel",
            accept=("info", "low"),
            why="Two points of comment ratio is ordinary editing.",
        ),
    ),
)


ORDINARY_EDITING = DriftCase(
    id="ordinary-editing",
    why=(
        "A week of feature work: growth spread evenly, no directory changing "
        "shape, nothing appearing anywhere new. Every change here is real and "
        "none is an event. A model that cannot say so is one whose `high` "
        "means nothing."
    ),
    lens=ORGANIZATION,
    snapshot=_snapshot(
        {
            "module-organization": (
                "44 modules across 9 directories; mean internal coupling 3.2"
            ),
            "commentary-ratio": "0.30 comment lines per code line across the package",
            "file-length": "code 5,090 lines; tests 3,340 lines; docs 1,260 lines",
        }
    ),
    report=DriftReport(
        baseline_at="2026-09-01T09:00:00+00:00",
        current_at="2026-09-08T09:00:00+00:00",
        probes_run=3,
        changes=[
            Change(
                probe_id="file-length",
                key="lines:vibe_sentinel/cli.py",
                kind="grew",
                before=1_820,
                after=1_910,
                label="vibe_sentinel/cli.py [code]: 1,910 lines",
            ),
            Change(
                probe_id="file-length",
                key="lines:tests/test_engine.py",
                kind="grew",
                before=410,
                after=470,
                label="tests/test_engine.py [tests]: 470 lines",
            ),
            Change(
                probe_id="module-organization",
                key="dir:vibe_sentinel",
                kind="grew",
                before=42,
                after=44,
                label="vibe_sentinel: 44 module(s), 16,180 lines",
            ),
            Change(
                probe_id="commentary-ratio",
                key="vibe_sentinel",
                kind="grew",
                before=0.29,
                after=0.30,
                label="vibe_sentinel: 1,223 comment lines / 9,910 code lines across 28 file(s)",
            ),
        ],
    ),
    bands=(
        Band(
            probe_id="file-length",
            key="lines:vibe_sentinel/cli.py",
            accept=("info", "low"),
            why="Five per cent on one module, in step with the tests that came with it.",
        ),
        Band(
            probe_id="file-length",
            key="lines:tests/test_engine.py",
            accept=("info", "low"),
            why="Tests growing alongside code is the shape you want, not drift.",
        ),
        Band(
            probe_id="module-organization",
            key="dir:vibe_sentinel",
            accept=("info", "low"),
            why="Two modules on forty-two, with the directory shape unchanged.",
        ),
        Band(
            probe_id="commentary-ratio",
            key="vibe_sentinel",
            accept=("info", "low"),
            why="One point of ratio, in the direction nobody worries about.",
        ),
    ),
)


def _probe_growth(
    case_id: str, guidance: str, accept: tuple[str, ...], why: str
) -> DriftCase:
    """The same report twice: the pair that measures whether guidance lands."""
    return DriftCase(
        id=case_id,
        why=why,
        lens=ORGANIZATION,
        guidance=guidance,
        snapshot=_snapshot(
            {
                "module-organization": (
                    "44 modules across 10 directories; mean internal coupling 3.0"
                ),
                "file-length": "code 5,090 lines; tests 3,340 lines; docs 1,260 lines",
            }
        ),
        report=DriftReport(
            baseline_at="2026-09-01T09:00:00+00:00",
            current_at="2026-09-08T09:00:00+00:00",
            probes_run=2,
            changes=[
                Change(
                    probe_id="module-organization",
                    key="dir:vibe_sentinel/probes",
                    kind="grew",
                    before=4,
                    after=6,
                    label="vibe_sentinel/probes: 6 module(s), 1,480 lines",
                ),
                Change(
                    probe_id="file-length",
                    key="lines:vibe_sentinel/cli.py",
                    kind="grew",
                    before=1_820,
                    after=1_910,
                    label="vibe_sentinel/cli.py [code]: 1,910 lines",
                ),
            ],
        ),
        bands=(
            Band(
                probe_id="module-organization",
                key="dir:vibe_sentinel/probes",
                accept=accept,
                why=why,
            ),
            Band(
                probe_id="file-length",
                key="lines:vibe_sentinel/cli.py",
                accept=("info", "low"),
                why="Five per cent on one module, alone, is ordinary.",
            ),
        ),
    )


PROBES_GREW_UNGUIDED = _probe_growth(
    "probe-directory-unguided",
    "",
    ("info", "low", "medium"),
    (
        "A directory going from four modules to six, with nothing said about "
        "what that directory is for. Every reading is defensible from the "
        "report alone, which is exactly why the band is wide — the case "
        "exists to be compared against the guided one, not to be scored "
        "hard."
    ),
)

PROBES_GREW_GUIDED = _probe_growth(
    "probe-directory-guided",
    PROBE_GROWTH_GUIDANCE,
    ("info", "low"),
    (
        "The identical report, with the project's standing note saying that "
        "probes/ is meant to grow and that one file per probe is the design. "
        "Two more probe modules is the design working. If this rates the same "
        "as the unguided case, the guidance is not reaching the answer."
    ),
)


DEPENDENCY_MOVED = DriftCase(
    id="dependency-downgraded",
    why=(
        "A dependency that went backwards, with nothing in the project asking "
        "it to — the shape of an agent quieting an error by pinning down. The "
        "lens names it explicitly, and `kind=changed` carries the two ends as "
        "strings rather than numbers, which is the part a renderer or a "
        "prompt gets wrong."
    ),
    lens=ORGANIZATION,
    snapshot=_snapshot(
        {
            "dependency-versions": "63 distributions; 61 from an index, 2 from a path",
            "module-organization": (
                "44 modules across 9 directories; mean internal coupling 3.2"
            ),
        }
    ),
    report=DriftReport(
        baseline_at="2026-09-01T09:00:00+00:00",
        current_at="2026-09-08T09:00:00+00:00",
        probes_run=2,
        changes=[
            Change(
                probe_id="dependency-versions",
                key="version:requests",
                kind="changed",
                before_state="2.32.3",
                after_state="2.28.0",
                label="requests 2.28.0",
            ),
            Change(
                probe_id="dependency-versions",
                key="version:pydantic",
                kind="changed",
                before_state="2.10.4",
                after_state="2.11.1",
                label="pydantic 2.11.1",
            ),
        ],
    ),
    bands=(
        Band(
            probe_id="dependency-versions",
            key="version:requests",
            accept=("medium", "high"),
            why=(
                "A version moving backwards is not ordinary maintenance, and "
                "nothing in the project declared it."
            ),
        ),
        Band(
            probe_id="dependency-versions",
            key="version:pydantic",
            accept=("info", "low"),
            why="A patch-level move forwards is what dependencies do.",
        ),
    ),
)


SILENT_HANDLERS = DriftCase(
    id="silent-handlers-appeared",
    why=(
        "What a shipped question does with a probe it was never told about, "
        "and the answer it should give. The built-in lens names six signals "
        "and none of them is an exception handler, so `silent-exceptions` "
        "reaches it as a number with a label and nothing else — and the "
        "right answer to a question that is not about this is that it is not "
        "about this. `high` here would be the failure: a question naming no "
        "signal for a measurement should not be calling it significant.\n\n"
        "A line for it was written into ORGANIZATION_QUESTION and reverted; "
        "the argument is in analyze.py beside the question. The other half "
        "of the pair is a `[[lens]]` declared by a project that does ban "
        "silent swallows: asked in its own words, the same report comes "
        "back `medium` or `high`. Two questions, two correct answers, one "
        "measurement — which is the whole argument for declaring a lens "
        "rather than editing a shipped prompt."
    ),
    lens=ORGANIZATION,
    snapshot=_snapshot(
        {
            "silent-exceptions": ("36 of 152 handler(s) tree-wide discard the error"),
            "module-organization": (
                "44 modules across 9 directories; mean internal coupling 3.2"
            ),
        }
    ),
    report=DriftReport(
        baseline_at="2026-09-01T09:00:00+00:00",
        current_at="2026-09-08T09:00:00+00:00",
        probes_run=2,
        changes=[
            Change(
                probe_id="silent-exceptions",
                key="silent-in:vibe_sentinel",
                kind="grew",
                before=14,
                after=31,
                label="vibe_sentinel: 31 of 118 handler(s) discard the error",
                attrs={"handlers": "118", "share": "0.263", "blanket": "6"},
            ),
            Change(
                probe_id="silent-exceptions",
                key="silent-total",
                kind="grew",
                before=19,
                after=36,
                label="36 of 152 handler(s) tree-wide discard the error",
                attrs={"handlers": "152", "share": "0.237"},
            ),
        ],
    ),
    bands=(
        Band(
            probe_id="silent-exceptions",
            key="silent-in:vibe_sentinel",
            accept=("info", "low", "medium"),
            why=(
                "Measured, not assumed: without a signal line the model "
                'answers `low` or `medium`, and its stated reason — "a code '
                'behavior metric, not structural" — is a fair reading of a '
                "question that never mentions error handling. `high` is what "
                "would be wrong here, and it is what appeared the moment a "
                "line for this probe was added."
            ),
        ),
        Band(
            probe_id="silent-exceptions",
            key="silent-total",
            accept=("info", "low", "medium"),
            why="The same fact counted a second way; the same reasoning.",
        ),
    ),
)


CASES: tuple[DriftCase, ...] = (
    REORGANISATION,
    ORDINARY_EDITING,
    PROBES_GREW_UNGUIDED,
    PROBES_GREW_GUIDED,
    DEPENDENCY_MOVED,
    SILENT_HANDLERS,
)
