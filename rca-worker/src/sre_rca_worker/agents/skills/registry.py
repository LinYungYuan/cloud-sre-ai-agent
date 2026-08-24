from collections.abc import Iterable, Mapping

from sre_rca_worker.agents.skills.models import SkillSpec
from sre_rca_worker.integrations.mcp.models import SpecialistKind

_SPECIALIST_BY_AGENT = {
    "metrics": SpecialistKind.METRICS,
    "trace": SpecialistKind.TRACE,
    "log": SpecialistKind.LOG,
}


class SkillRegistry:
    def __init__(self, skills: Iterable[SkillSpec]) -> None:
        by_agent: dict[str, SkillSpec] = {}
        names: set[str] = set()
        for skill in skills:
            if skill.name in names or skill.agent in by_agent:
                raise ValueError("duplicate skill name or agent")
            names.add(skill.name)
            by_agent[skill.agent] = skill
        for agent, kind in _SPECIALIST_BY_AGENT.items():
            skill = by_agent.get(agent)
            if skill is None:
                continue
            if any(
                not capability.startswith(f"{kind.value}.")
                for capability in skill.required_capabilities
            ):
                raise ValueError("specialist capabilities must match their endpoint")
        if (root_skill := by_agent.get("rca")) and root_skill.required_capabilities:
            raise ValueError("root RCA skill may not require capabilities")
        self._by_agent = by_agent

    @property
    def agent_names(self) -> frozenset[str]:
        return frozenset(self._by_agent)

    def get_for_agent(self, agent_name: str) -> SkillSpec:
        try:
            return self._by_agent[agent_name]
        except KeyError as error:
            raise LookupError("unknown RCA agent") from error

    def required_capabilities(
        self,
    ) -> Mapping[SpecialistKind, tuple[str, ...]]:
        return {
            kind: self._by_agent[agent].required_capabilities
            for agent, kind in _SPECIALIST_BY_AGENT.items()
            if agent in self._by_agent
        }
