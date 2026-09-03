"""Probe: which versions are actually installed, and where they came from.

Every other probe here measures source. This one measures the
environment that source runs in, and it exists because that environment
is the one part of a project nothing records.

A declared constraint is in ``pyproject.toml`` and shows up in
``git diff``. A lock file is in the repository too — a diff nobody reads,
but a diff. The *installed* version is in neither. An agent that runs
``uv pip install requests==2.28`` to make a test stop failing leaves no
artifact anywhere in the tree: the manifest still says what it said, the
lock file is untouched if there is one, and the next person to look sees
a repository that has not changed. The version they run is not the
version anyone chose.

So this is a probe and not a gate, by the test in CLAUDE.md: what it
reports is a transition. ``requests`` being at 2.32.5 is not news and
will not be news on the two-hundredth scan. ``requests`` going from
2.32.5 to 2.28.0 between Tuesday and Wednesday is the entire finding, and
it is worth reporting exactly once, when it happens.

Two series per distribution, kept apart for the reason ``modules``
separates fan-out from fan-in — one observation carries one fact, and
folding two into a series silently changes what every recorded point of
it meant:

  ``version:<name>``  the installed version
  ``origin:<name>``   ``index``, or the path or URL it was installed from

The second matters more than it looks. ``origin`` is read from the
wheel's ``direct_url.json``, which is written when a package came from
somewhere that is not an index — a local path, a git URL, a tarball. A
dependency quietly becoming a git checkout is a supply-chain event, it is
invisible in every manifest, and today nothing else here would say so.

Neither carries a ``value``: a version is an identity, not a magnitude,
and 1.10 does not sort after 1.9 in any reading that makes it one. They
carry a ``state`` instead, which is what ``compare`` diffs into a
``changed``. The one number this probe emits is the count of
distributions, which is a magnitude and is worth a trend line — an
environment that gains a package a week is a fact about the project.

Usage::

    python -m vibe_sentinel.probes.dependencies --scope all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vibe_sentinel.probes import emit, not_measured

#: What ``origin`` says for a package that came from an index, which is
#: almost all of them. A constant rather than an empty string because
#: ``compare`` only reports a state that moved between two non-empty
#: values — "" would make the arrival of a direct URL invisible, which is
#: the transition this series exists for.
FROM_INDEX = "index"

#: What ``origin`` says when nothing wrote an install record, so the
#: question has no answer rather than the answer "an index".
UNRECORDED = "unrecorded"

SCOPES = ("all", "declared")


def origin_of(direct_url: str, recorded: bool = True) -> tuple[str, str]:
    """Where one distribution came from: the state, and the detail.

    ``recorded`` is whether an installer wrote an install record at all.
    An empty ``direct_url`` means "from an index" only when something
    could have said otherwise; on an ``.egg-info``, which has nowhere to
    put a direct URL, it means nothing was written down. Reporting the
    second as ``index`` states a fact nobody measured — so it reports
    ``unrecorded``, and the two stay distinguishable in the history.

    ``direct_url.json`` is a JSON blob whose ``url`` identifies the
    source; the rest is resolution detail that moves between installs of
    the same thing. Taking the whole blob as the state would report a
    change every time a commit hash was re-resolved, which is noise on a
    series whose signal is "this stopped coming from an index".

    A local path never becomes the state, only the detail, for two
    reasons that both matter. It is a machine's absolute path — usually
    under someone's home directory — and a state is written to the
    history and printed in the report. And it makes the series
    machine-specific: the same project checked out at a different path
    would report a ``changed`` that describes the checkout rather than
    the dependency. A URL is not machine-specific and stays in the state,
    because *which* fork a package now comes from is the finding.
    """
    if not direct_url:
        return (FROM_INDEX if recorded else UNRECORDED), ""
    import json

    try:
        payload = json.loads(direct_url)
    except ValueError:
        # Unreadable, but present — and present is the finding. Saying
        # `index` here would report a package with a direct URL as having
        # come from PyPI.
        return "direct (unreadable)", direct_url[:200]
    url = str(payload.get("url") or "").strip()
    if not url:
        return "direct (no url)", ""
    vcs = payload.get("vcs_info") or {}
    if isinstance(vcs, dict) and vcs.get("vcs"):
        return f"{vcs['vcs']}+{url}", str(vcs.get("commit_id") or "")
    dir_info = payload.get("dir_info")
    if isinstance(dir_info, dict):
        return ("editable" if dir_info.get("editable") else "local path"), url
    return url, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe_sentinel.probes.dependencies",
        description="Installed versions and where each package came from.",
    )
    parser.add_argument(
        "--root", default=".", help="Project root, for --scope declared"
    )
    parser.add_argument(
        "--scope",
        default="all",
        choices=SCOPES,
        help=(
            "all: every distribution on the path. declared: only what "
            "the manifest names, plus everything those transitively require."
        ),
    )
    args = parser.parse_args(argv)

    # Imported here rather than at module scope: this reads the
    # environment the probe is *run in*, which is the point, and
    # `packages` owns that reading for the provenance gate already.
    # Sharing it means the two cannot disagree about what is installed.
    from vibe_sentinel.packages import (
        declared_requirements,
        installed_distributions,
        requirement_closure,
    )

    root = Path(args.root)
    installed = installed_distributions()
    if not installed:
        # Not zero observations. An interpreter with no distributions on
        # its path is a probe pointed at the wrong python, and recording
        # it as an empty environment would read on the next comparison as
        # every dependency having been removed.
        print(
            "probe dependencies: no distributions found on this interpreter's "
            "path. Run it with the project's own python — the one whose "
            "environment you want measured.",
            file=sys.stderr,
        )
        return 2

    scope = frozenset(installed)
    skipped: list[str] = []
    if args.scope == "declared":
        declared = {r.name for r in declared_requirements(root)}
        if not declared:
            print(
                f"probe dependencies: --scope declared, but nothing in {root} "
                f"declares any dependency. Point --root at the directory "
                f"holding pyproject.toml, or use --scope all.",
                file=sys.stderr,
            )
            return 2
        scope = requirement_closure(set(declared), installed) & frozenset(installed)

    observations: list[dict] = []
    measured = 0
    direct = 0
    for name in sorted(scope):
        dist = installed[name]
        if not dist.version:
            # A distribution whose metadata carries no version is one this
            # probe cannot measure. Emitting `state=""` would make it
            # invisible to `compare` on the way in and on the way out.
            skipped.append(f"{name} (no version in metadata)")
            continue
        origin, detail = origin_of(dist.direct_url, dist.provenance_recorded)
        measured += 1
        direct += origin not in (FROM_INDEX, UNRECORDED)
        observations.append(
            {
                "key": f"version:{name}",
                "state": dist.version,
                "label": f"{name} {dist.version}",
                "attrs": {
                    "installer": dist.installer or "(unrecorded)",
                    "origin": origin,
                },
            }
        )
        origin_attrs = {"version": dist.version}
        if detail:
            origin_attrs["detail"] = detail
        observations.append(
            {
                "key": f"origin:{name}",
                "state": origin,
                "label": f"{name} installed from {origin}",
                "attrs": origin_attrs,
            }
        )

    observations.append(
        {
            # The one magnitude here, so the one thing worth fitting: an
            # environment that gains a package a week is a fact about the
            # project that no single comparison shows.
            "key": "count:installed",
            "value": float(measured),
            "label": f"{measured} distribution(s) measured",
            "attrs": {"scope": args.scope},
        }
    )
    gaps = not_measured(skipped)
    observations.append(gaps)

    emit(
        observations,
        summary=(
            f"{measured} distribution(s) in scope {args.scope!r}, "
            f"{direct} installed from outside an index; {gaps['label']}"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
