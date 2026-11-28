"""The skills under skills/: what the wheel ships, and the routing skill's frontmatter."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from agent_bridge import install_cmd

REPO = Path(__file__).resolve().parents[1]


def test_the_wheel_ships_every_skill_the_installer_offers() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    included = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    shipped = {k: v for k, v in included.items() if k.startswith("skills/")}
    assert shipped == {
        f"skills/{name}/SKILL.md": f"agent_bridge/skills/{name}/SKILL.md" for name in install_cmd.SKILL_NAMES
    }
    on_disk = {p.parent.name for p in (REPO / "skills").glob("*/SKILL.md")}
    assert on_disk == set(install_cmd.SKILL_NAMES)


def _frontmatter(text: str) -> tuple[dict[str, str], list[str]]:
    lines = text.splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(": ")
        assert sep and key.isidentifier() and value.strip(), line
        assert ": " not in value and " #" not in value and value[0] not in "`'\"[{&*!|>%@", line
        fields[key] = value
    return fields, lines[end + 1:]


def test_the_routing_skill_frontmatter_names_its_folder_in_one_sentence() -> None:
    path = REPO / "skills" / "bridge-routing" / "SKILL.md"
    fields, body = _frontmatter(path.read_text(encoding="utf-8"))
    assert fields["name"] == path.parent.name
    description = fields["description"]
    assert description.endswith(".")
    assert not re.search(r"[.!?]\s", description), "description is more than one sentence"
    assert len(body) < 60
