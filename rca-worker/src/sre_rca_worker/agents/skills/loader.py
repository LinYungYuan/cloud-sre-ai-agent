from pathlib import Path

import yaml

from sre_rca_worker.agents.skills.models import SkillFrontmatter, SkillSpec


def load_skill(path: Path) -> SkillSpec:
    if path.name != "SKILL.md" or not path.is_file():
        raise ValueError("skill must be a SKILL.md file")
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        raw_frontmatter, body = text[4:].split("\n---\n", 1)
    except ValueError as error:
        raise ValueError("SKILL.md frontmatter is not closed") from error
    parsed = yaml.safe_load(raw_frontmatter)
    frontmatter = SkillFrontmatter.model_validate(parsed)
    if any("tool" in key.lower() for key in parsed):
        raise ValueError("Skill definitions may declare capabilities, not tools")
    if not frontmatter.required_capabilities and frontmatter.agent != "rca":
        raise ValueError("specialist skills require capabilities")
    return SkillSpec(**frontmatter.model_dump(), body=body.strip())


def load_skills(root: Path) -> tuple[SkillSpec, ...]:
    if root.name != "definitions" or not root.is_dir():
        raise ValueError("SKILL.md path must be below the definitions root")
    paths = sorted(root.glob("*/SKILL.md"))
    if any(path.parent.parent != root for path in paths):
        raise ValueError("SKILL.md path escaped the definitions root")
    return tuple(load_skill(path) for path in paths)
