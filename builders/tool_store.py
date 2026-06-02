import os
import re
import yaml
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("ToolStore")

TOOLS_DIR = os.path.join("builders", "tools")

TOOL_HANDLER_TYPES = {
    "builtin": "Built-in Python handler (logs, metrics, RCA, etc.)",
    "simulated": "Simulated tool for testing / placeholders",
    "mcp": "Tool exposed via an MCP server",
}


class ToolDefinition(BaseModel):
    id: str
    name: str  # registry key, e.g. logs_tool
    description: str = ""
    handler_type: str = "simulated"
    builtin_handler: str = ""  # logs_tool | metrics_tool | ...
    mcp_server_id: str = ""
    mcp_tool_name: str = ""
    parameters: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    is_builtin: bool = False


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "tool"


BUILTIN_HANDLERS = {
    "logs_tool": "LogsTool",
    "metrics_tool": "MetricsTool",
    "incident_search_tool": "IncidentSearchTool",
    "rca_tool": "RCAGenerationTool",
}


class ToolConfigStore:
    def __init__(self, base_dir: str = TOOLS_DIR):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, tool_id: str) -> str:
        return os.path.join(self.base_dir, f"{tool_id}.yaml")

    def list_all(self) -> List[ToolDefinition]:
        tools: List[ToolDefinition] = []
        if not os.path.isdir(self.base_dir):
            return tools
        for filename in sorted(os.listdir(self.base_dir)):
            if not filename.endswith(".yaml"):
                continue
            try:
                tools.append(self.get(filename[:-5]))
            except Exception as e:
                logger.warning(f"Skipping tool file {filename}: {e}")
        return tools

    def get(self, tool_id: str) -> ToolDefinition:
        path = self._path(tool_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Tool '{tool_id}' not found")
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        if "id" not in data:
            data["id"] = tool_id
        return ToolDefinition.model_validate(data)

    def create(
        self,
        name: str,
        description: str = "",
        handler_type: str = "simulated",
        builtin_handler: str = "",
        mcp_server_id: str = "",
        mcp_tool_name: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        tool_id: Optional[str] = None,
    ) -> ToolDefinition:
        if handler_type not in TOOL_HANDLER_TYPES:
            raise ValueError(f"Unknown handler_type '{handler_type}'")
        tool_id = tool_id or _slugify(name)
        if os.path.exists(self._path(tool_id)):
            raise ValueError(f"Tool '{tool_id}' already exists")
        defn = ToolDefinition(
            id=tool_id,
            name=name,
            description=description,
            handler_type=handler_type,
            builtin_handler=builtin_handler or (name if handler_type == "builtin" else ""),
            mcp_server_id=mcp_server_id,
            mcp_tool_name=mcp_tool_name,
            parameters=parameters or {},
            tags=tags or [],
        )
        self.save(defn)
        return defn

    def save(self, defn: ToolDefinition) -> ToolDefinition:
        with open(self._path(defn.id), "w") as f:
            yaml.dump(defn.model_dump(), f, default_flow_style=False, sort_keys=False)
        return defn

    def update(self, tool_id: str, updates: Dict) -> ToolDefinition:
        current = self.get(tool_id)
        data = current.model_dump()
        for key, value in updates.items():
            if value is not None:
                data[key] = value
        updated = ToolDefinition.model_validate(data)
        return self.save(updated)

    def delete(self, tool_id: str) -> bool:
        if not os.path.exists(self._path(tool_id)):
            return False
        if self.get(tool_id).is_builtin:
            raise ValueError("Cannot delete built-in tools")
        os.remove(self._path(tool_id))
        return True

    def seed_defaults(self) -> None:
        if self.list_all():
            return
        defaults = [
            ToolDefinition(
                id="logs_tool",
                name="logs_tool",
                description="Queries application and system logs for errors.",
                handler_type="builtin",
                builtin_handler="logs_tool",
                is_builtin=True,
            ),
            ToolDefinition(
                id="metrics_tool",
                name="metrics_tool",
                description="Analyzes performance metrics and saturation.",
                handler_type="builtin",
                builtin_handler="metrics_tool",
                is_builtin=True,
            ),
            ToolDefinition(
                id="incident_search_tool",
                name="incident_search_tool",
                description="Searches incident/ticket databases.",
                handler_type="builtin",
                builtin_handler="incident_search_tool",
                is_builtin=True,
            ),
            ToolDefinition(
                id="rca_tool",
                name="rca_tool",
                description="Synthesizes RCA reports from triage outputs.",
                handler_type="builtin",
                builtin_handler="rca_tool",
                is_builtin=True,
            ),
        ]
        for d in defaults:
            self.save(d)
        logger.info("Seeded default tool definitions")


def sync_tool_definitions_to_registry(tool_registry, tool_config_store: ToolConfigStore) -> int:
    """Register runtime tools from the configuration library."""
    from tools.implementations import LogsTool, MetricsTool, IncidentSearchTool, RCAGenerationTool
    from tools.configurable import ConfigurableTool

    impl_map = {
        "logs_tool": LogsTool,
        "metrics_tool": MetricsTool,
        "incident_search_tool": IncidentSearchTool,
        "rca_tool": RCAGenerationTool,
    }
    count = 0
    for defn in tool_config_store.list_all():
        try:
            if defn.handler_type == "builtin":
                cls = impl_map.get(defn.builtin_handler or defn.name)
                if cls:
                    tool_registry.register(cls())
                    count += 1
            elif defn.handler_type == "simulated":
                tool_registry.register(
                    ConfigurableTool(
                        defn.name,
                        defn.description,
                        handler_type="simulated",
                    )
                )
                count += 1
            elif defn.handler_type == "mcp":
                desc = f"MCP tool via {defn.mcp_server_id}/{defn.mcp_tool_name or defn.name}"
                tool_registry.register(
                    ConfigurableTool(
                        defn.name,
                        desc,
                        handler_type="mcp",
                        mcp_server_id=defn.mcp_server_id,
                        mcp_tool_name=defn.mcp_tool_name or defn.name,
                    )
                )
                count += 1
        except Exception as e:
            logger.warning(f"Failed to register tool {defn.id}: {e}")
    return count
