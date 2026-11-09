"""Write a Hermes checkout's pinned runtime requirements and uv overrides, minus the Python 3.14 marker.

Hermes 0.22 and later declare every runtime dependency with ``python_version >= '3.14'``: 3.14 is its
runtime, older interpreters only bridge its updater. The plugin's CI also runs Hermes on 3.11 and 3.13, so
it installs the same exact pins there with only that clause removed. On 3.14 the output equals the
original declaration.

Usage: hermes_requirements.py HERMES_CHECKOUT REQUIREMENTS_OUT OVERRIDES_OUT
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_CLAUSE = r"python_version\s*>=\s*['\"]3\.14['\"]"
_RULES = (
    (re.compile(rf"^\s*{_CLAUSE}\s*$"), ""),
    (re.compile(rf"^\s*{_CLAUSE}\s+and\s+"), ""),
    (re.compile(rf"\s+and\s+{_CLAUSE}\s*$"), ""),
    (re.compile(rf"\s+and\s+{_CLAUSE}(?=\s+and\s)"), ""),
)


def without_314(requirement: str) -> str:
    """``requirement`` with its ``python_version >= '3.14'`` clause removed; other markers stay."""
    if ";" not in requirement:
        return requirement
    spec, marker = (part.strip() for part in requirement.split(";", 1))
    for pattern, repl in _RULES:
        marker = pattern.sub(repl, marker)
    if re.search(_CLAUSE, marker):
        raise SystemExit(f"cannot drop the 3.14 clause from {requirement!r}")
    return f"{spec}; {marker}" if marker else spec


def main(checkout: str, requirements_out: str, overrides_out: str) -> None:
    data = tomllib.loads((Path(checkout) / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [without_314(dep) for dep in data["project"]["dependencies"]]
    overrides = data.get("tool", {}).get("uv", {}).get("override-dependencies", [])
    Path(requirements_out).write_text("\n".join(requirements) + "\n", encoding="utf-8")
    Path(overrides_out).write_text("\n".join(overrides) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
