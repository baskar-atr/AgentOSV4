import logging
import time
import asyncio
from typing import Any, Dict
from core.state import StateStore, TaskState, TaskNode
from tools.registry import ToolRegistry
from events.bus import EventBus
from events.types import (
    TaskQueuedEvent, TaskStartedEvent, TaskCompletedEvent,
    TaskFailedEvent, ToolStartedEvent, ToolCompletedEvent,
    ObservabilityEvent, ErrorEvent
)

logger = logging.getLogger("TaskExecutor")

class TaskExecutor:
    """
    Handles async execution of individual task nodes. Resolves data bindings,
    triggers underlying tools, updates session state stores, and fires lifecycles.
    """
    def __init__(self, tool_registry: ToolRegistry, event_bus: EventBus):
        self.tool_registry = tool_registry
        self.event_bus = event_bus

    async def execute_task(self, session_id: str, trace_id: str, task_id: str, state_store: StateStore) -> None:
        """
        Executes a specific task by retrieving dependency data, running the tool,
        updating the state registry, and publishing events.
        """
        # 1. Fetch current session and task definition
        session = await state_store.get_session(session_id)
        if not session:
            logger.error(f"Cannot execute task {task_id}: session {session_id} not found.")
            return

        task = session.dag.tasks.get(task_id)
        if not task:
            logger.error(f"Cannot execute task {task_id}: task definition not found in DAG.")
            return

        # 2. Update status to QUEUED then RUNNING
        await state_store.update_task_status(session_id, task_id, TaskState.QUEUED)
        await self.event_bus.publish(TaskQueuedEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"task_id": task_id, "tool": task.tool}
        ))

        await state_store.update_task_status(session_id, task_id, TaskState.RUNNING)
        await self.event_bus.publish(TaskStartedEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"task_id": task_id, "tool": task.tool, "started_at": time.time()}
        ))

        # 3. Pull outputs from dependencies and build tool input payload
        input_data = task.input_data.copy()
        for dep_id in task.depends_on:
            dep_node = session.dag.tasks.get(dep_id)
            if dep_node and dep_node.status == TaskState.COMPLETED:
                # Inject dependency output data into execution context
                input_data[dep_id] = dep_node.output_data

        tool_name = task.tool
        await self.event_bus.publish(ToolStartedEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"tool_name": tool_name, "task_id": task_id, "input_data": input_data}
        ))

        await self.event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"level": "INFO", "message": f"Executing tool '{tool_name}' for task '{task_id}'...", "module": "executor"}
        ))

        # 4. Lookup and execute the tool
        try:
            tool = self.tool_registry.get_tool(tool_name)
            
            # Execute tool asynchronously
            output_data = await tool.execute(session_id, trace_id, input_data, self.event_bus)
            
            # 5. Success transitions
            await state_store.update_task_status(
                session_id, task_id, TaskState.COMPLETED, output_data=output_data
            )
            
            await self.event_bus.publish(ToolCompletedEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"tool_name": tool_name, "task_id": task_id, "output_data": output_data}
            ))

            await self.event_bus.publish(TaskCompletedEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"task_id": task_id, "output_data": output_data, "completed_at": time.time()}
            ))

            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "INFO", "message": f"Task '{task_id}' completed successfully.", "module": "executor"}
            ))

        except Exception as e:
            # 6. Failure transitions
            err_msg = str(e)
            logger.exception(f"Error executing task '{task_id}': {err_msg}")
            
            await state_store.update_task_status(
                session_id, task_id, TaskState.FAILED, error=err_msg
            )
            
            await self.event_bus.publish(TaskFailedEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"task_id": task_id, "error": err_msg, "completed_at": time.time()}
            ))

            await self.event_bus.publish(ErrorEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"error_message": f"Task '{task_id}' failed: {err_msg}", "task_id": task_id}
            ))

            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "ERROR", "message": f"Task '{task_id}' execution failed: {err_msg}", "module": "executor"}
            ))
