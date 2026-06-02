import logging
from typing import Dict, List
from tools.base import BaseTool

logger = logging.getLogger("ToolRegistry")

class ToolRegistry:
    """
    Registry container for keeping track of instantiated tools.
    """
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            logger.warning(f"Overwriting tool '{tool.name}' in registry.")
        self._tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")

    def get_tool(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' is not registered.")
        return self._tools[name]

    def list_tools(self) -> List[BaseTool]:
        return list(self._tools.values())
