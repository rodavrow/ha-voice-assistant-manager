# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden files for what other programs depend on: page replies, prompts, generated config.

A snapshot fails when the output changes at all. That is the point: moving code must not
change these, and a change made on purpose is reviewed as a diff of the file. To accept a
change, run the test with VAM_UPDATE_SNAPSHOTS=1 and commit the file.
"""

import json
import os
import re
from pathlib import Path
from typing import Any

SNAPSHOTS = Path(__file__).parent / "snapshots"
# Home Assistant's generated ids (ULIDs and 32-digit hex) differ on every run.
_GENERATED_ID = re.compile(r"\b(?:[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{32})\b")
# A value that is nothing but a short random id (a remembered fact's) or today's date.
_SHORT_ID = re.compile(r"[0-9a-f]{8}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def stable(value: Any, names: dict[str, str] | None = None) -> Any:
    """`value` with ids that change per run replaced: by a name from `names`, else <id>."""
    names = names or {}

    def text(found: str) -> str:
        if _SHORT_ID.fullmatch(found):
            return "<id>"
        if _DATE.fullmatch(found):
            return "<date>"
        for real, name in names.items():
            found = found.replace(real, f"<{name}>")
        return _GENERATED_ID.sub("<id>", found)

    if isinstance(value, str):
        return text(value)
    if isinstance(value, dict):
        return {text(str(key)): stable(item, names) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [stable(item, names) for item in value]
    return value


def assert_matches(name: str, value: Any) -> None:
    """Compare `value` with tests/snapshots/<name>: JSON for data, plain text for a str."""
    path = SNAPSHOTS / name
    if isinstance(value, str):
        current = value if value.endswith("\n") else value + "\n"
    else:
        current = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get("VAM_UPDATE_SNAPSHOTS"):
        path.write_text(current, encoding="utf-8")
        return
    assert path.exists(), f"No snapshot {name}; run once with VAM_UPDATE_SNAPSHOTS=1"
    assert current == path.read_text(encoding="utf-8"), (
        f"{name} changed. If that is intended, update it with VAM_UPDATE_SNAPSHOTS=1"
    )
