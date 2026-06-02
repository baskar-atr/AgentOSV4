from abc import ABC, abstractmethod
from typing import Any, Dict
from events.bus import EventBus

class BaseTool(ABC):
    """
    Abstract Base Class for all system tools.
    Every tool must implement an async execute method.
    """
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    @abstractmethod
    async def execute(
        self, 
        session_id: str, 
        trace_id: str, 
        input_data: Dict[str, Any], 
        event_bus: EventBus
    ) -> Dict[str, Any]:
        """
        Executes the tool asynchronously.
        
        Args:
            session_id: The active session ID.
            trace_id: The trace ID.
            input_data: Parameters passed to the tool.
            event_bus: The EventBus instance to publish streaming token or status updates.
            
        Returns:
            A dictionary containing the structured execution results.
        """
        pass
