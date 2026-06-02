import asyncio
import logging
from core.state import StateStore, TaskState, DAGState
from executor.task_executor import TaskExecutor
from events.bus import EventBus
from events.types import BaseEvent, EventType, ObservabilityEvent

logger = logging.getLogger("DAGScheduler")

class DAGScheduler:
    """
    Event-driven scheduler that manages the execution lifecycle of a session's task graph.
    Subscribes to completion events to dynamically trigger downstream ready tasks.
    """
    def __init__(self, executor: TaskExecutor, event_bus: EventBus):
        self.executor = executor
        self.event_bus = event_bus

    async def schedule_session(self, session_id: str, trace_id: str, state_store: StateStore) -> None:
        """
        Main execution loop for scheduling tasks in a session.
        Listens to task changes via the event bus to schedule downstream tasks.
        """
        # 1. Subscribe to the event bus to listen for completed/failed tasks
        event_queue = await self.event_bus.subscribe()
        trigger_event = asyncio.Event()

        # 2. Start a background task to process events and wake up the scheduler loop
        async def event_listener():
            try:
                while True:
                    event: BaseEvent = await event_queue.get()
                    event_queue.task_done()
                    
                    # We are only interested in events for this session
                    if event.session_id != session_id:
                        continue

                    # Wake up scheduler on completion, failure, or error
                    if event.event_type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED, EventType.ERROR):
                        logger.debug(f"Scheduler received trigger event: {event.event_type} for task in session {session_id}")
                        trigger_event.set()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error in scheduler event listener: {e}")

        listener_task = asyncio.create_task(event_listener())

        # 3. Main scheduler execution loop
        try:
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "INFO", "message": "DAG Scheduler started monitoring task graph.", "module": "scheduler"}
            ))
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={
                    "level": "INFO",
                    "message": "Scheduling in progress",
                    "module": "scheduler",
                    "kind": "phase_status",
                    "phase": "scheduling",
                    "status": "running",
                }
            ))

            while True:
                # Fetch latest session state
                session = await state_store.get_session(session_id)
                if not session:
                    raise ValueError(f"Session {session_id} state lost.")

                dag: DAGState = session.dag

                # Check terminating conditions
                if dag.is_complete():
                    await self.event_bus.publish(ObservabilityEvent(
                        trace_id=trace_id, session_id=session_id,
                        payload={"level": "INFO", "message": "All DAG tasks completed successfully.", "module": "scheduler"}
                    ))
                    await self.event_bus.publish(ObservabilityEvent(
                        trace_id=trace_id, session_id=session_id,
                        payload={
                            "level": "INFO",
                            "message": "Scheduling completed",
                            "module": "scheduler",
                            "kind": "phase_status",
                            "phase": "scheduling",
                            "status": "completed",
                        }
                    ))
                    break

                if dag.is_failed():
                    await self.event_bus.publish(ObservabilityEvent(
                        trace_id=trace_id, session_id=session_id,
                        payload={"level": "ERROR", "message": "DAG execution failed due to task failures.", "module": "scheduler"}
                    ))
                    await self.event_bus.publish(ObservabilityEvent(
                        trace_id=trace_id, session_id=session_id,
                        payload={
                            "level": "ERROR",
                            "message": "Scheduling failed",
                            "module": "scheduler",
                            "kind": "phase_status",
                            "phase": "scheduling",
                            "status": "failed",
                        }
                    ))
                    raise RuntimeError("DAG execution failed: one or more tasks failed.")

                # Identify runnable tasks
                runnable_tasks = dag.get_runnable_tasks()
                
                # Filter out tasks that are already scheduled or running (double-check status)
                tasks_to_run = []
                for task in runnable_tasks:
                    # Update status to QUEUED in memory store immediately to avoid double-triggers
                    # prior to starting executor task
                    await state_store.update_task_status(session_id, task.id, TaskState.QUEUED)
                    tasks_to_run.append(task)

                if tasks_to_run:
                    await self.event_bus.publish(ObservabilityEvent(
                        trace_id=trace_id, session_id=session_id,
                        payload={"level": "INFO", "message": f"Scheduling {len(tasks_to_run)} parallel tasks: {[t.id for t in tasks_to_run]}", "module": "scheduler"}
                    ))
                    await self.event_bus.publish(ObservabilityEvent(
                        trace_id=trace_id, session_id=session_id,
                        payload={
                            "level": "INFO",
                            "message": f"Scheduling {len(tasks_to_run)} task(s)",
                            "module": "scheduler",
                            "kind": "phase_status",
                            "phase": "scheduling",
                            "status": "running",
                            "task_ids": [t.id for t in tasks_to_run],
                        }
                    ))
                    
                    # Start executor tasks concurrently in background
                    for task in tasks_to_run:
                        asyncio.create_task(
                            self.executor.execute_task(session_id, trace_id, task.id, state_store)
                        )

                # Reset trigger event and wait for changes
                trigger_event.clear()
                
                # Check running counts
                running_tasks = [t for t in dag.tasks.values() if t.status in (TaskState.RUNNING, TaskState.QUEUED)]
                
                if not tasks_to_run and not running_tasks:
                    # Deadlock detection: no tasks to run and nothing currently running, but DAG not complete
                    uncompleted = [t.id for t in dag.tasks.values() if t.status != TaskState.COMPLETED]
                    err_msg = f"Scheduler deadlock detected! Uncompleted tasks: {uncompleted} cannot progress due to missing dependencies."
                    logger.error(err_msg)
                    raise RuntimeError(err_msg)

                # Sleep/wait until a task finishes and triggers us
                # Timeout of 10s as safety backup
                try:
                    await asyncio.wait_for(trigger_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass

        finally:
            # Clean up event queue subscription and listener task
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass
            await self.event_bus.unsubscribe(event_queue)
