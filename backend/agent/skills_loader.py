"""Load ``SKILL.md`` files and concatenate them into the investigation prompt.

Skills are concatenated at build time (project_plan.md §6.2) — one folder per
concern. Do not merge skill files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# Investigation skills, in the order they should appear in the system prompt.
INVESTIGATION_SKILLS: tuple[str, ...] = (
    "break-triage",
    "corporate-action-analysis",
    "root-cause-classification",
    "resolution-recommendation",
    "explanation-writing",
    "confidence-calibration",
)

MEMORY_WRITER_SKILL = "memory-writer"


class Skill:
    """Parsed Agent Skill (YAML frontmatter + markdown body)."""

    def __init__(self, name: str, description: str, body: str, path: Path) -> None:
        self.name = name
        self.description = description
        self.body = body
        self.path = path

    def render(self) -> str:
        header = f"## Skill: {self.name}"
        if self.description:
            header += f"\n{self.description}"
        return f"{header}\n\n{self.body.strip()}\n"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse simple YAML frontmatter (key: value) without a YAML dependency."""
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return {}, stripped
    rest = stripped[3:]
    end = rest.find("\n---")
    if end < 0:
        return {}, stripped
    fm_block = rest[:end].strip()
    body = rest[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, body


def load_skill(name: str, *, skills_dir: Path | None = None) -> Skill:
    """Load ``skills/<name>/SKILL.md``."""
    root = skills_dir or SKILLS_DIR
    path = root / name / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(f"Skill file missing: {path}")
    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    return Skill(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        body=body,
        path=path,
    )


def load_skills(
    names: tuple[str, ...] | list[str] = INVESTIGATION_SKILLS,
    *,
    skills_dir: Path | None = None,
) -> list[Skill]:
    return [load_skill(name, skills_dir=skills_dir) for name in names]


def concatenate_skills(skills: list[Skill]) -> str:
    """Join rendered skills for the system prompt."""
    return "\n---\n\n".join(skill.render() for skill in skills)


def investigation_skills_prompt(*, skills_dir: Path | None = None) -> str:
    return concatenate_skills(load_skills(INVESTIGATION_SKILLS, skills_dir=skills_dir))


def memory_writer_skill_prompt(*, skills_dir: Path | None = None) -> str:
    return load_skill(MEMORY_WRITER_SKILL, skills_dir=skills_dir).render()


def skill_inventory(*, skills_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return name/path for tests and diagnostics."""
    root = skills_dir or SKILLS_DIR
    rows: list[dict[str, Any]] = []
    for name in (*INVESTIGATION_SKILLS, MEMORY_WRITER_SKILL):
        path = root / name / "SKILL.md"
        rows.append({"name": name, "path": str(path), "exists": path.is_file()})
    return rows
