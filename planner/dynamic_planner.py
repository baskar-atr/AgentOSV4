import re
import logging
from typing import List
from core.config import load_agent_config
from core.state import DAGState, TaskNode, TaskState
from skills.registry import SkillRegistry, Skill
from events.bus import EventBus
from events.types import PlanCreatedEvent, ObservabilityEvent

logger = logging.getLogger("DynamicPlanner")

class DynamicPlanner:
    """
    Simulates a cognitive planner. Matches user requests to skills and constructs
    a directed acyclic graph (DAG) of task execution blocks.
    """
    def __init__(self, skill_registry: SkillRegistry, event_bus: EventBus):
        self.skill_registry = skill_registry
        self.event_bus = event_bus

    async def generate_plan(self, session_id: str, trace_id: str, query: str) -> DAGState:
        await self.event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"level": "INFO", "message": f"Planner processing query: '{query}'", "module": "planner"}
        ))

        # Extract incident ID if present (e.g., INC123, INC-999)
        inc_match = re.search(r'(INC-?\d+)', query, re.IGNORECASE)
        target_id = inc_match.group(1).upper() if inc_match else "INC-123"

        config = load_agent_config()
        enabled = set(config.enabled_skills)

        # Match skills from registry, filtered by agent config
        matched_skills = [
            s for s in self.skill_registry.match_skills(query)
            if s.name in enabled
        ]

        # If no skills matched, fallback to all enabled skills
        if not matched_skills:
            await self.event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "WARNING", "message": "No direct skills matched, applying enabled skills fallback.", "module": "planner"}
            ))
            matched_skills = [s for s in self.skill_registry.list_skills() if s.name in enabled]

        tasks = {}

        # Build the DAG nodes
        # If IncidentTriageSkill is present:
        has_triage = any(s.name == "IncidentTriageSkill" for s in matched_skills)
        has_rca = any(s.name == "RootCauseAnalysisSkill" for s in matched_skills)

        # Force triage tasks if rca is requested (dependency mapping)
        if has_rca or has_triage:
            # Add logs, metrics, incident search
            tasks["logs_analysis"] = TaskNode(
                id="logs_analysis",
                tool="logs_tool",
                depends_on=[],
                status=TaskState.PENDING,
                input_data={"target_id": target_id}
            )
            tasks["metrics_analysis"] = TaskNode(
                id="metrics_analysis",
                tool="metrics_tool",
                depends_on=[],
                status=TaskState.PENDING,
                input_data={"target_id": target_id}
            )
            tasks["incident_search"] = TaskNode(
                id="incident_search",
                tool="incident_search_tool",
                depends_on=[],
                status=TaskState.PENDING,
                input_data={"target_id": target_id}
            )

        if has_rca:
            # Add RCA node which depends on the other three
            tasks["rca_generation"] = TaskNode(
                id="rca_generation",
                tool="rca_tool",
                depends_on=["logs_analysis", "metrics_analysis", "incident_search"],
                status=TaskState.PENDING,
                input_data={"target_id": target_id}
            )

        dag = DAGState(tasks=tasks)

        # Publish the plan created event
        plan_details = {
            "query": query,
            "target_id": target_id,
            "matched_skills": [s.name for s in matched_skills],
            "tasks": [
                {
                    "id": t.id,
                    "tool": t.tool,
                    "depends_on": t.depends_on
                } for t in dag.tasks.values()
            ]
        }
        
        await self.event_bus.publish(PlanCreatedEvent(
            trace_id=trace_id,
            session_id=session_id,
            payload=plan_details
        ))

        await self.event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"level": "INFO", "message": f"Successfully compiled execution plan with {len(tasks)} tasks.", "module": "planner"}
        ))

        return dag
