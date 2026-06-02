import os
import uuid
import logging
import asyncio
import uvicorn
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.config import (
    AgentConfig,
    load_agent_config,
    load_agent_config_raw,
    save_agent_config,
    update_agent_config,
    apply_prompt_bindings,
    apply_agent_bindings,
    list_agents,
    create_agent,
    set_active_agent,
    get_active_agent_id,
    _agent_path,
)
from builders.prompt_store import PromptStore, PROMPT_SLOTS
from builders.skill_store import SkillStore
from builders.llm_store import LLMStore, LLM_PROVIDERS
from builders.mcp_store import MCPStore, MCP_TRANSPORTS
from builders.tool_store import ToolConfigStore, TOOL_HANDLER_TYPES, sync_tool_definitions_to_registry
from core.chat import ChatStore
from core.state import StateStore, TaskState
from events.bus import EventBus
from events.types import BaseEvent, EventType
from skills.registry import Skill, SkillRegistry
from skills.definitions import register_default_skills
from tools.registry import ToolRegistry
from planner.dynamic_planner import DynamicPlanner
from executor.task_executor import TaskExecutor
from scheduler.dag_scheduler import DAGScheduler
from runtime.engine import OrchestrationEngine
from observability.tracer import TraceRecorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("Gateway")

event_bus = EventBus()
state_store = StateStore(event_bus=event_bus)
chat_store = ChatStore()
skill_registry = SkillRegistry()
tool_registry = ToolRegistry()
trace_recorder = TraceRecorder(event_bus=event_bus)
prompt_store = PromptStore()
skill_store = SkillStore()
llm_store = LLMStore()
mcp_store = MCPStore()
tool_config_store = ToolConfigStore()

executor = TaskExecutor(tool_registry, event_bus)
planner = DynamicPlanner(skill_registry, event_bus)
scheduler = DAGScheduler(executor, event_bus)
engine = OrchestrationEngine(planner, scheduler, state_store, event_bus)


# --- Request / response schemas ---

class QueryRequest(BaseModel):
    query: str
    conversation_id: Optional[str] = None
    agent_id: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    prompts: Optional[Dict[str, str]] = None
    enabled_skills: Optional[List[str]] = None
    enabled_tools: Optional[List[str]] = None
    policies: Optional[Dict[str, Any]] = None
    primary_llm: Optional[Dict[str, Any]] = None
    secondary_llm: Optional[Dict[str, Any]] = None


class CreateAgentRequest(BaseModel):
    name: str
    description: str = ""
    agent_id: Optional[str] = None
    clone_from: Optional[str] = None
    prompts: Optional[Dict[str, str]] = None
    enabled_skills: Optional[List[str]] = None


class CreateSkillRequest(BaseModel):
    name: str
    description: str
    trigger_conditions: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    planner_hints: str = ""
    dependencies: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)
    parallelizable: bool = True
    skill_id: Optional[str] = None


class PromptTemplateRequest(BaseModel):
    name: str
    slot: str
    content: str
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    prompt_id: Optional[str] = None


class PromptTemplateUpdateRequest(BaseModel):
    name: Optional[str] = None
    slot: Optional[str] = None
    content: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None


class SkillDefinitionUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    trigger_conditions: Optional[List[str]] = None
    tools: Optional[List[str]] = None
    planner_hints: Optional[str] = None
    dependencies: Optional[List[str]] = None
    examples: Optional[List[str]] = None
    parallelizable: Optional[bool] = None


class AgentConfigurationRequest(BaseModel):
    prompt_bindings: Optional[Dict[str, str]] = None
    enabled_skills: Optional[List[str]] = None
    enabled_tools: Optional[List[str]] = None
    primary_llm_id: Optional[str] = None
    secondary_llm_id: Optional[str] = None
    enabled_mcp_servers: Optional[List[str]] = None


class LLMModelRequest(BaseModel):
    name: str
    provider: str
    model_name: str
    description: str = ""
    api_key_env: str = ""
    base_url: str = ""
    temperature: float = 0.0
    max_tokens: int = 2048
    tags: List[str] = Field(default_factory=list)
    llm_id: Optional[str] = None


