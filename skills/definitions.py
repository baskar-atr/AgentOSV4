from skills.registry import Skill, SkillRegistry

def register_default_skills(registry: SkillRegistry) -> None:
    # 1. Incident Triage Skill
    triage_skill = Skill(
        name="IncidentTriageSkill",
        description="Gathers logs, performance metrics, and database ticket search history to understand production issues.",
        trigger_conditions=[
            "incident", "inc", "issue", "bug", "crash", 
            "production issue", "outage", "triage", "error"
        ],
        tools=["logs_tool", "metrics_tool", "incident_search_tool"],
        planner_hints="Fetches observability details and ticket reports. These logs, metrics, and tickets do not depend on each other and should run concurrently.",
        dependencies=[],
        examples=[
            "Analyze production issue INC123",
            "Triage incident INC999",
            "What went wrong with the database yesterday?"
        ],
        parallelizable=True
    )
    registry.register(triage_skill)

    # 2. Root Cause Analysis Skill
    rca_skill = Skill(
        name="RootCauseAnalysisSkill",
        description="Performs final aggregation and synthesizes root cause metrics, logs, and ticket histories to draft an incident report.",
        trigger_conditions=["rca", "root cause", "analyze", "synthesize", "explain why"],
        tools=["rca_tool"],
        planner_hints="Generates final RCA reports. Requires triage details as prerequisites. Must execute after triage completes.",
        dependencies=["IncidentTriageSkill"],
        examples=[
            "Analyze production issue INC123 and generate RCA",
            "Write a root cause analysis for the database crash"
        ],
        parallelizable=False
    )
    registry.register(rca_skill)
