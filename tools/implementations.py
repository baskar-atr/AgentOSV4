import asyncio
import random
from typing import Any, Dict
from tools.base import BaseTool
from events.bus import EventBus
from events.types import TokenStreamEvent, ObservabilityEvent, EventType
from core.config import load_agent_config

class LogsTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="logs_tool",
            description="Queries application, container, and database system log streams for errors and exceptions."
        )

    async def execute(
        self, session_id: str, trace_id: str, input_data: Dict[str, Any], event_bus: EventBus
    ) -> Dict[str, Any]:
        target = input_data.get("target_id", "INC-DEFAULT")
        
        # Helper to stream status messages
        async def stream_log_status(msg: str):
            await event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "INFO", "message": f"[logs_tool] {msg}", "module": "logs_tool"}
            ))
            await event_bus.publish(TokenStreamEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"chunk": f"🔍 Logs: {msg}\n", "task_id": "logs_analysis"}
            ))
            await asyncio.sleep(0.4)

        await stream_log_status(f"Initializing log stream scanner for resource mapping of {target}...")
        await stream_log_status("Connecting to container stdout/stderr collector (Kubernetes cluster: prod-us-east)...")
        await stream_log_status("Querying log index for timestamp window [-15m, +5m] around incident trigger...")
        await stream_log_status("Analyzing 1,248 log entries. Filtering out informational logs...")
        await stream_log_status("CRITICAL: Found Database Connection Pool exhaustion error at 15:28:44 UTC!")
        await stream_log_status("CRITICAL: connection_pool.py:34 - 'QueuePool limit of size 20 overflow 10 reached, connection timed out.'")
        
        return {
            "status": "success",
            "log_lines_scanned": 1248,
            "errors_found": [
                {
                    "timestamp": "2026-05-25T15:28:44Z",
                    "level": "CRITICAL",
                    "logger": "sqlalchemy.pool.impl.QueuePool",
                    "message": "QueuePool limit of size 20 overflow 10 reached, connection timed out, timeout 30.0s."
                }
            ],
            "findings": "Database connection pool exhaustion detected. Application threads are waiting for DB connections, causing HTTP 504 gateway timeouts."
        }


class MetricsTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="metrics_tool",
            description="Fetches CPU, RAM, database connection pool, and networking metrics from Prometheus."
        )

    async def execute(
        self, session_id: str, trace_id: str, input_data: Dict[str, Any], event_bus: EventBus
    ) -> Dict[str, Any]:
        target = input_data.get("target_id", "INC-DEFAULT")
        
        async def stream_metric_status(msg: str):
            await event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "INFO", "message": f"[metrics_tool] {msg}", "module": "metrics_tool"}
            ))
            await event_bus.publish(TokenStreamEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"chunk": f"📈 Metrics: {msg}\n", "task_id": "metrics_analysis"}
            ))
            await asyncio.sleep(0.3)

        await stream_metric_status("Querying Prometheus TSDB metrics API...")
        await stream_metric_status("Analyzing 'http_requests_total' rate: spike in Gateway 504 responses detected...")
        await stream_metric_status("Analyzing 'process_cpu_seconds_total': CPU utilization steady at 42%...")
        await stream_metric_status("Analyzing 'db_connections_active': saturated at 30/30 limit...")
        await stream_metric_status("Analyzing 'db_connections_waiting': 48 requests queued waiting for db connections...")
        
        return {
            "status": "success",
            "metrics": {
                "http_504_rate_per_sec": 8.4,
                "cpu_utilization_pct": 42.1,
                "memory_utilization_pct": 68.5,
                "active_db_connections": 30,
                "max_db_connections_limit": 30,
                "db_connection_queue_size": 48
            },
            "findings": "Prometheus metrics confirm DB connection saturation. Active connections hit the limit of 30, with a queue size of 48 pending requests."
        }


class IncidentSearchTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="incident_search_tool",
            description="Searches ITSM platforms (ServiceNow, PagerDuty) for related historical incident records and runs."
        )

    async def execute(
        self, session_id: str, trace_id: str, input_data: Dict[str, Any], event_bus: EventBus
    ) -> Dict[str, Any]:
        target = input_data.get("target_id", "INC-DEFAULT")
        
        async def stream_incident_status(msg: str):
            await event_bus.publish(ObservabilityEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"level": "INFO", "message": f"[incident_search_tool] {msg}", "module": "incident_search_tool"}
            ))
            await event_bus.publish(TokenStreamEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"chunk": f"📋 Incidents: {msg}\n", "task_id": "incident_search"}
            ))
            await asyncio.sleep(0.35)

        await stream_incident_status(f"Searching ServiceNow KB and incident index for ticket references containing '{target}'...")
        await stream_incident_status("Running semantic search on past 90 days incidents...")
        await stream_incident_status("Found 1 highly correlated resolved incident from 14 days ago: INC-77492.")
        await stream_incident_status("INC-77492 description: 'Database pool exhaustion during weekly traffic peak'. Resolution: 'Increased pool size from 10 to 30'.")
        
        return {
            "status": "success",
            "historical_matches": [
                {
                    "incident_id": "INC-77492",
                    "title": "Database connection pool exhausted on api-gateway",
                    "severity": "P1",
                    "closed_at": "2026-05-11T12:00:00Z",
                    "resolution": "Increased sqlalchemy pool_size in microservice deployment configuration and adjusted backend Gunicorn worker concurrency."
                }
            ],
            "findings": "Historical match INC-77492 indicates that pool limits have been an issue in the past. Suggests database configuration adjustment is required."
        }


class RCAGenerationTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="rca_tool",
            description="Aggregates and synthesizes logs, metrics, and incident databases to produce a professional Root Cause Analysis (RCA) report."
        )

    async def execute(
        self, session_id: str, trace_id: str, input_data: Dict[str, Any], event_bus: EventBus
    ) -> Dict[str, Any]:
        # Retrieve findings from dependencies passed through input_data
        logs_output = input_data.get("logs_analysis", {})
        metrics_output = input_data.get("metrics_analysis", {})
        incidents_output = input_data.get("incident_search", {})

        target = input_data.get("target_id", "INC-123")

        # System prompt simulation log
        await event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"level": "INFO", "message": "[rca_tool] Synthesizing metrics, logs, and incidents into report...", "module": "rca_tool"}
        ))
        model_name = "unknown"
        try:
            model_name = load_agent_config().primary_llm.model_name
        except Exception:
            pass
        await event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={
                "level": "INFO",
                "kind": "llm_call",
                "status": "running",
                "model_name": model_name,
                "provider": "simulated",
                "tool_name": "rca_tool",
                "message": f"LLM generation started ({model_name})",
                "module": "rca_tool",
            }
        ))

        # Generate a professional markdown report template
        report_template = f"""# ROOT CAUSE ANALYSIS REPORT: {target}
**Status**: DRAFT (Generated by Agentic Orchestrator)
**Severity**: High
**Date/Time**: 2026-05-25 15:30 UTC

## 1. Executive Summary
On 2026-05-25, an incident occurred resulting in high latency and 504 Gateway Timeout errors for API consumers. The automated Agentic AI Platform has analyzed logs, service metrics, and history databases. 

The analysis concludes that **database connection pool exhaustion** on the backend microservice led to threads blocking, which eventually starved the API gateway.

---

## 2. Telemetry and Log Findings
- **Log Scanner**:
  - Found QueuePool connection timeouts: `QueuePool limit of size 20 overflow 10 reached, connection timed out.`
  - The errors coincide directly with the start of the 504 response spikes.
- **Metrics Analysis**:
  - Active database connections peaked at **30/30** (100% saturation).
  - Average of **48 requests** were queued in the connection pool waiter line.
  - CPU utilization remained stable at **42%**, ruling out general server overloading.

---

## 3. Historical Correlation
- Found past ticket **INC-77492** (Closed: 14 days ago).
- Historical ticket notes recommend adjusting connection pool limits and tweaking connection timeouts.

---

## 4. Root Cause
The database connection pool size (30 maximum) was insufficient to handle a concurrent traffic spike. Because connection checkouts take longer under peak load, pool starvation occurred, causing incoming requests to block, hit timeout values, and respond with HTTP 504 gateways.

---

## 5. Recommended Remediation Actions
1. **Immediate**: Restart microservice pods to release orphaned/hung connections. (Completed automatically).
2. **Short-Term**: Increase sqlalchemy connection pool limit to **50** and increase max_overflow parameter to **20** in deployment configuration.
3. **Long-Term**: Implement connection recycling and execute a query optimization pass on high-latency operations.
"""

        # Stream this report word-by-word to simulate real-time generation
        words = report_template.split(" ")
        for i, word in enumerate(words):
            chunk = word + " "
            if "\n" in word:
                chunk = word + " "
            is_final = i == len(words) - 1
            await event_bus.publish(TokenStreamEvent(
                trace_id=trace_id, session_id=session_id,
                payload={"chunk": chunk, "task_id": "rca_generation", "is_final": is_final}
            ))
            # Vary sleep slightly to feel like realistic LLM typing
            await asyncio.sleep(0.015 + random.random() * 0.01)

        await event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={"level": "INFO", "message": "[rca_tool] RCA report synthesis completed.", "module": "rca_tool"}
        ))
        await event_bus.publish(ObservabilityEvent(
            trace_id=trace_id, session_id=session_id,
            payload={
                "level": "INFO",
                "kind": "llm_call",
                "status": "completed",
                "model_name": model_name,
                "provider": "simulated",
                "tool_name": "rca_tool",
                "message": f"LLM generation completed ({model_name})",
                "module": "rca_tool",
            }
        ))

        return {
            "status": "success",
            "report": report_template,
            "remediations": [
                "Increase db pool limit to 50",
                "Enable connection recycling",
                "Monitor Prometheus db_connections_waiting metric"
            ]
        }
