from collections.abc import Iterable

from sre_rca_worker.agents.skills.models import SkillSpec


class SkillRegistry:
    def __init__(self, skills: Iterable[SkillSpec]) -> None:
        by_agent: dict[str, SkillSpec] = {}
        names: set[str] = set()
        for skill in skills:
            if skill.name in names or skill.agent in by_agent:
                raise ValueError("duplicate skill name or agent")
            names.add(skill.name)
            by_agent[skill.agent] = skill
        self._by_agent = by_agent

    @property
    def agent_names(self) -> frozenset[str]:
        return frozenset(self._by_agent)

    def get_for_agent(self, agent_name: str) -> SkillSpec:
        try:
            return self._by_agent[agent_name]
        except KeyError as error:
            raise LookupError("unknown RCA agent") from error