class LLMModelUpdateRequest(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model_name: Optional[str] = None
    description: Optional[str] = None
    api_key_env: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tags: Optional[List[str]] = None


class MCPServerRequest(BaseModel):
    name: str
    transport: str = "stdio"
    description: str = ""
    command: str = ""
    args: List[str] = Field(default_factory=list)
    url: str = ""
    env: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    tags: List[str] = Field(default_factory=list)
    mcp_id: Optional[str] = None


class MCPServerUpdateRequest(BaseModel):
    name: Optional[str] = None
    transport: Optional[str] = None
    description: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    url: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None
    tags: Optional[List[str]] = None


class ToolDefinitionRequest(BaseModel):
    name: str
    description: str = ""
    handler_type: str = "simulated"
    builtin_handler: str = ""
    mcp_server_id: str = ""
    mcp_tool_name: str = ""
    parameters: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    tool_id: Optional[str] = None


class ToolDefinitionUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    handler_type: Optional[str] = None
    builtin_handler: Optional[str] = None
    mcp_server_id: Optional[str] = None
    mcp_tool_name: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None


class ConversationCreateRequest(BaseModel):
    agent_id: Optional[str] = None
    title: str = "New chat"


class ActivateAgentRequest(BaseModel):
    agent_id: str


async def _run_session_with_chat(
    session_id: str,
    trace_id: str,
    query: str,
    conversation_id: Optional[str],
) -> None:
    if conversation_id:
        await chat_store.add_message(conversation_id, "user", query, session_id)

    try:
        final_state = await engine.run_session(session_id, trace_id, query)
        if conversation_id and final_state.final_response:
            await chat_store.add_message(
                conversation_id, "assistant", final_state.final_response, session_id
            )
    except Exception as e:
        if conversation_id:
            await chat_store.add_message(
                conversation_id,
                "assistant",
                f"Sorry, something went wrong: {e}",
                session_id,
            )


def _sync_skills() -> None:
    skill_store.sync_to_registry(skill_registry)


def _sync_tools() -> None:
    sync_tool_definitions_to_registry(tool_registry, tool_config_store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    prompt_store.seed_defaults()
    skill_store.seed_defaults()
    llm_store.seed_defaults()
    mcp_store.seed_defaults()
    tool_config_store.seed_defaults()
    _sync_skills()
    _sync_tools()
    load_agent_config()
    await trace_recorder.start()
    logger.info(
        "Application gateway initialized. Prompts=%s Skills=%s LLMs=%s MCP=%s Tools=%s",
        len(prompt_store.list_all()),
        len(skill_store.list_all()),
        len(llm_store.list_all()),
        len(mcp_store.list_all()),
        len(tool_config_store.list_all()),
    )
    yield
    await trace_recorder.stop()
    logger.info("Application gateway shut down.")


app = FastAPI(
    title="AgentOS Chat",
    description="ChatGPT-style agent platform with configurable skills and dynamic agents.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_ui_dir = os.path.join(os.path.dirname(__file__), "ui")
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate"}


@app.get("/api")
async def api_root():
    return {
        "service": "AgentOS",
        "version": "2.0.0",
        "health": "/api/health",
        "ui": "/",
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": "2.0.0",
        "prompts": len(prompt_store.list_all()),
        "skills": len(skill_store.list_all()),
        "llms": len(llm_store.list_all()),
        "mcp": len(mcp_store.list_all()),
        "tools": len(tool_config_store.list_all()),
        "traced_sessions": len(await trace_recorder.list_sessions()),
    }


@app.get("/")
async def get_index():
    ui_path = os.path.join(_ui_dir, "index.html")
    if not os.path.exists(ui_path):
        raise HTTPException(status_code=404, detail="UI index.html not found.")
    return FileResponse(ui_path, headers=_NO_CACHE)


@app.get("/app.js")
async def serve_app_js():
    """Primary UI script (avoid /ui mount 404 issues)."""
    js_path = os.path.join(_ui_dir, "app.js")
    if not os.path.exists(js_path):
        raise HTTPException(status_code=404, detail="app.js not found.")
    return FileResponse(js_path, media_type="application/javascript", headers=_NO_CACHE)


@app.get("/ui/app.js")
async def serve_app_js_ui_path():
    """Alias for older index.html versions."""
    return await serve_app_js()


def _summarize_session_usage(raw_events: List[BaseEvent]) -> Dict[str, Any]:
    """Extract tools, MCP calls, LLM calls and phase statuses from trace events."""
    tool_defs = {t.name: t for t in tool_config_store.list_all()}
    tools: Dict[str, Dict[str, Any]] = {}
    mcp_calls: Dict[str, Dict[str, Any]] = {}
    llm_calls: Dict[str, Dict[str, Any]] = {}
    phases: Dict[str, Dict[str, Any]] = {}

    for event in raw_events:
        payload = event.payload or {}
        if event.event_type == EventType.TOOL_STARTED:
            tool_name = payload.get("tool_name")
            if not tool_name:
                continue
            defn = tool_defs.get(tool_name)
            tool_kind = defn.handler_type if defn else "builtin"
            tools[tool_name] = {
                "name": tool_name,
                "kind": tool_kind,
                "mcp_server_id": getattr(defn, "mcp_server_id", "") if defn else "",
                "mcp_tool_name": getattr(defn, "mcp_tool_name", "") if defn else "",
                "last_used_at": event.timestamp,
            }
        elif event.event_type == EventType.OBSERVABILITY:
            kind = payload.get("kind")
            if kind == "phase_status":
                phase = payload.get("phase") or "unknown"
                phases[phase] = {
                    "phase": phase,
                    "status": payload.get("status") or "running",
                    "message": payload.get("message") or "",
                    "timestamp": event.timestamp,
                }
            elif kind == "mcp_call":
                key = f"{payload.get('mcp_server_id','')}::{payload.get('mcp_tool_name','')}"
                mcp_calls[key] = {
                    "mcp_server_id": payload.get("mcp_server_id") or "",
                    "mcp_tool_name": payload.get("mcp_tool_name") or "",
                    "tool_name": payload.get("tool_name") or "",
                    "status": payload.get("status") or "completed",
                    "timestamp": event.timestamp,
                }
            elif kind == "llm_call":
                key = payload.get("model_name") or "unknown"
                llm_calls[key] = {
                    "model_name": payload.get("model_name") or "unknown",
                    "provider": payload.get("provider") or "unknown",
                    "tool_name": payload.get("tool_name") or "",
                    "status": payload.get("status") or "completed",
                    "timestamp": event.timestamp,
                }

    return {
        "tools": sorted(tools.values(), key=lambda x: x.get("name", "")),
        "mcp_calls": sorted(mcp_calls.values(), key=lambda x: x.get("timestamp", 0)),
        "llm_calls": sorted(llm_calls.values(), key=lambda x: x.get("timestamp", 0)),
        "phases": sorted(phases.values(), key=lambda x: x.get("timestamp", 0)),
    }


# --- Chat & sessions ---

@app.get("/api/conversations")
async def get_conversations():
    convs = await chat_store.list_conversations()
    return [c.model_dump() for c in convs]


@app.post("/api/conversations")
async def create_conversation(request: ConversationCreateRequest):
    agent_id = request.agent_id or get_active_agent_id()
    conv = await chat_store.create_conversation(agent_id=agent_id, title=request.title)
    return conv.model_dump()


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    conv = await chat_store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv.model_dump()


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    ok = await chat_store.delete_conversation(conversation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"status": "deleted"}


@app.post("/api/sessions")
async def start_session(request: QueryRequest):
    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    trace_id = f"tr_{uuid.uuid4().hex[:8]}"

    if request.agent_id:
        try:
            set_active_agent(request.agent_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{request.agent_id}' not found")

    logger.info(f"Session {session_id}: {request.query[:80]}…")

    await trace_recorder.register_run(
        session_id,
        trace_id,
        query=request.query,
        conversation_id=request.conversation_id,
    )

    asyncio.create_task(
        _run_session_with_chat(
            session_id, trace_id, request.query, request.conversation_id
        )
    )

    return {
        "status": "initiated",
        "session_id": session_id,
        "trace_id": trace_id,
        "conversation_id": request.conversation_id,
        "agent_id": get_active_agent_id(),
    }


@app.get("/api/sessions/{session_id}")
async def get_session_state(session_id: str):
    session = await state_store.get_session(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": f"Session {session_id} not found."})
    return session.model_dump()


@app.get("/api/sessions/{session_id}/trace")
async def get_session_trace(session_id: str):
    timeline = await trace_recorder.get_timeline(session_id)
    raw_events = await trace_recorder.get_session_trace(session_id)
    return {
        "session_id": session_id,
        "timeline": timeline,
        "events": [event.model_dump() for event in raw_events],
    }


@app.get("/api/sessions/{session_id}/usage")
async def get_session_usage(session_id: str):
    raw_events = await trace_recorder.get_session_trace(session_id)
    if not raw_events:
        raise HTTPException(status_code=404, detail=f"No trace data for session '{session_id}'")
    return {
        "session_id": session_id,
        **_summarize_session_usage(raw_events),
    }


# --- Hierarchical observability ---

@app.get("/api/observability/sessions")
async def list_observability_sessions():
    """List recorded runs (threads) with summary metadata (newest first)."""
    return await trace_recorder.list_session_summaries()


@app.get("/api/observability/conversations")
async def list_observability_conversations():
    """Chat sessions that have or may have trace data."""
    convs = await chat_store.list_conversations()
    summaries = await trace_recorder.list_session_summaries()
    by_conv: Dict[str, int] = {}
    for s in summaries:
        cid = s.get("conversation_id") or "_orphan"
        by_conv[cid] = by_conv.get(cid, 0) + 1
    result = []
    for conv in convs:
        result.append({
            "conversation_id": conv.id,
            "title": conv.title,
            "message_count": len(conv.messages),
            "thread_count": by_conv.get(conv.id, 0),
            "updated_at": conv.updated_at,
        })
    if by_conv.get("_orphan"):
        result.append({
            "conversation_id": "_orphan",
            "title": "Unlinked runs",
            "message_count": 0,
            "thread_count": by_conv["_orphan"],
            "updated_at": 0,
        })
    return result


@app.get("/api/observability/hierarchy")
async def get_observability_hierarchy():
    """Full platform tree: Session → Thread → Trace → Span."""
    return await trace_recorder.get_platform_hierarchy(chat_store, state_store)


@app.get("/api/observability/conversations/{conversation_id}")
async def get_observability_conversation(conversation_id: str):
    """Hierarchy for one chat session."""
    if conversation_id == "_orphan":
        platform = await trace_recorder.get_platform_hierarchy(chat_store, state_store)
        orphan = next(
            (c for c in platform["tree"]["children"] if c.get("attributes", {}).get("orphan")),
            None,
        )
        if not orphan:
            raise HTTPException(status_code=404, detail="No unlinked runs")
        return {"conversation_id": "_orphan", "title": "Unlinked runs", "tree": orphan}
    conv = await chat_store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    data = await trace_recorder.get_conversation_hierarchy(
        conversation_id, conv.title, chat_store, state_store
    )
    timeline = []
    for msg in conv.messages:
        if msg.session_id:
            timeline.extend(await trace_recorder.get_timeline(msg.session_id))
    data["timeline"] = sorted(timeline, key=lambda x: x.get("timestamp") or 0)
    return data


@app.get("/api/observability/sessions/{session_id}")
async def get_observability_session(session_id: str):
    """Full observability payload: hierarchical tree, flat timeline, raw events, stats."""
    events = await trace_recorder.get_session_trace(session_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"No trace data for session '{session_id}'")
    session = await state_store.get_session(session_id)
    session_dict = session.model_dump() if session else None
    hierarchy = await trace_recorder.get_hierarchical_trace(session_id, session_dict)
    timeline = await trace_recorder.get_timeline(session_id)
    return {
        **hierarchy,
        "timeline": timeline,
        "events": [e.model_dump() for e in events],
        "usage": _summarize_session_usage(events),
        "session_state": session_dict,
    }


@app.get("/api/observability/sessions/{session_id}/tree")
async def get_observability_tree(session_id: str):
    """Hierarchical span tree only."""
    events = await trace_recorder.get_session_trace(session_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"No trace data for session '{session_id}'")
    session = await state_store.get_session(session_id)
    session_dict = session.model_dump() if session else None
    return await trace_recorder.get_hierarchical_trace(session_id, session_dict)


# --- Agents ---

@app.get("/api/agents")
async def get_agents():
    return list_agents()


@app.post("/api/agents/activate")
async def activate_agent(request: ActivateAgentRequest):
    try:
        set_active_agent(request.agent_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "ok", "active_agent_id": request.agent_id}


@app.post("/api/agents")
async def post_create_agent(request: CreateAgentRequest):
    base = None
    if request.clone_from:
        from core.config import _agent_path
        base = load_agent_config(_agent_path(request.clone_from))
    try:
        cfg = create_agent(
            name=request.name,
            description=request.description,
            base_config=base,
            agent_id=request.agent_id,
        )
        if request.prompts:
            cfg.prompts.update(request.prompts)
        if request.enabled_skills:
            cfg.enabled_skills = request.enabled_skills
        save_agent_config(cfg)
        return cfg.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str):
    path = _agent_path(agent_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Agent not found")
    raw = load_agent_config_raw(path)
    resolved = apply_agent_bindings(raw)
    return {
        **raw.model_dump(),
        "resolved_prompts": resolved.prompts,
        "resolved_primary_llm": resolved.primary_llm.model_dump(),
        "resolved_secondary_llm": resolved.secondary_llm.model_dump(),
    }


@app.get("/api/agents/{agent_id}/configuration")
async def get_agent_configuration(agent_id: str):
    path = _agent_path(agent_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Agent not found")
    raw = load_agent_config_raw(path)
    resolved = apply_agent_bindings(raw)
    return {
        "agent_id": agent_id,
        "agent": raw.model_dump(),
        "resolved_prompts": resolved.prompts,
        "resolved_primary_llm": resolved.primary_llm.model_dump(),
        "resolved_secondary_llm": resolved.secondary_llm.model_dump(),
        "prompt_bindings": raw.prompt_bindings,
        "enabled_skills": raw.enabled_skills,
        "enabled_tools": raw.enabled_tools,
        "primary_llm_id": raw.primary_llm_id,
        "secondary_llm_id": raw.secondary_llm_id,
        "enabled_mcp_servers": raw.enabled_mcp_servers,
        "prompt_slots": [{"slot": k, "description": v} for k, v in PROMPT_SLOTS.items()],
        "prompt_library": [p.model_dump() for p in prompt_store.list_all()],
        "skill_library": [s.model_dump() for s in skill_store.list_all()],
        "llm_library": [m.model_dump() for m in llm_store.list_all()],
        "mcp_library": [m.model_dump() for m in mcp_store.list_all()],
        "tool_library": [t.model_dump() for t in tool_config_store.list_all()],
        "llm_providers": [{"id": k, "label": v} for k, v in LLM_PROVIDERS.items()],
    }


@app.put("/api/agents/{agent_id}/configuration")
async def put_agent_configuration(agent_id: str, request: AgentConfigurationRequest):
    path = _agent_path(agent_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        raw = load_agent_config_raw(path)
        data = request.model_dump(exclude_unset=True)
        for key in (
            "prompt_bindings",
            "enabled_skills",
            "enabled_tools",
            "primary_llm_id",
            "secondary_llm_id",
            "enabled_mcp_servers",
        ):
            if key in data:
                setattr(raw, key, data[key])
        save_agent_config(raw, path)
        cfg = apply_agent_bindings(raw)
        return {
            "status": "ok",
            "agent": cfg.model_dump(),
            "prompt_bindings": cfg.prompt_bindings,
            "enabled_skills": cfg.enabled_skills,
            "enabled_tools": cfg.enabled_tools,
            "primary_llm_id": cfg.primary_llm_id,
            "secondary_llm_id": cfg.secondary_llm_id,
            "enabled_mcp_servers": cfg.enabled_mcp_servers,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- LLM Builder ---

@app.get("/api/builders/llms/meta")
async def llm_builder_meta():
    return {"providers": [{"id": k, "label": v} for k, v in LLM_PROVIDERS.items()]}


@app.get("/api/builders/llms")
async def list_llm_models():
    return [m.model_dump() for m in llm_store.list_all()]


@app.post("/api/builders/llms")
async def create_llm_model(request: LLMModelRequest):
    try:
        profile = llm_store.create(
            name=request.name,
            provider=request.provider,
            model_name=request.model_name,
            description=request.description,
            api_key_env=request.api_key_env,
            base_url=request.base_url,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            tags=request.tags,
            llm_id=request.llm_id,
        )
        return profile.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/builders/llms/{llm_id}")
async def get_llm_model(llm_id: str):
    try:
        return llm_store.get(llm_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="LLM model not found")


@app.put("/api/builders/llms/{llm_id}")
async def update_llm_model(llm_id: str, request: LLMModelUpdateRequest):
    try:
        profile = llm_store.update(llm_id, request.model_dump(exclude_none=True))
        return profile.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="LLM model not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/builders/llms/{llm_id}")
async def delete_llm_model(llm_id: str):
    try:
        if not llm_store.delete(llm_id):
            raise HTTPException(status_code=404, detail="LLM model not found")
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- MCP Builder ---

@app.get("/api/builders/mcp/meta")
async def mcp_builder_meta():
    return {"transports": [{"id": k, "label": v} for k, v in MCP_TRANSPORTS.items()]}


@app.get("/api/builders/mcp")
async def list_mcp_servers():
    return [m.model_dump() for m in mcp_store.list_all()]


@app.post("/api/builders/mcp")
async def create_mcp_server(request: MCPServerRequest):
    try:
        cfg = mcp_store.create(
            name=request.name,
            transport=request.transport,
            description=request.description,
            command=request.command,
            args=request.args,
            url=request.url,
            env=request.env,
            enabled=request.enabled,
            tags=request.tags,
            mcp_id=request.mcp_id,
        )
        return cfg.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/builders/mcp/{mcp_id}")
async def get_mcp_server(mcp_id: str):
    try:
        return mcp_store.get(mcp_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="MCP server not found")


@app.put("/api/builders/mcp/{mcp_id}")
async def update_mcp_server(mcp_id: str, request: MCPServerUpdateRequest):
    try:
        cfg = mcp_store.update(mcp_id, request.model_dump(exclude_none=True))
        return cfg.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="MCP server not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/builders/mcp/{mcp_id}")
async def delete_mcp_server(mcp_id: str):
    try:
        if not mcp_store.delete(mcp_id):
            raise HTTPException(status_code=404, detail="MCP server not found")
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Tool configuration library ---

@app.get("/api/builders/tools/meta")
async def tool_builder_meta():
    from builders.tool_store import BUILTIN_HANDLERS
    return {
        "handler_types": [{"id": k, "label": v} for k, v in TOOL_HANDLER_TYPES.items()],
        "builtin_handlers": [{"id": k, "label": v} for k, v in BUILTIN_HANDLERS.items()],
    }


@app.get("/api/builders/tools")
async def list_tool_definitions():
    return [t.model_dump() for t in tool_config_store.list_all()]


@app.post("/api/builders/tools")
async def create_tool_definition(request: ToolDefinitionRequest):
    try:
        defn = tool_config_store.create(
            name=request.name,
            description=request.description,
            handler_type=request.handler_type,
            builtin_handler=request.builtin_handler,
            mcp_server_id=request.mcp_server_id,
            mcp_tool_name=request.mcp_tool_name,
            parameters=request.parameters,
            tags=request.tags,
            tool_id=request.tool_id,
        )
        _sync_tools()
        return defn.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/builders/tools/{tool_id}")
async def get_tool_definition(tool_id: str):
    try:
        return tool_config_store.get(tool_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Tool not found")


@app.put("/api/builders/tools/{tool_id}")
async def update_tool_definition(tool_id: str, request: ToolDefinitionUpdateRequest):
    try:
        defn = tool_config_store.update(tool_id, request.model_dump(exclude_none=True))
        _sync_tools()
        return defn.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Tool not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/builders/tools/{tool_id}")
async def delete_tool_definition(tool_id: str):
    try:
        if not tool_config_store.delete(tool_id):
            raise HTTPException(status_code=404, detail="Tool not found")
        _sync_tools()
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Config, skills, tools ---

@app.get("/api/config")
async def get_config():
    return load_agent_config().model_dump()


@app.put("/api/config")
async def put_config(request: ConfigUpdateRequest):
    updates = request.model_dump(exclude_none=True)
    cfg = update_agent_config(updates)
    return cfg.model_dump()


@app.get("/api/skills")
async def list_skills_api():
    return [skill.model_dump() for skill in skill_registry.list_skills()]


# --- Prompt Builder ---

@app.get("/api/builders/prompt-slots")
async def get_prompt_slots():
    return [{"slot": k, "description": v} for k, v in PROMPT_SLOTS.items()]


@app.get("/api/builders/prompts")
async def list_prompt_templates():
    return [p.model_dump() for p in prompt_store.list_all()]


@app.post("/api/builders/prompts")
async def create_prompt_template(request: PromptTemplateRequest):
    try:
        tpl = prompt_store.create(
            name=request.name,
            slot=request.slot,
            content=request.content,
            description=request.description,
            tags=request.tags,
            prompt_id=request.prompt_id,
        )
        return tpl.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/builders/prompts/{prompt_id}")
async def get_prompt_template(prompt_id: str):
    try:
        return prompt_store.get(prompt_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Prompt not found")


@app.put("/api/builders/prompts/{prompt_id}")
async def update_prompt_template(prompt_id: str, request: PromptTemplateUpdateRequest):
    try:
        tpl = prompt_store.update(prompt_id, request.model_dump(exclude_none=True))
        return tpl.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Prompt not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/builders/prompts/{prompt_id}")
async def delete_prompt_template(prompt_id: str):
    try:
        if not prompt_store.delete(prompt_id):
            raise HTTPException(status_code=404, detail="Prompt not found")
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Skill Builder ---

@app.get("/api/builders/skills")
async def list_skill_definitions():
    return [s.model_dump() for s in skill_store.list_all()]


@app.post("/api/builders/skills")
async def create_skill_definition(request: CreateSkillRequest):
    try:
        skill = skill_store.create(
            name=request.name,
            description=request.description,
            trigger_conditions=request.trigger_conditions,
            tools=request.tools,
            planner_hints=request.planner_hints,
            dependencies=request.dependencies,
            examples=request.examples,
            parallelizable=request.parallelizable,
            skill_id=request.skill_id,
        )
        _sync_skills()
        return skill.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/builders/skills/{skill_id}")
async def get_skill_definition(skill_id: str):
    try:
        return skill_store.get(skill_id).model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Skill not found")


@app.put("/api/builders/skills/{skill_id}")
async def update_skill_definition(skill_id: str, request: SkillDefinitionUpdateRequest):
    try:
        skill = skill_store.update(skill_id, request.model_dump(exclude_none=True))
        _sync_skills()
        return skill.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Skill not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/builders/skills/{skill_id}")
async def delete_skill_definition(skill_id: str):
    try:
        if not skill_store.delete(skill_id):
            raise HTTPException(status_code=404, detail="Skill not found")
        _sync_skills()
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/skills")
async def register_skill_legacy(request: CreateSkillRequest):
    """Legacy endpoint — delegates to Skill Builder."""
    return await create_skill_definition(request)


@app.get("/api/tools")
async def list_tools_api():
    return [
        {"name": tool.name, "description": tool.description}
        for tool in tool_registry.list_tools()
    ]


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    event_queue = await event_bus.subscribe()

    try:
        session = await state_store.get_session(session_id)
        if session:
            await websocket.send_json({
                "event_type": "STATE_SYNC",
                "session_id": session_id,
                "trace_id": session.trace_id,
                "timestamp": session.created_at,
                "payload": session.model_dump(),
            })
    except Exception as e:
        logger.error(f"State sync failed: {e}")

    try:
        while True:
            event: BaseEvent = await event_queue.get()
            event_queue.task_done()
            if event.session_id == session_id:
                try:
                    await websocket.send_json(event.model_dump())
                except WebSocketDisconnect:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await event_bus.unsubscribe(event_queue)


# Static UI assets — register LAST so API routes take precedence
if os.path.isdir(_ui_dir):
    app.mount("/ui", StaticFiles(directory=_ui_dir), name="ui")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
