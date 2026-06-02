import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class EventType(str, Enum):
    PLAN_CREATED = "PLAN_CREATED"
    TASK_QUEUED = "TASK_QUEUED"
    TASK_STARTED = "TASK_STARTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOKEN_STREAM = "TOKEN_STREAM"
    STATE_UPDATED = "STATE_UPDATED"
    OBSERVABILITY = "OBSERVABILITY"
    FINAL_RESPONSE = "FINAL_RESPONSE"
    ERROR = "ERROR"

class BaseEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str
    session_id: str
    timestamp: float = Field(default_factory=time.time)
    event_type: EventType
    payload: Dict[str, Any] = Field(default_factory=dict)

class PlanCreatedEvent(BaseEvent):
    event_type: EventType = EventType.PLAN_CREATED
    # Payload details: tasks DAG structure

class TaskQueuedEvent(BaseEvent):
    event_type: EventType = EventType.TASK_QUEUED
    # Payload details: task_id, tool

class TaskStartedEvent(BaseEvent):
    event_type: EventType = EventType.TASK_STARTED
    # Payload details: task_id, tool, started_at

class TaskCompletedEvent(BaseEvent):
    event_type: EventType = EventType.TASK_COMPLETED
    # Payload details: task_id, output_data, completed_at

class TaskFailedEvent(BaseEvent):
    event_type: EventType = EventType.TASK_FAILED
    # Payload details: task_id, error, completed_at

class ToolStartedEvent(BaseEvent):
    event_type: EventType = EventType.TOOL_STARTED
    # Payload details: tool_name, task_id, input_data

class ToolCompletedEvent(BaseEvent):
    event_type: EventType = EventType.TOOL_COMPLETED
    # Payload details: tool_name, task_id, output_data

class TokenStreamEvent(BaseEvent):
    event_type: EventType = EventType.TOKEN_STREAM
    # Payload details: text_chunk, task_id (if applicable), is_final

class StateUpdatedEvent(BaseEvent):
    event_type: EventType = EventType.STATE_UPDATED
    # Payload details: prev_state, current_state

class ObservabilityEvent(BaseEvent):
    event_type: EventType = EventType.OBSERVABILITY
    # Payload details: log_level, message, module

class FinalResponseEvent(BaseEvent):
    event_type: EventType = EventType.FINAL_RESPONSE
    # Payload details: response_text, duration_seconds

class ErrorEvent(BaseEvent):
    event_type: EventType = EventType.ERROR
    # Payload details: error_message, stack_trace
