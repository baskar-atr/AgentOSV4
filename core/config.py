import os
import re
import yaml
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("Config")

AGENTS_DIR = "agents"
ACTIVE_AGENT_FILE = os.path.join(AGENTS_DIR, ".active")
DEFAULT_AGENT_ID = "default"
DEFAULT_AGENT_PATH = os.path.join(AGENTS_DIR, "agent.yaml")


class LLMConfig(BaseModel):
    provider: str = "simulated"
    model_name: str = "gpt-4-turbo"
    temperature: float = 0.0
    max_tokens: int = 2048


class AgentConfig(BaseModel):
    id: str = DEFAULT_AGENT_ID
    name: str = "TriageAgent"
    version: str = "1.0.0"
    description: str = "Default incident triage and RCA agent"
    # References to builders/llms/*.yaml profiles
    primary_llm_id: Optional[str] = None
    secondary_llm_id: Optional[str] = None
    primary_llm: LLMConfig = Field(default_factory=LLMConfig)
    secondary_llm: LLMConfig = Field(default_factory=LLMConfig)
    # slot key -> prompt template id (from Prompt Builder)
    prompt_bindings: Dict[str, str] = Field(default_factory=dict)
    # resolved / legacy inline prompts (filled from bindings when loaded)
    prompts: Dict[str, str] = Field(default_factory=lambda: {
        "planner_system_prompt": "You are a senior triage agent...",
        "synthesis_system_prompt": "Aggregate logs, metrics, and incident databases..."
    })
    # skill names from Skill Builder library
    enabled_skills: List[str] = Field(default_factory=lambda: ["IncidentTriageSkill", "RootCauseAnalysisSkill"])
    enabled_tools: List[str] = Field(default_factory=lambda: [
        "logs_tool", "metrics_tool", "incident_search_tool", "rca_tool"
    ])
    # MCP server ids from builders/mcp/*.yaml
    enabled_mcp_servers: List[str] = Field(default_factory=list)
    policies: Dict[str, Any] = Field(default_factory=lambda: {
        "max_concurrency": 4,
        "timeout_seconds": 30
    })


def _agent_path(agent_id: str) -> str:
    if agent_id == DEFAULT_AGENT_ID:
        return DEFAULT_AGENT_PATH
    return os.path.join(AGENTS_DIR, f"{agent_id}.yaml")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


def get_active_agent_id() -> str:
    if os.path.exists(ACTIVE_AGENT_FILE):
        try:
            with open(ACTIVE_AGENT_FILE, "r") as f:
                agent_id = f.read().strip()
                if agent_id:
                    return agent_id
        except OSError as e:
            logger.warning(f"Could not read active agent file: {e}")
    return DEFAULT_AGENT_ID


