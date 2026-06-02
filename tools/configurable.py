import asyncio
from typing import Any, Dict
from tools.base import BaseTool
from events.bus import EventBus
from events.types import ObservabilityEvent, TokenStreamEvent


class ConfigurableTool(BaseTool):
    """Generic tool used for simulated or MCP-backed definitions from Tool Builder."""
    def __init__(
        self,
        name: str,
        description: str,
        *,
        handler_type: str = "simulated",
        mcp_server_id: str = "",
        mcp_tool_name: str = "",
    ):
        super().__init__(name=name, description=description)
        self.handler_type = handler_type
        self.mcp_server_id = mcp_server_id
        self.mcp_tool_name = mcp_tool_name

    async def execute(
        self,
        session_id: str,
        trace_id: str,
        input_data: Dict[str, Any],
        event_bus: EventBus,
    ) -> Dict[str, Any]:
        await event_bus.publish(
            ObservabilityEvent(
                trace_id=trace_id,
                session_id=session_id,
                payload={
                    "level": "INFO",
                    "message": f"[{self.name}] executing with input keys: {list(input_data.keys())}",
                    "module": self.name,
                },
            )
        )
        if self.handler_type == "mcp":
            await event_bus.publish(
                ObservabilityEvent(
                    trace_id=trace_id,
                    session_id=session_id,
                    payload={
                        "level": "INFO",
                        "kind": "mcp_call",
                        "message": f"MCP call via {self.mcp_server_id}/{self.mcp_tool_name or self.name}",
                        "module": self.name,
                        "tool_name": self.name,
                        "mcp_server_id": self.mcp_server_id,
                        "mcp_tool_name": self.mcp_tool_name or self.name,
                        "status": "completed",
                    },
                )
            )
        await event_bus.publish(
            TokenStreamEvent(
                trace_id=trace_id,
                session_id=session_id,
                payload={"chunk": f"[{self.name}] completed simulated run.\n", "task_id": self.name},
            )
        )
        await asyncio.sleep(0.2)
        return {
            "status": "success",
            "findings": f"Simulated output from configured tool '{self.name}'.",
            "input_received": input_data,
        }
