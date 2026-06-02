"""
Build hierarchical observability trees from flat session event streams.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from events.types import BaseEvent, EventType


def _status_from_task_state(state: Optional[str]) -> str:
    if not state:
        return "pending"
    s = str(state).lower()
    if s in ("completed", "success"):
        return "completed"
    if s in ("failed", "error"):
        return "failed"
    if s in ("running", "queued"):
        return "running"
    return "pending"


def _duration_ms(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None:
        return None
    return round((end - start) * 1000, 2)


def _make_node(
    node_id: str,
    name: str,
    kind: str,
    status: str = "info",
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    children: Optional[List[Dict[str, Any]]] = None,
    **attributes: Any,
) -> Dict[str, Any]:
    node: Dict[str, Any] = {
        "id": node_id,
        "name": name,
        "kind": kind,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "duration_ms": _duration_ms(start_time, end_time),
        "children": children or [],
        "attributes": {k: v for k, v in attributes.items() if v is not None},
    }
    return node


def _append_child(parent: Dict[str, Any], child: Dict[str, Any]) -> None:
    parent["children"].append(child)
    if parent.get("start_time") is None and child.get("start_time") is not None:
        parent["start_time"] = child["start_time"]
    if child.get("end_time") is not None:
        parent["end_time"] = child["end_time"]
    parent["duration_ms"] = _duration_ms(parent.get("start_time"), parent.get("end_time"))


def _rollup_status(parent: Dict[str, Any]) -> None:
    children = parent.get("children") or []
    if not children:
        return
    statuses = [c.get("status") for c in children]
    if any(s == "failed" for s in statuses):
        parent["status"] = "failed"
    elif any(s == "running" for s in statuses):
        parent["status"] = "running"
    elif any(s == "completed" for s in statuses):
        parent["status"] = "completed"
    elif all(s in ("completed", "info", "pending") for s in statuses):
        parent["status"] = "completed" if any(s == "completed" for s in statuses) else "info"
    elif all(s == "info" for s in statuses):
        parent["status"] = "info"


def _finalize_open_spans(
    nodes: List[Dict[str, Any]],
    end_time: Optional[float],
    *,
    force: str = "completed",
) -> None:
    """Close spans still marked running/pending once the run has finished."""
    for node in nodes:
        span_type = (node.get("attributes") or {}).get("span_type")
        status = node.get("status")
        if status in ("running", "pending") and span_type != "event":
            node["status"] = force
            if end_time is not None:
                node["end_time"] = node.get("end_time") or end_time
                node["duration_ms"] = _duration_ms(node.get("start_time"), node.get("end_time"))
        elif status == "running" and span_type == "event":
            node["status"] = "info"
        children = node.get("children") or []
        _finalize_open_spans(children, end_time, force=force)
        _rollup_status(node)


def build_span_tree(
    events: List[BaseEvent],
    session_state: Optional[Dict[str, Any]] = None,
    *,
    run_id: str = "",
) -> Dict[str, Any]:
    """Build span-level tree (planning, tasks, tools) for one trace/run."""
    if not events:
        return {
            "session_id": None,
            "trace_id": None,
            "query": "",
            "status": "empty",
            "start_time": None,
            "end_time": None,
            "duration_ms": None,
            "stats": {},
            "spans": [],
        }

    ordered = sorted(events, key=lambda e: e.timestamp)
    session_id = run_id or ordered[0].session_id
    trace_id = ordered[0].trace_id
    prefix = session_id

    planning = _make_node(
        f"{prefix}:planning", "Planning", "span", status="info", span_type="phase"
    )
    scheduling = _make_node(
        f"{prefix}:scheduling", "Scheduling", "span", status="info", span_type="phase"
    )
    execution = _make_node(
        f"{prefix}:execution", "Execution", "span", status="info", span_type="phase"
    )
    finalization = _make_node(
        f"{prefix}:finalization", "Finalization", "span", status="info", span_type="phase"
    )
    errors_phase = _make_node(
        f"{prefix}:errors", "Errors", "span", status="info", span_type="phase"
    )
    span_roots = [planning, scheduling, execution, finalization]
    phase_nodes = {
        "planning": planning,
        "scheduling": scheduling,
        "execution": execution,
        "finalization": finalization,
    }

    task_nodes: Dict[str, Dict[str, Any]] = {}
    tool_nodes: Dict[str, Dict[str, Any]] = {}
    log_nodes: Dict[str, Dict[str, Any]] = {}
    query = ""
    plan_tasks: List[Dict[str, Any]] = []
    token_counts: Dict[str, int] = {}
    overall_status = "running"
    final_end_time: Optional[float] = None
    stats: Dict[str, int] = {
        "total_events": len(ordered),
        "tasks": 0,
        "tools": 0,
        "errors": 0,
        "token_streams": 0,
    }

    def ensure_task(task_id: str, tool: str = "", depends_on: Optional[List[str]] = None) -> Dict[str, Any]:
        if task_id in task_nodes:
            if tool and not task_nodes[task_id]["attributes"].get("tool"):
                task_nodes[task_id]["attributes"]["tool"] = tool
            return task_nodes[task_id]
        label = tool or task_id
        node = _make_node(
            f"{prefix}:task:{task_id}",
            f"Task · {task_id}",
            "span",
            status="pending",
            span_type="task",
            attributes={"task_id": task_id, "tool": tool, "depends_on": depends_on or []},
        )
        task_nodes[task_id] = node
        _append_child(execution, node)
        stats["tasks"] += 1
        return node

    def ensure_tool(task_id: str, tool_name: str) -> Dict[str, Any]:
        key = f"{task_id}:{tool_name}"
        if key in tool_nodes:
            return tool_nodes[key]
        task = ensure_task(task_id, tool_name)
        node = _make_node(
            f"{prefix}:tool:{key}",
            f"Tool · {tool_name}",
            "span",
            status="pending",
            span_type="tool",
            attributes={"tool_name": tool_name, "task_id": task_id},
        )
        tool_nodes[key] = node
        _append_child(task, node)
        stats["tools"] += 1
        return node

    def add_event_leaf(
        parent: Dict[str, Any],
        event: BaseEvent,
        name: str,
        status: str = "info",
    ) -> Dict[str, Any]:
        leaf = _make_node(
            event.event_id,
            name,
            "span",
            status=status,
            start_time=event.timestamp,
            end_time=event.timestamp,
            span_type="event",
            event_type=event.event_type.value,
            payload=event.payload,
        )
        _append_child(parent, leaf)
        return leaf

    for event in ordered:
        payload = event.payload or {}
        et = event.event_type

        if et == EventType.PLAN_CREATED:
            query = payload.get("query") or query
            plan_tasks = payload.get("tasks") or []
            for t in plan_tasks:
                ensure_task(
                    t.get("id", "unknown"),
                    t.get("tool", ""),
                    t.get("depends_on") or [],
                )
            add_event_leaf(
                planning,
                event,
                f"Plan · {len(plan_tasks)} tasks",
                status="completed",
            )
            planning["status"] = "completed"
            continue

        if et == EventType.OBSERVABILITY:
            module = (payload.get("module") or "system").lower()
            level = (payload.get("level") or "INFO").upper()
            message = payload.get("message") or ""
            if payload.get("kind") == "phase_status":
                phase = payload.get("phase")
                node = phase_nodes.get(phase)
                if node:
                    mapped = _status_from_task_state(payload.get("status") or "running")
                    node["status"] = mapped
                    if mapped == "running" and node.get("start_time") is None:
                        node["start_time"] = event.timestamp
                    if mapped in ("completed", "failed"):
                        node["end_time"] = event.timestamp
                    leaf_status = "info"
                    if mapped == "completed":
                        leaf_status = "completed"
                    elif mapped == "failed":
                        leaf_status = "failed"
                    add_event_leaf(node, event, message or f"{phase} {mapped}", status=leaf_status)
                    continue
            parent = planning if module == "planner" else execution
            if module == "scheduler":
                parent = scheduling
            if module in ("kernel", "orchestrator", "engine"):
                parent = planning
            log_key = f"{module}:{message[:48]}"
            if log_key not in log_nodes:
                log_nodes[log_key] = _make_node(
                    f"{prefix}:log:{len(log_nodes)}",
                    f"[{module}] {message[:120]}",
                    "span",
                    status="failed" if level == "ERROR" else "info",
                    start_time=event.timestamp,
                    span_type="log",
                    attributes={"module": module, "level": level},
                )
                _append_child(parent, log_nodes[log_key])
            else:
                log_nodes[log_key]["end_time"] = event.timestamp
            continue

        if et == EventType.TASK_QUEUED:
            task_id = payload.get("task_id", "unknown")
            task = ensure_task(task_id, payload.get("tool", ""))
            task["status"] = "pending"
            add_event_leaf(task, event, "Queued", status="info")
            continue

        if et == EventType.TASK_STARTED:
            task_id = payload.get("task_id", "unknown")
            task = ensure_task(task_id, payload.get("tool", ""))
            task["status"] = "running"
            if task.get("start_time") is None:
                task["start_time"] = payload.get("started_at") or event.timestamp
            add_event_leaf(task, event, "Started", status="info")
            continue

        if et == EventType.TASK_COMPLETED:
            task_id = payload.get("task_id", "unknown")
            task = ensure_task(task_id, payload.get("tool", ""))
            task["status"] = "completed"
            task["end_time"] = payload.get("completed_at") or event.timestamp
            task["duration_ms"] = _duration_ms(task.get("start_time"), task.get("end_time"))
            add_event_leaf(task, event, "Completed", status="info")
            stream_key = f"{task_id}:stream"
            if stream_key in tool_nodes:
                tool_nodes[stream_key]["status"] = "completed"
                tool_nodes[stream_key]["end_time"] = event.timestamp
            continue

        if et == EventType.TASK_FAILED:
            task_id = payload.get("task_id", "unknown")
            task = ensure_task(task_id, payload.get("tool", ""))
            task["status"] = "failed"
            task["end_time"] = event.timestamp
            overall_status = "failed"
            stats["errors"] += 1
            add_event_leaf(
                task,
                event,
                f"Failed · {payload.get('error', 'unknown')[:80]}",
                status="failed",
            )
            continue

        if et == EventType.TOOL_STARTED:
            task_id = payload.get("task_id", "unknown")
            tool_name = payload.get("tool_name", "tool")
            tool = ensure_tool(task_id, tool_name)
            tool["status"] = "running"
            tool["start_time"] = event.timestamp
            add_event_leaf(tool, event, "Tool started", status="info")
            continue

        if et == EventType.TOOL_COMPLETED:
            task_id = payload.get("task_id", "unknown")
            tool_name = payload.get("tool_name", "tool")
            tool = ensure_tool(task_id, tool_name)
            tool["status"] = "completed"
            tool["end_time"] = event.timestamp
            tool["duration_ms"] = _duration_ms(tool.get("start_time"), tool.get("end_time"))
            add_event_leaf(tool, event, "Tool completed", status="info")
            continue

        if et == EventType.TOKEN_STREAM:
            stats["token_streams"] += 1
            task_id = payload.get("task_id") or "_session"
            key = f"{task_id}:stream"
            if key not in tool_nodes:
                parent = task_nodes.get(task_id) or execution
                tool_nodes[key] = _make_node(
                    f"{prefix}:tokens:{task_id}",
                    "Token stream",
                    "span",
                    status="running",
                    start_time=event.timestamp,
                    span_type="stream",
                    attributes={"task_id": task_id},
                )
                _append_child(parent, tool_nodes[key])
            token_counts[key] = token_counts.get(key, 0) + 1
            tool_nodes[key]["attributes"]["chunks"] = token_counts[key]
            tool_nodes[key]["end_time"] = event.timestamp
            if payload.get("is_final"):
                tool_nodes[key]["status"] = "completed"
            continue

        if et == EventType.STATE_UPDATED:
            task_id = payload.get("task_id")
            if task_id and task_id in task_nodes:
                new_status = payload.get("new_status")
                if new_status:
                    task_nodes[task_id]["status"] = _status_from_task_state(new_status)
            continue

        if et == EventType.FINAL_RESPONSE:
            overall_status = "completed"
            final_end_time = event.timestamp
            finalization["status"] = "completed"
            finalization["start_time"] = event.timestamp
            finalization["end_time"] = event.timestamp
            _finalize_open_spans(span_roots, event.timestamp, force="completed")
            _append_child(
                finalization,
                _make_node(
                    event.event_id,
                    "Final response",
                    "span",
                    status="completed",
                    span_type="event",
                    start_time=event.timestamp,
                    end_time=event.timestamp,
                    duration_seconds=payload.get("duration_seconds"),
                    preview=(payload.get("response_text") or "")[:200],
                ),
            )
            continue

        if et == EventType.ERROR:
            overall_status = "failed"
            stats["errors"] += 1
            _append_child(
                errors_phase,
                _make_node(
                    event.event_id,
                    payload.get("error_message", "Error")[:120],
                    "span",
                    status="failed",
                    span_type="event",
                    start_time=event.timestamp,
                    end_time=event.timestamp,
                    event_type=et.value,
                    payload=payload,
                ),
            )
            continue

    if errors_phase["children"]:
        span_roots.append(errors_phase)
        errors_phase["status"] = "failed"

    if session_state:
        query = query or session_state.get("query") or ""
        dag_tasks = (session_state.get("dag") or {}).get("tasks") or {}
        for task_id, task_data in dag_tasks.items():
            node = ensure_task(
                task_id,
                task_data.get("tool", ""),
                task_data.get("depends_on") or [],
            )
            node["status"] = _status_from_task_state(task_data.get("status"))
            if task_data.get("started_at"):
                node["start_time"] = node["start_time"] or task_data["started_at"]
            if task_data.get("completed_at"):
                node["end_time"] = task_data.get("completed_at")
                node["duration_ms"] = _duration_ms(node.get("start_time"), node.get("end_time"))
            if task_data.get("error"):
                node["attributes"]["error"] = task_data["error"]
        if session_state.get("is_completed"):
            overall_status = "completed" if overall_status != "failed" else "failed"

    start_time = ordered[0].timestamp
    end_time = final_end_time or ordered[-1].timestamp

    if overall_status == "completed":
        _finalize_open_spans(span_roots, end_time, force="completed")

    for phase in (planning, scheduling, execution, finalization, errors_phase):
        _rollup_status(phase)

    return {
        "session_id": session_id,
        "trace_id": trace_id,
        "query": query,
        "status": overall_status,
        "start_time": start_time,
        "end_time": end_time,
        "duration_ms": _duration_ms(start_time, end_time),
        "stats": stats,
        "spans": span_roots,
    }


def build_run_hierarchy(
    events: List[BaseEvent],
    *,
    thread_id: str,
    trace_id: str,
    conversation_id: Optional[str] = None,
    query: str = "",
    session_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Thread → Trace → Spans for one orchestration run."""
    span_data = build_span_tree(events, session_state, run_id=thread_id)
    if not events:
        span_data["trace_id"] = trace_id
        span_data["query"] = query

    trace_id = span_data.get("trace_id") or trace_id
    query = span_data.get("query") or query
    status = span_data.get("status") or "empty"

    trace_node = _make_node(
        trace_id or f"{thread_id}:trace",
        f"Trace · {trace_id or 'unknown'}",
        "trace",
        status=status,
        start_time=span_data.get("start_time"),
        end_time=span_data.get("end_time"),
        conversation_id=conversation_id,
        thread_id=thread_id,
    )
    for span in span_data.get("spans") or []:
        _append_child(trace_node, span)
    _rollup_status(trace_node)

    thread_node = _make_node(
        thread_id,
        f"Thread · {(query or thread_id)[:72]}",
        "thread",
        status=status,
        start_time=span_data.get("start_time"),
        end_time=span_data.get("end_time"),
        conversation_id=conversation_id,
        trace_id=trace_id,
        query=query,
    )
    _append_child(thread_node, trace_node)
    _rollup_status(thread_node)

    return {
        **span_data,
        "thread_id": thread_id,
        "conversation_id": conversation_id,
        "thread": thread_node,
        "trace": trace_node,
    }


