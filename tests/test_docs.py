"""Docs consistency: plugin.yaml, the code and the Markdown docs must name the same env vars, settings, tools and
commands, and every relative link and in-page anchor must resolve. Fix the docs when this fails, not the test."""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

import tallyhand
from tallyhand import commands, obs_commands

ROOT = Path(tallyhand.__file__).resolve().parent
README = ROOT / "README.md"
CONFIGURATION = ROOT / "docs" / "configuration.md"
TWITCH_TOOLS = ROOT / "docs" / "twitch-tools.md"
OBS_DOC = ROOT / "docs" / "obs.md"
LINKED_FILES = [README, ROOT / "docs" / "configuration.md", TWITCH_TOOLS, OBS_DOC, ROOT / "PRIVACY.md",
                ROOT / "SECURITY.md"]

TRIGGER_NAMES = frozenset({"hermes", "h"})  # trigger prefixes, not chat commands; covered by test_triggers.py


def manifest() -> dict:
    return YAML(typ="safe").load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def code_commands() -> set[str]:
    """Every chat command the code answers: the registered table plus the OBS set (registered at gateway start)."""
    return set(commands.registered()) | set(obs_commands.chat_commands())


def backticked(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


def command_tokens(text: str) -> set[str]:
    """Single ``!name`` tokens in backticked spans, minus triggers and the TALLYHAND_TRIGGERS examples row."""
    found = set()
    for line in text.splitlines():
        if "TALLYHAND_TRIGGERS" in line:
            continue  # the row documents extra trigger prefixes such as !ask,!q, not commands
        for span in backticked(line):
            for token in re.findall(r"!([a-z][a-z0-9_]*)", span):
                if token not in TRIGGER_NAMES:
                    found.add(token)
    return found


def test_env_names_in_configuration():
    """Every requires_env and optional_env name in plugin.yaml appears in docs/configuration.md."""
    manifest_env = [entry["name"] for entry in manifest()["requires_env"] + manifest()["optional_env"]]
    assert manifest_env, "plugin.yaml declares no env vars"
    text = CONFIGURATION.read_text()
    missing = [name for name in manifest_env if name not in text]
    assert not missing, f"env vars missing from docs/configuration.md: {missing}"


def test_config_schema_in_configuration():
    """Every config_schema key in plugin.yaml appears in docs/configuration.md."""
    schema = manifest()["config_schema"]
    assert schema, "plugin.yaml declares no settings"
    text = CONFIGURATION.read_text()
    missing = [key for key in schema if f"`{key}`" not in text]
    assert not missing, f"settings missing from docs/configuration.md: {missing}"


def _readme_table_rows() -> dict[str, str]:
    """README Configuration table: setting name -> Default cell, for rows with a backticked name and default."""
    rows = {}
    for line in README.read_text().splitlines():
        match = re.match(r"\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|", line)
        if match:
            rows[match.group(1)] = match.group(2)
    assert rows, "README Configuration table has no rows"
    return rows


def test_config_defaults_in_readme_table():
    """Every config_schema key with a default has a README Configuration row showing that default."""
    schema = manifest()["config_schema"]
    rows = _readme_table_rows()
    missing = [key for key, spec in schema.items() if "default" in spec and key not in rows]
    assert not missing, f"settings with defaults missing from the README Configuration table: {missing}"
    wrong = {key: rows[key] for key, spec in schema.items()
             if "default" in spec and key in rows and rows[key].strip().lower() != str(spec["default"]).lower()}
    assert not wrong, f"README defaults differ from plugin.yaml: {wrong}"


def test_tools_in_docs():
    """Every provides_tools name in plugin.yaml appears in docs/twitch-tools.md or docs/obs.md."""
    tools = manifest()["provides_tools"]
    assert tools, "plugin.yaml declares no tools"
    text = TWITCH_TOOLS.read_text() + "\n" + OBS_DOC.read_text()
    missing = [name for name in tools if name not in text]
    assert not missing, f"tools missing from docs/twitch-tools.md and docs/obs.md: {missing}"


def test_code_commands_documented():
    """Every chat command the code answers appears as !name in the README or docs."""
    names = code_commands()
    assert len(names) >= 12, f"expected at least the built-in, notes and OBS commands, got {sorted(names)}"
    text = "".join(path.read_text() for path in
                   [README, CONFIGURATION, TWITCH_TOOLS, OBS_DOC, ROOT / "PRIVACY.md", ROOT / "SECURITY.md"])
    missing = [name for name in names if f"!{name}" not in text]
    assert not missing, f"commands missing from the README and docs: {missing}"


def test_readme_commands_exist():
    """Every !command the README lists exists in the code's command table."""
    names = code_commands()
    listed = command_tokens(README.read_text())
    assert listed, "README lists no commands"
    unknown = sorted(name for name in listed if name not in names)
    assert not unknown, f"README lists commands the code does not answer: {unknown}"


def slugify(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation, spaces to hyphens."""
    slug = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", slug)


def headings(text: str) -> set[str]:
    slugs = set()
    for line in text.splitlines():
        if match := re.match(r"#{1,6}\s+(.*)", line):
            slugs.add(slugify(re.sub(r"`([^`]+)`", r"\1", match.group(1))))
    return slugs


def test_relative_links_resolve():
    """Every relative Markdown link in the README, docs, PRIVACY and SECURITY points at a file that exists."""
    failures = []
    for path in LINKED_FILES:
        for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            target = match.group(1).strip()
            if re.match(r"(?:https?://|mailto:|#)", target):
                continue
            file_part, _, _ = target.partition("#")
            resolved = (path.parent / file_part).resolve() if file_part else path
            if not resolved.is_file():
                failures.append(f"{path.name}: {target}")
    assert not failures, f"relative links with no file: {failures}"


def test_anchors_resolve():
    """Every in-page #anchor in those files matches a heading in the linked file."""
    failures = []
    for path in LINKED_FILES:
        for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            target = match.group(1).strip()
            if re.match(r"(?:https?://|mailto:)", target):
                continue
            file_part, sep, anchor = target.partition("#")
            if not sep:
                continue
            linked = (path.parent / file_part).resolve() if file_part else path
            if linked.is_file() and anchor not in headings(linked.read_text()):
                failures.append(f"{path.name}: {target}")
    assert not failures, f"anchors with no heading: {failures}"
