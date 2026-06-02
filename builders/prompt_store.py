import os
import re
import yaml
import logging
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("PromptStore")

PROMPTS_DIR = os.path.join("builders", "prompts")

PROMPT_SLOTS = {
    "planner_system_prompt": "Planner — system instructions for DAG / task planning",
    "synthesis_system_prompt": "Synthesis — system instructions for final response / RCA",
    "tool_guidance_prompt": "Tool guidance — optional hints when invoking tools",
    "user_context_prompt": "User context — preamble injected with each session",
}


class PromptTemplate(BaseModel):
    id: str
    name: str
    slot: str = "planner_system_prompt"
    description: str = ""
    content: str = ""
    tags: List[str] = Field(default_factory=list)
    is_builtin: bool = False


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "prompt"


class PromptStore:
    """Persistent library of reusable prompt templates."""

    def __init__(self, base_dir: str = PROMPTS_DIR):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, prompt_id: str) -> str:
        return os.path.join(self.base_dir, f"{prompt_id}.yaml")

    def list_all(self) -> List[PromptTemplate]:
        templates: List[PromptTemplate] = []
        if not os.path.isdir(self.base_dir):
            return templates
        for filename in sorted(os.listdir(self.base_dir)):
            if not filename.endswith(".yaml"):
                continue
            try:
                templates.append(self.get(filename[:-5]))
            except Exception as e:
                logger.warning(f"Skipping prompt file {filename}: {e}")
        return templates

    def get(self, prompt_id: str) -> PromptTemplate:
        path = self._path(prompt_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Prompt '{prompt_id}' not found")
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        if "id" not in data:
            data["id"] = prompt_id
        return PromptTemplate.model_validate(data)

    def create(
        self,
        name: str,
        slot: str,
        content: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        prompt_id: Optional[str] = None,
    ) -> PromptTemplate:
        if slot not in PROMPT_SLOTS:
            raise ValueError(f"Unknown slot '{slot}'. Valid: {list(PROMPT_SLOTS.keys())}")
        prompt_id = prompt_id or _slugify(name)
        path = self._path(prompt_id)
        if os.path.exists(path):
            raise ValueError(f"Prompt '{prompt_id}' already exists")
        tpl = PromptTemplate(
            id=prompt_id,
            name=name,
            slot=slot,
            description=description,
            content=content,
            tags=tags or [],
        )
        self.save(tpl)
        return tpl

    def save(self, template: PromptTemplate) -> PromptTemplate:
        path = self._path(template.id)
        with open(path, "w") as f:
            yaml.dump(template.model_dump(), f, default_flow_style=False, sort_keys=False)
        return template

    def update(self, prompt_id: str, updates: Dict) -> PromptTemplate:
        current = self.get(prompt_id)
        data = current.model_dump()
        for key, value in updates.items():
            if value is not None:
                data[key] = value
        if data.get("slot") and data["slot"] not in PROMPT_SLOTS:
            raise ValueError(f"Unknown slot '{data['slot']}'")
        updated = PromptTemplate.model_validate(data)
        return self.save(updated)

    def delete(self, prompt_id: str) -> bool:
        path = self._path(prompt_id)
        if not os.path.exists(path):
            return False
        tpl = self.get(prompt_id)
        if tpl.is_builtin:
            raise ValueError("Cannot delete built-in prompt templates")
        os.remove(path)
        return True

    def seed_defaults(self) -> None:
        """Create starter templates if the library is empty."""
        if self.list_all():
            return
        defaults = [
            PromptTemplate(
                id="planner-default",
                name="Expert Dynamic Planner",
                slot="planner_system_prompt",
                description="Plans DAG tasks from user queries and available skills.",
                content=(
                    "You are an expert dynamic AI planner.\n"
                    "Analyze the user's operational query, match it with available skills,\n"
                    "and generate a dynamic Task execution DAG with correct dependency wiring."
                ),
                tags=["planning", "default"],
                is_builtin=True,
            ),
            PromptTemplate(
                id="synthesis-default",
                name="Principal SRE Synthesis",
                slot="synthesis_system_prompt",
                description="Aggregates tool outputs into an RCA-style report.",
                content=(
                    "You are a principal staff reliability engineer.\n"
                    "Aggregate execution metrics, debug logs, and ticket history from the DAG,\n"
                    "and write a detailed Root Cause Analysis (RCA) report."
                ),
                tags=["synthesis", "rca", "default"],
                is_builtin=True,
            ),
        ]
        for tpl in defaults:
            self.save(tpl)
        logger.info("Seeded default prompt templates")