def build_conversation_hierarchy(
    conversation_id: str,
    title: str,
    threads: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Session (chat) → Threads → Traces → Spans."""
    if not threads:
        session_node = _make_node(
            conversation_id,
            title or f"Session {conversation_id}",
            "session",
            status="empty",
            conversation_id=conversation_id,
        )
        return {
            "conversation_id": conversation_id,
            "title": title,
            "status": "empty",
            "tree": session_node,
        }

    thread_nodes = [t["thread"] for t in threads if t.get("thread")]
    statuses = [n.get("status") for n in thread_nodes]
    if any(s == "failed" for s in statuses):
        overall = "failed"
    elif any(s == "running" for s in statuses):
        overall = "running"
    elif all(s in ("completed", "empty") for s in statuses):
        overall = "completed"
    else:
        overall = "info"

    starts = [n.get("start_time") for n in thread_nodes if n.get("start_time")]
    ends = [n.get("end_time") for n in thread_nodes if n.get("end_time")]
    start = min(starts) if starts else None
    end = max(ends) if ends else None

    session_node = _make_node(
        conversation_id,
        title or f"Session {conversation_id}",
        "session",
        status=overall,
        start_time=start,
        end_time=end,
        conversation_id=conversation_id,
        thread_count=len(thread_nodes),
    )
    for tn in sorted(thread_nodes, key=lambda n: n.get("start_time") or 0):
        _append_child(session_node, tn)
    _rollup_status(session_node)

    return {
        "conversation_id": conversation_id,
        "title": title,
        "status": overall,
        "start_time": start,
        "end_time": end,
        "duration_ms": _duration_ms(start, end),
        "thread_count": len(thread_nodes),
        "tree": session_node,
    }


def build_platform_hierarchy(
    conversations: List[Dict[str, Any]],
    orphan_threads: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Root platform view: all chat sessions and unlinked runs."""
    children: List[Dict[str, Any]] = []
    for conv in conversations:
        if conv.get("tree"):
            children.append(conv["tree"])
    for run in orphan_threads:
        if run.get("thread"):
            orphan_session = _make_node(
                run.get("conversation_id") or f"orphan:{run.get('thread_id')}",
                run.get("query") or f"Unlinked run {run.get('thread_id')}",
                "session",
                status=run.get("status", "info"),
                orphan=True,
            )
            _append_child(orphan_session, run["thread"])
            children.append(orphan_session)

    root = _make_node("platform", "AgentOS Observability", "platform", status="info")
    for child in sorted(children, key=lambda n: n.get("start_time") or 0, reverse=True):
        _append_child(root, child)
    _rollup_status(root)

    return {"status": "ok", "session_count": len(children), "tree": root}


def build_observability_tree(
    events: List[BaseEvent],
    session_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Backward-compatible: returns thread → trace → spans as `tree`."""
    if not events:
        return {
            "session_id": None,
            "trace_id": None,
            "query": "",
            "status": "empty",
            "tree": _make_node("empty", "No trace data", "session", status="empty"),
        }
    ordered = sorted(events, key=lambda e: e.timestamp)
    run = build_run_hierarchy(
        events,
        thread_id=ordered[0].session_id,
        trace_id=ordered[0].trace_id,
        session_state=session_state,
    )
    return {
        "session_id": run.get("session_id"),
        "trace_id": run.get("trace_id"),
        "query": run.get("query"),
        "status": run.get("status"),
        "start_time": run.get("start_time"),
        "end_time": run.get("end_time"),
        "duration_ms": run.get("duration_ms"),
        "stats": run.get("stats", {}),
        "tree": run["thread"],
    }