def set_active_agent(agent_id: str) -> None:
    path = _agent_path(agent_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Agent '{agent_id}' not found at {path}")
    os.makedirs(AGENTS_DIR, exist_ok=True)
    with open(ACTIVE_AGENT_FILE, "w") as f:
        f.write(agent_id)


def list_agents() -> List[Dict[str, Any]]:
    os.makedirs(AGENTS_DIR, exist_ok=True)
    active_id = get_active_agent_id()
    agents: List[Dict[str, Any]] = []

    if os.path.exists(DEFAULT_AGENT_PATH):
        cfg = load_agent_config(DEFAULT_AGENT_PATH)
        agents.append({
            "id": cfg.id,
            "name": cfg.name,
            "description": cfg.description,
            "version": cfg.version,
            "is_active": active_id == cfg.id,
        })

    for filename in sorted(os.listdir(AGENTS_DIR)):
        if not filename.endswith(".yaml") or filename == "agent.yaml":
            continue
        agent_id = filename[:-5]
        path = os.path.join(AGENTS_DIR, filename)
        try:
            cfg = load_agent_config(path)
            agents.append({
                "id": cfg.id,
                "name": cfg.name,
                "description": cfg.description,
                "version": cfg.version,
                "is_active": active_id == cfg.id,
            })
        except Exception as e:
            logger.warning(f"Skipping invalid agent file {filename}: {e}")

    return agents


def apply_llm_bindings(config: AgentConfig) -> AgentConfig:
    """Resolve primary_llm_id / secondary_llm_id into inline LLM configs."""
    if not config.primary_llm_id and not config.secondary_llm_id:
        return config
    try:
        from builders.llm_store import LLMStore
        store = LLMStore()
    except ImportError:
        return config
    resolved = config.model_copy(deep=True)
    if config.primary_llm_id:
        try:
            resolved.primary_llm = store.get(config.primary_llm_id).to_llm_config()
        except FileNotFoundError:
            logger.warning(f"LLM profile '{config.primary_llm_id}' not found")
    if config.secondary_llm_id:
        try:
            resolved.secondary_llm = store.get(config.secondary_llm_id).to_llm_config()
        except FileNotFoundError:
            logger.warning(f"LLM profile '{config.secondary_llm_id}' not found")
    return resolved


def apply_agent_bindings(config: AgentConfig) -> AgentConfig:
    """Apply all builder library bindings (prompts + LLMs)."""
    return apply_llm_bindings(apply_prompt_bindings(config))


def apply_prompt_bindings(config: AgentConfig) -> AgentConfig:
    """Merge prompt template bindings into the agent's prompts dict."""
    if not config.prompt_bindings:
        return config
    try:
        from builders.prompt_store import PromptStore
        store = PromptStore()
    except ImportError:
        return config

    resolved = config.model_copy(deep=True)
    for slot, prompt_id in config.prompt_bindings.items():
        try:
            tpl = store.get(prompt_id)
            resolved.prompts[slot] = tpl.content
        except FileNotFoundError:
            logger.warning(f"Prompt template '{prompt_id}' not found for slot '{slot}'")
    return resolved


def load_agent_config(config_path: Optional[str] = None, resolve_prompts: bool = True) -> AgentConfig:
    """
    Loads agent configuration from a YAML file.
    Uses the active agent when config_path is not specified.
    """
    if config_path is None:
        active_id = get_active_agent_id()
        config_path = _agent_path(active_id)

    if not os.path.exists(config_path):
        logger.warning(f"Config file not found at {config_path}. Creating default config.")
        os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
        default_config = AgentConfig()
        save_agent_config(default_config, config_path)
        return apply_agent_bindings(default_config) if resolve_prompts else default_config

    try:
        with open(config_path, "r") as f:
            data = yaml.safe_load(f) or {}
        if "id" not in data:
            if config_path == DEFAULT_AGENT_PATH:
                data["id"] = DEFAULT_AGENT_ID
            else:
                data["id"] = os.path.splitext(os.path.basename(config_path))[0]
        cfg = AgentConfig.model_validate(data)
        if resolve_prompts:
            cfg = apply_agent_bindings(cfg)
        return cfg
    except Exception as e:
        logger.error(f"Failed to parse config from {config_path}: {e}. Returning defaults.")
        return AgentConfig()


def load_agent_config_raw(config_path: Optional[str] = None) -> AgentConfig:
    """Load agent YAML without resolving prompt bindings (for editing)."""
    return load_agent_config(config_path, resolve_prompts=False)


def configure_agent(
    agent_id: str,
    prompt_bindings: Optional[Dict[str, str]] = None,
    enabled_skills: Optional[List[str]] = None,
    enabled_tools: Optional[List[str]] = None,
    primary_llm_id: Optional[str] = None,
    secondary_llm_id: Optional[str] = None,
    enabled_mcp_servers: Optional[List[str]] = None,
    clear_primary_llm_id: bool = False,
    clear_secondary_llm_id: bool = False,
) -> AgentConfig:
    path = _agent_path(agent_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Agent '{agent_id}' not found")
    cfg = load_agent_config_raw(path)
    if prompt_bindings is not None:
        cfg.prompt_bindings = prompt_bindings
    if enabled_skills is not None:
        cfg.enabled_skills = enabled_skills
    if enabled_tools is not None:
        cfg.enabled_tools = enabled_tools
    if primary_llm_id is not None:
        cfg.primary_llm_id = primary_llm_id
    elif clear_primary_llm_id:
        cfg.primary_llm_id = None
    if secondary_llm_id is not None:
        cfg.secondary_llm_id = secondary_llm_id
    elif clear_secondary_llm_id:
        cfg.secondary_llm_id = None
    if enabled_mcp_servers is not None:
        cfg.enabled_mcp_servers = enabled_mcp_servers
    save_agent_config(cfg, path)
    return apply_agent_bindings(cfg)


def save_agent_config(config: AgentConfig, config_path: Optional[str] = None) -> AgentConfig:
    if config_path is None:
        config_path = _agent_path(config.id)
    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False, sort_keys=False)
    return config


def create_agent(
    name: str,
    description: str = "",
    base_config: Optional[AgentConfig] = None,
    agent_id: Optional[str] = None,
) -> AgentConfig:
    agent_id = agent_id or _slugify(name)
    path = _agent_path(agent_id)
    if os.path.exists(path):
        raise ValueError(f"Agent '{agent_id}' already exists")

    if base_config is None:
        base_config = load_agent_config()

    new_config = base_config.model_copy(deep=True)
    new_config.id = agent_id
    new_config.name = name
    new_config.description = description or f"Custom agent: {name}"
    save_agent_config(new_config, path)
    return new_config


def update_agent_config(updates: Dict[str, Any], agent_id: Optional[str] = None) -> AgentConfig:
    target_id = agent_id or get_active_agent_id()
    path = _agent_path(target_id)
    current = load_agent_config(path)
    merged = current.model_dump()
    for key, value in updates.items():
        if value is not None:
            merged[key] = value
    updated = AgentConfig.model_validate(merged)
    save_agent_config(updated, path)
    return updated
