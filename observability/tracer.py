import asyncio
import logging
import time
from collections import defaultdict
from typing import Dict, List, Any, Optional
from events.bus import EventBus
from events.types import BaseEvent, EventType
from observability.hierarchy import (
    build_observability_tree,
    build_run_hierarchy,
    build_conversation_hierarchy,
    build_platform_hierarchy,
)

logger = logging.getLogger("TraceRecorder")

class TraceRecorder:
    """
    Subscribes to the event bus and records a chronological ledger of all events
    emitted during a session's lifetime. Exposes analytical timelines.
    """
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._traces: Dict[str, List[BaseEvent]] = defaultdict(list)
        self._run_meta: Dict[str, Dict[str, Any]] = {}
        self._listener_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def register_run(
        self,
        session_id: str,
        trace_id: str,
        query: str = "",
        conversation_id: Optional[str] = None,
    ) -> None:
        async with self._lock:
            self._run_meta[session_id] = {
                "session_id": session_id,
                "trace_id": trace_id,
                "query": query,
                "conversation_id": conversation_id,
                "started_at": time.time(),
            }

    async def get_run_meta(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            return dict(self._run_meta.get(session_id, {}))

    async def start(self) -> None:
        """
        Starts the background queue consumption listener.
        """
        queue = await self.event_bus.subscribe()
        
        async def listen_loop():
            try:
                while True:
                    event: BaseEvent = await queue.get()
                    queue.task_done()
                    
                    async with self._lock:
                        self._traces[event.session_id].append(event)
                        logger.debug(f"Recorded trace event: {event.event_type} for session: {event.session_id}")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error in trace recorder listener loop: {e}")
            finally:
                await self.event_bus.unsubscribe(queue)

        self._listener_task = asyncio.create_task(listen_loop())
        logger.info("Trace recorder listener loop started.")

    async def stop(self) -> None:
        """
        Stops the tracer background process.
        """
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            logger.info("Trace recorder listener loop stopped.")

    async def get_session_trace(self, session_id: str) -> List[BaseEvent]:
        async with self._lock:
            # Return a copy of the recorded events list
            return list(self._traces.get(session_id, []))

    async def list_sessions(self) -> List[str]:
        async with self._lock:
            return list(self._traces.keys())

    async def get_session_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        events = await self.get_session_trace(session_id)
        if not events:
            return None
        ordered = sorted(events, key=lambda e: e.timestamp)
        trace_id = ordered[0].trace_id
        query = ""
        status = "running"
        error_count = 0
        for event in ordered:
            if event.event_type == EventType.PLAN_CREATED:
                query = event.payload.get("query") or query
            elif event.event_type == EventType.FINAL_RESPONSE:
                status = "completed"
            elif event.event_type in (EventType.ERROR, EventType.TASK_FAILED):
                error_count += 1
                status = "failed"
        meta = await self.get_run_meta(session_id)
        return {
            "session_id": session_id,
            "trace_id": trace_id,
            "conversation_id": meta.get("conversation_id"),
            "query": query or meta.get("query") or "",
            "status": status,
            "event_count": len(ordered),
            "error_count": error_count,
            "start_time": ordered[0].timestamp,
            "end_time": ordered[-1].timestamp,
            "duration_ms": round((ordered[-1].timestamp - ordered[0].timestamp) * 1000, 2),
        }

    async def list_session_summaries(self) -> List[Dict[str, Any]]:
        session_ids = await self.list_sessions()
        summaries = []
        for session_id in session_ids:
            summary = await self.get_session_summary(session_id)
            if summary:
                summaries.append(summary)
        summaries.sort(key=lambda s: s.get("start_time") or 0, reverse=True)
        return summaries

    async def get_hierarchical_trace(
        self,
        session_id: str,
        session_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        events = await self.get_session_trace(session_id)
        meta = await self.get_run_meta(session_id)
        if not events:
            return build_observability_tree(events, session_state)
        ordered = sorted(events, key=lambda e: e.timestamp)
        run = build_run_hierarchy(
            events,
            thread_id=session_id,
            trace_id=meta.get("trace_id") or ordered[0].trace_id,
            conversation_id=meta.get("conversation_id"),
            query=meta.get("query") or "",
            session_state=session_state,
        )
        return {
            "session_id": session_id,
            "trace_id": run.get("trace_id"),
            "conversation_id": run.get("conversation_id"),
            "query": run.get("query"),
            "status": run.get("status"),
            "start_time": run.get("start_time"),
            "end_time": run.get("end_time"),
            "duration_ms": run.get("duration_ms"),
            "stats": run.get("stats", {}),
            "tree": run["thread"],
        }

    async def get_conversation_hierarchy(
        self,
        conversation_id: str,
        title: str,
        chat_store: Any,
        state_store: Any,
    ) -> Dict[str, Any]:
        conv = await chat_store.get_conversation(conversation_id)
        thread_ids: List[str] = []
        if conv:
            title = conv.title or title
            for msg in conv.messages:
                if msg.session_id and msg.session_id not in thread_ids:
                    thread_ids.append(msg.session_id)
        summaries = await self.list_session_summaries()
        for s in summaries:
            if s.get("conversation_id") == conversation_id and s["session_id"] not in thread_ids:
                thread_ids.append(s["session_id"])

        threads = []
        for tid in thread_ids:
            events = await self.get_session_trace(tid)
            if not events:
                continue
            meta = await self.get_run_meta(tid)
            session = await state_store.get_session(tid)
            session_dict = session.model_dump() if session else None
            threads.append(
                build_run_hierarchy(
                    events,
                    thread_id=tid,
                    trace_id=meta.get("trace_id") or events[0].trace_id,
                    conversation_id=conversation_id,
                    query=meta.get("query") or "",
                    session_state=session_dict,
                )
            )
        return build_conversation_hierarchy(conversation_id, title, threads)

    async def get_platform_hierarchy(self, chat_store: Any, state_store: Any) -> Dict[str, Any]:
        conversations = []
        convs = await chat_store.list_conversations()
        for conv in convs:
            conversations.append(
                await self.get_conversation_hierarchy(
                    conv.id, conv.title, chat_store, state_store
                )
            )
        summaries = await self.list_session_summaries()
        linked = set()
        for c in conversations:
            for child in (c.get("tree") or {}).get("children") or []:
                linked.add(child.get("id"))

        orphan_threads = []
        for s in summaries:
            sid = s["session_id"]
            if sid in linked:
                continue
            events = await self.get_session_trace(sid)
            if not events:
                continue
            meta = await self.get_run_meta(sid)
            session = await state_store.get_session(sid)
            orphan_threads.append(
                build_run_hierarchy(
                    events,
                    thread_id=sid,
                    trace_id=meta.get("trace_id") or s.get("trace_id"),
                    conversation_id=meta.get("conversation_id"),
                    query=meta.get("query") or s.get("query") or "",
                    session_state=session.model_dump() if session else None,
                )
            )
        return build_platform_hierarchy(conversations, orphan_threads)

    async def get_timeline(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Translates raw event lists into human-readable chronological reports.
        """
        events = await self.get_session_trace(session_id)
        timeline = []
        
        for event in events:
            readable_msg = ""
            payload = event.payload
            
            if event.event_type == EventType.PLAN_CREATED:
                tasks = ", ".join([f"{t['id']} ({t['tool']})" for t in payload.get("tasks", [])])
                readable_msg = f"Planner compiled execution graph: [{tasks}] for query: '{payload.get('query')}'"
            elif event.event_type == EventType.TASK_QUEUED:
                readable_msg = f"Task '{payload.get('task_id')}' queued in scheduler."
            elif event.event_type == EventType.TASK_STARTED:
                readable_msg = f"Task '{payload.get('task_id')}' started executing tool '{payload.get('tool')}'."
            elif event.event_type == EventType.TASK_COMPLETED:
                readable_msg = f"Task '{payload.get('task_id')}' completed successfully."
            elif event.event_type == EventType.TASK_FAILED:
                readable_msg = f"Task '{payload.get('task_id')}' failed: {payload.get('error')}."
            elif event.event_type == EventType.TOOL_STARTED:
                readable_msg = f"Tool '{payload.get('tool_name')}' invoked for task '{payload.get('task_id')}'."
            elif event.event_type == EventType.TOOL_COMPLETED:
                readable_msg = f"Tool '{payload.get('tool_name')}' execution complete."
            elif event.event_type == EventType.TOKEN_STREAM:
                # Token streams are fine-grained, we only summarize them or skip in timeline to avoid noise,
                # but we can add a lightweight entry if it's the start of a stream
                continue
            elif event.event_type == EventType.STATE_UPDATED:
                readable_msg = f"Task state machine transition: {payload.get('task_id')} changed from {payload.get('prev_status')} to {payload.get('new_status')}."
            elif event.event_type == EventType.OBSERVABILITY:
                readable_msg = f"[{payload.get('module', 'system').upper()}] {payload.get('message')}"
            elif event.event_type == EventType.FINAL_RESPONSE:
                readable_msg = f"Final report generated in {payload.get('duration_seconds', 0.0):.2f} seconds."
            elif event.event_type == EventType.ERROR:
                readable_msg = f"System Error encountered: {payload.get('error_message')}"
            
            if readable_msg:
                timeline.append({
                    "timestamp": event.timestamp,
                    "event_type": event.event_type.value,
                    "message": readable_msg,
                    "event_id": event.event_id
                })
                
        return timeline
