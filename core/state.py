import time
import asyncio
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from events.types import BaseEvent, EventType, StateUpdatedEvent

class TaskState(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class TaskNode(BaseModel):
    id: str
    tool: str
    depends_on: List[str] = Field(default_factory=list)
    status: TaskState = TaskState.PENDING
    input_data: Dict[str, Any] = Field(default_factory=dict)
    output_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

class DAGState(BaseModel):
    tasks: Dict[str, TaskNode] = Field(default_factory=dict)

    def get_runnable_tasks(self) -> List[TaskNode]:
        """
        Returns a list of tasks that have all dependencies COMPLETED and are currently PENDING.
        """
        runnable = []
        for task_id, task in self.tasks.items():
            if task.status != TaskState.PENDING:
                continue
            
            # Check dependencies
            deps_met = True
            for dep_id in task.depends_on:
                dep_node = self.tasks.get(dep_id)
                if not dep_node or dep_node.status != TaskState.COMPLETED:
                    deps_met = False
                    break
            
            if deps_met:
                runnable.append(task)
        return runnable

    def is_complete(self) -> bool:
        """
        Returns True if all tasks in the DAG are COMPLETED.
        """
        return all(task.status == TaskState.COMPLETED for task in self.tasks.values())

    def is_failed(self) -> bool:
        """
        Returns True if any task in the DAG has FAILED.
        """
        return any(task.status == TaskState.FAILED for task in self.tasks.values())

class SessionState(BaseModel):
    session_id: str
    trace_id: str
    query: str
    dag: DAGState = Field(default_factory=DAGState)
    variables: Dict[str, Any] = Field(default_factory=dict)
    is_completed: bool = False
    final_response: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

class StateStore:
    """
    In-memory state store containing states of active user query sessions.
    Ensures safe concurrency using locks per session.
    """
    def __init__(self, event_bus: Any = None):
        self._states: Dict[str, SessionState] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._event_bus = event_bus

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        async with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = asyncio.Lock()
            return self._locks[session_id]

    async def create_session(self, session_id: str, trace_id: str, query: str) -> SessionState:
        lock = await self._get_lock(session_id)
        async with lock:
            state = SessionState(
                session_id=session_id,
                trace_id=trace_id,
                query=query
            )
            self._states[session_id] = state
            return state.model_copy(deep=True)

    async def get_session(self, session_id: str) -> Optional[SessionState]:
        lock = await self._get_lock(session_id)
        async with lock:
            state = self._states.get(session_id)
            if state:
                return state.model_copy(deep=True)
            return None

    async def update_session(self, session_id: str, state: SessionState) -> None:
        lock = await self._get_lock(session_id)
        async with lock:
            state.updated_at = time.time()
            self._states[session_id] = state

    async def update_task_status(
        self, session_id: str, task_id: str, status: TaskState, error: Optional[str] = None, output_data: Optional[Dict[str, Any]] = None
    ) -> SessionState:
        """
        Updates task state, timestamps, and output values inside a session.
        Emits a STATE_UPDATED event automatically if event_bus is configured.
        """
        lock = await self._get_lock(session_id)
        async with lock:
            session = self._states.get(session_id)
            if not session:
                raise ValueError(f"Session {session_id} not found.")

            task = session.dag.tasks.get(task_id)
            if not task:
                raise ValueError(f"Task {task_id} not found in session {session_id}.")

            prev_status = task.status
            task.status = status
            
            if status == TaskState.RUNNING:
                task.started_at = time.time()
            elif status in (TaskState.COMPLETED, TaskState.FAILED):
                task.completed_at = time.time()
                if error:
                    task.error = error
                if output_data is not None:
                    task.output_data = output_data
                    # Merge outputs into session variables
                    session.variables[task_id] = output_data

            session.updated_at = time.time()
            copied_session = session.model_copy(deep=True)

        # Emit state update outside the lock
        if self._event_bus and prev_status != status:
            event = StateUpdatedEvent(
                trace_id=session.trace_id,
                session_id=session_id,
                payload={
                    "task_id": task_id,
                    "prev_status": prev_status.value,
                    "new_status": status.value,
                    "dag": copied_session.dag.model_dump()
                }
            )
            await self._event_bus.publish(event)

        return copied_session

    async def finalize_session(self, session_id: str, final_response: str) -> SessionState:
        lock = await self._get_lock(session_id)
        async with lock:
            session = self._states.get(session_id)
            if not session:
                raise ValueError(f"Session {session_id} not found.")

            session.is_completed = True
            session.final_response = final_response
            session.updated_at = time.time()
            copied_session = session.model_copy(deep=True)

        return copied_session
