import os
import re
import yaml
import logging
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("SkillStore")

SKILLS_DIR = os.path.join("builders", "skills")


class SkillDefinition(BaseModel):
    id: str
    name: str
    description: str
    trigger_conditions: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    planner_hints: str = ""
    dependencies: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)
    parallelizable: bool = True
    is_builtin: bool = False

    def to_registry_skill(self):
        from skills.registry import Skill
        return Skill(
            name=self.name,
            description=self.description,
            trigger_conditions=self.trigger_conditions,
            tools=self.tools,
            planner_hints=self.planner_hints,
            dependencies=self.dependencies,
            examples=self.examples,
            parallelizable=self.parallelizable,
        )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "skill"


class SkillStore:
    """Persistent library of skill definitions."""

    def __init__(self, base_dir: str = SKILLS_DIR):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, skill_id: str) -> str:
        return os.path.join(self.base_dir, f"{skill_id}.yaml")

    def list_all(self) -> List[SkillDefinition]:
        skills: List[SkillDefinition] = []
        if not os.path.isdir(self.base_dir):
            return skills
        for filename in sorted(os.listdir(self.base_dir)):
            if not filename.endswith(".yaml"):
                continue
            try:
                skills.append(self.get(filename[:-5]))
            except Exception as e:
                logger.warning(f"Skipping skill file {filename}: {e}")
        return skills

    def get(self, skill_id: str) -> SkillDefinition:
        path = self._path(skill_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Skill '{skill_id}' not found")
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        if "id" not in data:
            data["id"] = skill_id
        return SkillDefinition.model_validate(data)

    def find_by_name(self, name: str) -> Optional[SkillDefinition]:
        for skill in self.list_all():
            if skill.name == name:
                return skill
        return None

    def create(
        self,
        name: str,
        description: str,
        trigger_conditions: Optional[List[str]] = None,
        tools: Optional[List[str]] = None,
        planner_hints: str = "",
        dependencies: Optional[List[str]] = None,
        examples: Optional[List[str]] = None,
        parallelizable: bool = True,
        skill_id: Optional[str] = None,
    ) -> SkillDefinition:
        skill_id = skill_id or _slugify(name)
        if os.path.exists(self._path(skill_id)):
            raise ValueError(f"Skill '{skill_id}' already exists")
        skill = SkillDefinition(
            id=skill_id,
            name=name,
            description=description,
            trigger_conditions=trigger_conditions or [],
            tools=tools or [],
            planner_hints=planner_hints,
            dependencies=dependencies or [],
            examples=examples or [],
            parallelizable=parallelizable,
        )
        self.save(skill)
        return skill

    def save(self, skill: SkillDefinition) -> SkillDefinition:
        with open(self._path(skill.id), "w") as f:
            yaml.dump(skill.model_dump(), f, default_flow_style=False, sort_keys=False)
        return skill

    def update(self, skill_id: str, updates: Dict) -> SkillDefinition:
        current = self.get(skill_id)
        data = current.model_dump()
        for key, value in updates.items():
            if value is not None:
                data[key] = value
        updated = SkillDefinition.model_validate(data)
        return self.save(updated)

    def delete(self, skill_id: str) -> bool:
        if not os.path.exists(self._path(skill_id)):
            return False
        skill = self.get(skill_id)
        if skill.is_builtin:
            raise ValueError("Cannot delete built-in skills")
        os.remove(self._path(skill_id))
        return True

    def sync_to_registry(self, registry) -> int:
        count = 0
        for skill_def in self.list_all():
            registry.register(skill_def.to_registry_skill())
            count += 1
        return count

    def seed_defaults(self) -> None:
        if self.list_all():
            return
        defaults = [
            SkillDefinition(
                id="incident-triage",
                name="IncidentTriageSkill",
                description="Gathers logs, metrics, and ticket search for production issues.",
                trigger_conditions=[
                    "incident", "inc", "issue", "bug", "crash",
                    "production issue", "outage", "triage", "error",
                ],
                tools=["logs_tool", "metrics_tool", "incident_search_tool"],
                planner_hints="Run logs, metrics, and tickets concurrently.",
                parallelizable=True,
                is_builtin=True,
            ),
            SkillDefinition(
                id="root-cause-analysis",
                name="RootCauseAnalysisSkill",
                description="Synthesizes RCA report after triage completes.",
                trigger_conditions=["rca", "root cause", "analyze", "synthesize", "explain why"],
                tools=["rca_tool"],
                planner_hints="Must run after triage tasks complete.",
                dependencies=["IncidentTriageSkill"],
                parallelizable=False,
                is_builtin=True,
            ),
        ]
        for skill in defaults:
            self.save(skill)
        logger.info("Seeded default skill definitions")
