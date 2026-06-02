import logging
from typing import Any, Dict, List
from pydantic import BaseModel, Field

logger = logging.getLogger("SkillRegistry")

class Skill(BaseModel):
    name: str
    description: str
    trigger_conditions: List[str] = Field(default_factory=list) # Keywords/Regex to match user query
    tools: List[str] = Field(default_factory=list)              # Tools required by this skill
    planner_hints: str = ""                                     # Instructions to help planner orchestrate
    dependencies: List[str] = Field(default_factory=list)        # Skill names this depends on
    examples: List[str] = Field(default_factory=list)           # Example queries matching this skill
    parallelizable: bool = True                                 # Can these tools be run in parallel?

class SkillRegistry:
    """
    Registry for loading and querying agent skills.
    Used by the planner to map queries to necessary tools and workflow DAGs.
    """
    def __init__(self):
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            logger.warning(f"Overwriting skill '{skill.name}' in registry.")
        self._skills[skill.name] = skill
        logger.info(f"Registered skill: {skill.name}")

    def get_skill(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"Skill '{name}' is not registered.")
        return self._skills[name]

    def list_skills(self) -> List[Skill]:
        return list(self._skills.values())

    def match_skills(self, query: str) -> List[Skill]:
        """
        Scans trigger conditions to see which skills match the user's intent.
        """
        matched = []
        query_lower = query.lower()
        for skill in self._skills.values():
            for condition in skill.trigger_conditions:
                if condition.lower() in query_lower:
                    matched.append(skill)
                    break
        return matched
