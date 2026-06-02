import os
import re
import yaml
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("MCPStore")

MCP_DIR = os.path.join("builders", "mcp")

MCP_TRANSPORTS = {
    "stdio": "Stdio — local process (command + args)",
    "sse": "SSE — remote HTTP server URL",
}


class MCPServerConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    transport: str = "stdio"
    command: str = ""
    args: List[str] = Field(default_factory=list)
    url: str = ""
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    tags: List[str] = Field(default_factory=list)
    is_builtin: bool = False


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "mcp"


class MCPStore:
    def __init__(self, base_dir: str = MCP_DIR):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, mcp_id: str) -> str:
        return os.path.join(self.base_dir, f"{mcp_id}.yaml")

    def list_all(self) -> List[MCPServerConfig]:
        servers: List[MCPServerConfig] = []
        if not os.path.isdir(self.base_dir):
            return servers
        for filename in sorted(os.listdir(self.base_dir)):
            if not filename.endswith(".yaml"):
                continue
            try:
                servers.append(self.get(filename[:-5]))
            except Exception as e:
                logger.warning(f"Skipping MCP file {filename}: {e}")
        return servers

    def get(self, mcp_id: str) -> MCPServerConfig:
        path = self._path(mcp_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"MCP server '{mcp_id}' not found")
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        if "id" not in data:
            data["id"] = mcp_id
        return MCPServerConfig.model_validate(data)

    def create(
        self,
        name: str,
        transport: str = "stdio",
        description: str = "",
        command: str = "",
        args: Optional[List[str]] = None,
        url: str = "",
        env: Optional[Dict[str, str]] = None,
        enabled: bool = True,
        tags: Optional[List[str]] = None,
        mcp_id: Optional[str] = None,
    ) -> MCPServerConfig:
        if transport not in MCP_TRANSPORTS:
            raise ValueError(f"Unknown transport '{transport}'")
        mcp_id = mcp_id or _slugify(name)
        if os.path.exists(self._path(mcp_id)):
            raise ValueError(f"MCP server '{mcp_id}' already exists")
        cfg = MCPServerConfig(
            id=mcp_id,
            name=name,
            description=description,
            transport=transport,
            command=command,
            args=args or [],
            url=url,
            env=env or {},
            enabled=enabled,
            tags=tags or [],
        )
        self.save(cfg)
        return cfg

    def save(self, config: MCPServerConfig) -> MCPServerConfig:
        with open(self._path(config.id), "w") as f:
            yaml.dump(config.model_dump(), f, default_flow_style=False, sort_keys=False)
        return config

    def update(self, mcp_id: str, updates: Dict) -> MCPServerConfig:
        current = self.get(mcp_id)
        data = current.model_dump()
        for key, value in updates.items():
            if value is not None:
                data[key] = value
        updated = MCPServerConfig.model_validate(data)
        return self.save(updated)

    def delete(self, mcp_id: str) -> bool:
        if not os.path.exists(self._path(mcp_id)):
            return False
        if self.get(mcp_id).is_builtin:
            raise ValueError("Cannot delete built-in MCP servers")
        os.remove(self._path(mcp_id))
        return True

    def seed_defaults(self) -> None:
        if self.list_all():
            return
        defaults = [
            MCPServerConfig(
                id="filesystem-mcp",
                name="Filesystem MCP",
                description="Example stdio MCP for local filesystem access.",
                transport="stdio",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                env={},
                tags=["example", "filesystem"],
                is_builtin=True,
            ),
        ]
        for s in defaults:
            self.save(s)
        logger.info("Seeded default MCP server configs")
