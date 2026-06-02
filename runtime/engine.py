import time
import logging
import traceback
from typing import Optional
from core.state import StateStore, SessionState
from planner.dynamic_planner import DynamicPlanner
from scheduler.dag_scheduler import DAGScheduler
from events.bus import EventBus
from events.types import FinalResponseEvent, ErrorEvent, ObservabilityEvent

logger = logging.getLogger("OrchestrationEngine")

class OrchestrationEngine:
    """
    The orchestrator kernel. Integrates planner, scheduler, state store, and event bus
    to manage the runtime lifecycle of user query sessions.
    """
    def __init__(
        self,
        planner: DynamicPlanner,
        scheduler: DAGScheduler,
        state_store: StateStore,
        event_bus: EventBus
    ):
        self.planner = planner
        self.scheduler = scheduler
        self.state_store = state_store
        self.event_bus = event_bus

    async def run_session(self, session_id: str, trace_id: str, query: str) -> SessionState:
        """
        Executes a user query session by planning the DAG, scheduling tasks,
        monitoring progress, and synthesizing the final response.
        """
        start_time = time.time()
        
        # 1. Initialize session in state store
        await self.state_store.create_session(session_id, trace_id, query)
        
        await self.event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"level": "INFO", "message": f"Orchestrator kernel initiated session: {session_id}", "module": "kernel"}
        ))
        await self.event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={
                "level": "INFO",
                "message": "Planning in progress",
                "module": "kernel",
                "kind": "phase_status",
                "phase": "planning",
                "status": "running",
            }
        ))

        try:
            # 2. Dynamic Planning Phase
            dag = await self.planner.generate_plan(session_id, trace_id, query)
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={
                    "level": "INFO",
                    "message": "Planning completed",
                    "module": "kernel",
                    "kind": "phase_status",
                    "phase": "planning",
                    "status": "completed",
                }
            ))
            
            # Update session with plan
            session = await self.state_store.get_session(session_id)
            if not session:
                raise ValueError("Session disappeared during planning.")
            session.dag = dag
            await self.state_store.update_session(session_id, session)

            # 3. Schedule and run the DAG
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={
                    "level": "INFO",
                    "message": "Execution in progress",
                    "module": "kernel",
                    "kind": "phase_status",
                    "phase": "execution",
                    "status": "running",
                }
            ))
            await self.scheduler.schedule_session(session_id, trace_id, self.state_store)
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={
                    "level": "INFO",
                    "message": "Execution completed",
                    "module": "kernel",
                    "kind": "phase_status",
                    "phase": "execution",
                    "status": "completed",
                }
            ))

            # 4. Final synthesis phase
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={
                    "level": "INFO",
                    "message": "Finalization in progress",
                    "module": "kernel",
                    "kind": "phase_status",
                    "phase": "finalization",
                    "status": "running",
                }
            ))
            session = await self.state_store.get_session(session_id)
            if not session:
                raise ValueError("Session state lost prior to finalization.")

            # Resolve the main summary content
            final_text = ""
            if "rca_generation" in session.dag.tasks:
                rca_task = session.dag.tasks["rca_generation"]
                if rca_task.output_data and "report" in rca_task.output_data:
                    final_text = rca_task.output_data["report"]
            
            if not final_text:
                # Fallback: assemble summaries from whatever completed
                parts = []
                for task_id, task in session.dag.tasks.items():
                    if task.status == "COMPLETED" and task.output_data:
                        findings = task.output_data.get("findings", "")
                        if findings:
                            parts.append(f"### {task_id.replace('_', ' ').title()}\n{findings}")
                if parts:
                    final_text = "# Execution Summary\n\n" + "\n\n".join(parts)
                else:
                    final_text = "No findings could be compiled. The execution ended without output."

            # Finalize session
            final_state = await self.state_store.finalize_session(session_id, final_text)
            
            duration = time.time() - start_time
            await self.event_bus.publish(FinalResponseEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"response_text": final_text, "duration_seconds": duration}
            ))
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={
                    "level": "INFO",
                    "message": "Finalization completed",
                    "module": "kernel",
                    "kind": "phase_status",
                    "phase": "finalization",
                    "status": "completed",
                }
            ))
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={
                    "level": "INFO",
                    "message": "Session completed",
                    "module": "kernel",
                    "kind": "phase_status",
                    "phase": "session",
                    "status": "completed",
                }
            ))

            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "INFO", "message": f"Orchestrator kernel finished session execution in {duration:.2f}s.", "module": "kernel"}
            ))

            return final_state

        except Exception as e:
            # Handle catastrophic failures
            err_msg = str(e)
            stack = traceback.format_exc()
            logger.exception(f"Catastrophic failure in session {session_id}: {err_msg}")
            
            await self.event_bus.publish(ErrorEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"error_message": err_msg, "stack_trace": stack}
            ))

            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "CRITICAL", "message": f"Catastrophic execution error: {err_msg}", "module": "kernel"}
            ))
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={
                    "level": "ERROR",
                    "message": "Session failed",
                    "module": "kernel",
                    "kind": "phase_status",
                    "phase": "session",
                    "status": "failed",
                }
            ))
            
            # Attempt to mark session as failed
            try:
                session = await self.state_store.get_session(session_id)
                if session:
                    session.is_completed = True
                    session.final_response = f"An execution error occurred: {err_msg}"
                    await self.state_store.update_session(session_id, session)
            except Exception as se:
                logger.error(f"Failed to update session status on error: {se}")

            raise e
