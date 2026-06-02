import os
import re
import yaml
import logging
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

from core.config import LLMConfig

logger = logging.getLogger("LLMStore")

LLMS_DIR = os.path.join("builders", "llms")

LLM_PROVIDERS = {
    "simulated": "Simulated (no external API)",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "azure_openai": "Azure OpenAI",
    "google": "Google Gemini",
    "ollama": "Ollama (local)",
    "custom": "Custom OpenAI-compatible",
}


class LLMModelProfile(BaseModel):
    id: str
    name: str
    description: str = ""
    provider: str = "simulated"
    model_name: str = "gpt-4-turbo"
    api_key_env: str = ""  # env var name, never store raw keys
    base_url: str = ""
    temperature: float = 0.0
    max_tokens: int = 2048
    tags: List[str] = Field(default_factory=list)
    is_builtin: bool = False

    def to_llm_config(self) -> LLMConfig:
        return LLMConfig(
            provider=self.provider,
            model_name=self.model_name,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "llm"


class LLMStore:
    def __init__(self, base_dir: str = LLMS_DIR):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, llm_id: str) -> str:
        return os.path.join(self.base_dir, f"{llm_id}.yaml")

    def list_all(self) -> List[LLMModelProfile]:
        profiles: List[LLMModelProfile] = []
        if not os.path.isdir(self.base_dir):
            return profiles
        for filename in sorted(os.listdir(self.base_dir)):
            if not filename.endswith(".yaml"):
                continue
            try:
                profiles.append(self.get(filename[:-5]))
            except Exception as e:
                logger.warning(f"Skipping LLM file {filename}: {e}")
        return profiles

    def get(self, llm_id: str) -> LLMModelProfile:
        path = self._path(llm_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"LLM model '{llm_id}' not found")
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        if "id" not in data:
            data["id"] = llm_id
        return LLMModelProfile.model_validate(data)

    def create(
        self,
        name: str,
        provider: str,
        model_name: str,
        description: str = "",
        api_key_env: str = "",
        base_url: str = "",
        temperature: float = 0.0,
        max_tokens: int = 2048,
        tags: Optional[List[str]] = None,
        llm_id: Optional[str] = None,
    ) -> LLMModelProfile:
        if provider not in LLM_PROVIDERS:
            raise ValueError(f"Unknown provider '{provider}'. Valid: {list(LLM_PROVIDERS.keys())}")
        llm_id = llm_id or _slugify(name)
        if os.path.exists(self._path(llm_id)):
            raise ValueError(f"LLM model '{llm_id}' already exists")
        profile = LLMModelProfile(
            id=llm_id,
            name=name,
            description=description,
            provider=provider,
            model_name=model_name,
            api_key_env=api_key_env,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            tags=tags or [],
        )
        self.save(profile)
        return profile

    def save(self, profile: LLMModelProfile) -> LLMModelProfile:
        with open(self._path(profile.id), "w") as f:
            yaml.dump(profile.model_dump(), f, default_flow_style=False, sort_keys=False)
        return profile

    def update(self, llm_id: str, updates: Dict) -> LLMModelProfile:
        current = self.get(llm_id)
        data = current.model_dump()
        for key, value in updates.items():
            if value is not None:
                data[key] = value
        if data.get("provider") and data["provider"] not in LLM_PROVIDERS:
            raise ValueError(f"Unknown provider '{data['provider']}'")
        updated = LLMModelProfile.model_validate(data)
        return self.save(updated)

    def delete(self, llm_id: str) -> bool:
        if not os.path.exists(self._path(llm_id)):
            return False
        if self.get(llm_id).is_builtin:
            raise ValueError("Cannot delete built-in LLM profiles")
        os.remove(self._path(llm_id))
        return True

    def seed_defaults(self) -> None:
        if self.list_all():
            return
        defaults = [
            LLMModelProfile(
                id="gpt-4-turbo",
                name="GPT-4 Turbo",
                description="Primary OpenAI-class model (simulated in dev).",
                provider="simulated",
                model_name="gpt-4-turbo",
                temperature=0.0,
                max_tokens=4096,
                tags=["primary", "default"],
                is_builtin=True,
            ),
            LLMModelProfile(
                id="gpt-3-5-turbo",
                name="GPT-3.5 Turbo",
                description="Fast secondary model for lighter tasks.",
                provider="simulated",
                model_name="gpt-3.5-turbo",
                temperature=0.2,
                max_tokens=2048,
                tags=["secondary"],
                is_builtin=True,
            ),
        ]
        for p in defaults:
            self.save(p)
        logger.info("Seeded default LLM model profiles")
