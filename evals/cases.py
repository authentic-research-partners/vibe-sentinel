"""What a labelled example looks like, for each of the four judgements.

Cases are Python rather than a TOML dialect of their own. The tool
already has three declared-not-shipped surfaces — ``[[probe]]``,
``[[danger]]``, ``[[secret]]`` — and a fourth config format that only the
evals read would be a second thing to validate, version and get wrong,
for corpora that mostly want a file tree or a whole drift report rather
than a table. The safety corpus does declare its extra dangers as TOML
and hand them to :func:`vibe_sentinel.safety.load_dangers`, because there
the loader is part of what is being measured.

Every case carries ``why``. A label with no argument behind it is the
thing that quietly rots: the model's answer changes, somebody decides
the label was wrong, and nobody can reconstruct which. ``also_ok`` is the
other half of that honesty — where a careful person would accept two
answers, the corpus says so instead of scoring one of them wrong.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from vibe_sentinel.analyze import Lens
from vibe_sentinel.schemas import DriftReport, Snapshot


class Labelled(BaseModel):
    """The half every case shares: an id, an argument, and a label."""

    model_config = ConfigDict(frozen=True)

    id: str
    why: str
    """Why this label is the right one. Read by whoever disagrees with it."""

    verdict: str = ""
    """What the model should answer. Empty means this case is not put to
    the model at all — plenty of the corpus exists to measure the
    mechanical stage and nothing else."""
    also_ok: tuple[str, ...] = ()
    """Other answers a careful person would accept. Counted as correct,
    and still recorded in the matrix as what they were."""

    def accepts(self, observed: str) -> bool:
        return observed == self.verdict or observed in self.also_ok


class HistoryEntry(BaseModel):
    """One earlier tool call by the same actor, as the review will see it.

    A tool call, not a shell line: the journal records ``Write`` and
    ``Edit`` alongside ``Bash``, and half of what makes a command
    dangerous is a file the agent wrote three steps ago.
    """

    model_config = ConfigDict(frozen=True)

    tool: str = "Bash"
    command: str = ""
    target: str = ""


class SafetyCase(Labelled):
    """One tool call, its history, and what should happen to it.

    ``history`` is a list of commands, not of results: the journal records
    a call in front of it running, so the model sees what the agent asked
    for and never what came back. Cases have to be decidable from that —
    which is the real shape of the problem, since the command that made
    this one dangerous is a command.
    """

    tool: str = "Bash"
    command: str = ""
    target: str = ""
    cwd: str = ""
    history: tuple[HistoryEntry, ...] = ()

    escalates: bool = False
    """Whether triage must flag it. The two halves of this field are the
    two failure modes: a missed escalation is the whole failure, and a
    flagged ``ls`` is how the gate gets switched off."""
    dangers: tuple[str, ...] = ()
    """Danger ids triage must name. Triage does not only decide whether
    to ask — it decides what is asked — so naming nothing relevant is a
    failure even when the command was flagged."""


class CredentialCase(Labelled):
    """One file to plant, and what the gate should make of it."""

    path: str
    """Relative to the planted tree, POSIX-separated."""
    content: str

    detected: bool = False
    """Whether the pattern stage must produce a candidate for this file."""
    rules: tuple[str, ...] = ()
    """Rule ids that must be among the candidates. A file flagged by the
    wrong rule is asked the wrong question."""


class PackageFacts(BaseModel):
    """One installed distribution, as the near-miss prompt will see it.

    The fields are exactly what ``build_near_miss_context`` reads, because
    that context is the whole of what the model gets: a summary, who
    installed it, whether anything asked for it, and whether this
    project's source imports it.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str = "1.0.0"
    summary: str = ""
    installer: str = "uv"
    declared: bool = False
    """Named in the project's own manifest."""
    required_by: tuple[str, ...] = ()
    imported: bool = False


class NearMissCase(Labelled):
    """Two installed names one edit apart, and what they actually are."""

    left: PackageFacts
    right: PackageFacts

    paired: bool = True
    """Whether ``near_misses`` must report this pair at all. False is a
    real label: names differing only in digits are deliberately not
    reported, and a corpus that never tested for that would not notice
    the rule going away."""
    suspect: str = ""
    """Which name is the imposter, when one is. Recorded but not scored:
    the audit's own guess never becomes a key, and neither should this."""

    @property
    def pair(self) -> tuple[str, str]:
        return (self.left.name, self.right.name)

    @property
    def facts(self) -> tuple[PackageFacts, PackageFacts]:
        return (self.left, self.right)


class Band(BaseModel):
    """The severities a person would accept for one change.

    A band rather than one value, because severity is a judgement with a
    real spread — ``medium`` and ``high`` on a directory that doubled are
    both defensible, and scoring one of them wrong measures the corpus
    author's taste rather than the model.
    """

    model_config = ConfigDict(frozen=True)

    probe_id: str
    key: str
    accept: tuple[str, ...]
    why: str = ""

    @property
    def pair(self) -> tuple[str, str]:
        return (self.probe_id, self.key)


class DriftCase(Labelled):
    """One whole drift report, one lens, and the bands for its changes.

    A report rather than a change: the lens is asked about all of it at
    once, over one shared prefix, and what a change is worth depends on
    what else moved with it.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    report: DriftReport
    snapshot: Snapshot
    lens: Lens
    guidance: str = ""
    """The project's standing note, in front of the question — the thing
    ``[drift] guidance`` carries. Two cases here differ only by it, which
    is how the corpus measures whether it is read at all."""
    bands: tuple[Band, ...] = ()

    def band_for(self, probe_id: str, key: str) -> Band | None:
        for band in self.bands:
            if band.pair == (probe_id, key):
                return band
        return None


def change_key(probe_id: str, key: str) -> str:
    """The identity of a change, as one string, for reporting.

    ``(probe_id, key)`` is the pair the changes table stores and the pair
    a rating has to land on; the key alone collapses two probes measuring
    one directory. Same identity here, flattened only for display.
    """
    return f"{probe_id}::{key}"


__all__ = [
    "Band",
    "CredentialCase",
    "DriftCase",
    "HistoryEntry",
    "Labelled",
    "NearMissCase",
    "PackageFacts",
    "SafetyCase",
    "change_key",
]
