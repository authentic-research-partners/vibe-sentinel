"""Where a project's vibe-sentinel files live.

Three constants and nothing else. They have their own module because the
PreToolUse hook has to recognise a project root before it can record
anything, and importing :mod:`vibe_sentinel.config` to learn one filename
would pull in pydantic and the whole configuration model — ~54 ms per
tool call for a string. Same reasoning as :mod:`vibe_sentinel.journal`,
and ``test_hook.py`` asserts the hook's import graph stays clean.

:mod:`vibe_sentinel.config` and :mod:`vibe_sentinel.db.connection` build
on these, so there is still exactly one definition of each name.
"""

from __future__ import annotations

#: Hand-edited probe set and runtime settings, found by walking up from
#: the working directory.
CONFIG_FILENAME = ".vibe-sentinel.toml"

#: Everything vibe-sentinel writes for a project lives under here.
SENTINEL_DIR = ".vibe-sentinel"

#: The history database, inside :data:`SENTINEL_DIR`.
DB_FILENAME = "history.db"
