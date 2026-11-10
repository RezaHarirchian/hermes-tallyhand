"""The bot's own Hermes profile, as the README's Recommended setup makes it: the built-in memory, the user profile and
the memory provider are off. Hermes' defaults (memory on) bar every question but the owner's, which
test_memory_gate.py covers; every other test runs with this profile so that viewers are answered."""

from __future__ import annotations

from pathlib import Path

CONFIG = "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n  provider: ''\n"


def write(home: Path) -> Path:
    """Make ``home`` a Hermes home with that config.yaml; returns ``home``."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return home
