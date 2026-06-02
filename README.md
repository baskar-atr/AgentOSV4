# AetherOS: Real-Time Event-Driven Dynamic Agentic AI Platform

## Quick start (UI + backend)

```bash
cd AgentOSV4
pip3 install -r requirements.txt
./start.sh
```

Then open **http://localhost:8000** in your browser (not `file://`).

Verify the API:

```bash
curl http://localhost:8000/api/health
# {"status":"ok","version":"2.0.0",...}
```

If you see `{"detail":"Not Found"}`, an **old server** is still on port 8000 — stop it (`Ctrl+C`) and run `./start.sh` again.

---

AetherOS is a production-grade, event-driven orchestration kernel designed for dynamic Agentic AI workloads. Built on top of Python, FastAPI, asyncio, and WebSockets, AetherOS models agent reasoning as dynamic, state-driven Directed Acyclic Graphs (DAGs) of tasks, executing them concurrently based on dependency mapping while streaming granular telemetry and final reports back to the user in real time.

---

## 1. System Architecture

AetherOS follows a clean, decoupled service architecture:

```
                  +---------------------------------------+
                  |         Web Browser / Client          |
                  +-------------------+-------------------+
                                      |
                           (HTTP POST | WebSocket)
                                      v
                  +-------------------+-------------------+
                  |        FastAPI Gateway (app.py)        |
                  +---------+-------------------+---------+
                            |                   ^
             (Launch Run)   |                   | (Filter & Fan-out
             to Thread      |                   |  Websocket Events)
                            v                   |
                  +---------+-------------------+---------+
                  |     Orchestration Engine (runtime/)   |<---+
                  +---------+-------------------+---------+    |
                            |                   |            |
         (Generate Plan)    |                   |            | (Read/Write
                            v                   |            |  Session State)
                  +---------+---------+         |            |
                  | Dynamic Planner   |         |            |
                  |    (planner/)     |         |            |
                  +-------------------+         |            v
                                                |     +------+------+
                                                |     | State Store |
         (Schedule Ready Nodes)                 |     |   (core/)   |
                            v                   |     +------+------+
                  +---------+---------+         |            ^
                  |   DAG Scheduler   |         |            |
                  |   (scheduler/)    |         |            |
                  +---------+---------+         |            | (Trigger Status
                            |                   |            |  Transitions)
             (Run Workers   |                   |            |
              Parallelly)   v                   v            |
                  +---------+---------+   +-----+-----+      |
                  |  Task Executor    +-->| Event Bus +------+
                  |    (executor/)    |   | (events/) |
                  +---------+---------+   +-----+-----+
                            |                   |
                            v                   v
                     +------+------+     +------+------+
                     | Async Tools |     |   Tracer    |
                     |  (tools/)   |     | (observ/t)  |
                     +-------------+     +-------------+
```

### Layer Descriptions

1. **API Gateway Layer (`app.py`)**: Exposes REST endpoints for session instantiation and catalog inspection. Hosts a WebSocket connection dispatcher which hooks client connections to active event streams.
2. **Orchestration Kernel (`runtime/engine.py`)**: Interlinks the planner, scheduler, state stores, and event systems. Coordinates session state compilation and formats final response synthesis.
3. **Dynamic Planner (`planner/dynamic_planner.py`)**: Evaluates unstructured user input against trigger keywords in the `SkillRegistry`, resolving and constructing a logical execution flow (DAG) made of distinct task nodes.
4. **State Machine Store (`core/state.py`)**: Thread-safe in-memory key-value database managing session attributes and state transitions (`PENDING` -> `QUEUED` -> `RUNNING` -> `COMPLETED` / `FAILED`).
5. **DAG Scheduler (`scheduler/dag_scheduler.py`)**: Evaluates active task graph lists. Launches independent tasks concurrently and blocks downstream task nodes until parent prerequisites are resolved.
6. **Task Executor (`executor/task_executor.py`)**: Extracts completed outputs from dependency nodes, merges them into input arguments, triggers concrete tool modules, and tracks worker failures.
7. **Fan-Out Event Bus (`events/bus.py`)**: A decoupled async event bus utilizing subscriber queues. Emits granular telemetry (`PLAN_CREATED`, `TASK_STARTED`, `TOKEN_STREAM`, `FINAL_RESPONSE`) to listeners.
8. **Observability Tracer (`observability/tracer.py`)**: Records chronologically structured timeline logs of internal event flows, facilitating post-mortem inspections.

---

## 2. Core Execution Runtime Flow

A user session executes through the following transactional cycle:

```
[User Query]
     │
     ▼
[Planner Engine] ────► 1. Match trigger conditions to Registered Skills.
     │                 2. Generate task graph (DAGState) and map dependencies.
     ▼
[State Store]    ────► 3. Initialize SessionState and write DAG.
     │                 4. Emit PLAN_CREATED event to Event Bus.
     ▼
[DAG Scheduler]  ────► 5. Run execution loop in background.
     │                 6. Fetch tasks where (status == PENDING) and (dependencies met).
     ▼
[Task Executor]  ────► 7. Resolve variables. Pull dependency outputs.
     │                 8. Spin up Tool Executor (asyncio.create_task).
     ▼
[Async Tools]    ────► 9. Execute logic (query logs/metrics/incidents).
     │                 10. Stream intermediate output chunks (TOKEN_STREAM).
     ▼
[Event Bus]      ────► 11. Fan-out events to (Tracer, Websocket API, Logger).
     │
     ▼
[Websocket Gateway] ──► 12. Push JSON state changes to Frontend UI in real-time.
     │
     ▼
[Scheduler Loop] ────► 13. Detect TASK_COMPLETED event. Set trigger event to re-evaluate.
     │                 14. Schedule downstream tasks (e.g. rca_generation).
     ▼
[Orchestrator]   ────► 15. All tasks complete. Compile final output and emit FINAL_RESPONSE.
```

---

## 3. Dynamic DAG Scheduling & Concurrency

AetherOS resolves and runs tasks without hardcoded workflows:

1. **Prerequisite Resolution**: The `DAGState.get_runnable_tasks()` function identifies any task whose state is currently `PENDING` and whose parents in `depends_on` are marked `COMPLETED`.
2. **Concurrent Dispatch**: The scheduler calls `asyncio.create_task(executor.execute_task(...))` for every runnable node simultaneously. When the triage phase launches, `logs_analysis`, `metrics_analysis`, and `incident_search` execute in parallel via `asyncio`.
3. **Variable Inter-Binding**: When the `rca_generation` task starts, the `TaskExecutor` aggregates output payloads from completed parents:
   ```python
   input_data = task.input_data.copy()
   for dep_id in task.depends_on:
       dep_node = session.dag.tasks.get(dep_id)
       if dep_node and dep_node.status == TaskState.COMPLETED:
           input_data[dep_id] = dep_node.output_data
   ```
4. **Event-Driven Rescheduling**: The scheduler suspends itself waiting on an `asyncio.Event`. When a worker completes and emits a `TASK_COMPLETED` or `TASK_FAILED` event, the event listener triggers the scheduler's event, waking it up to compute the next runnable layer of the graph.

---

## 4. Setup & Running Instructions

### Prerequisites
- Python 3.9 or higher
- Web Browser (Chrome, Safari, Firefox)

### Installation
1. Clone the project workspace or navigate to the directory:
   ```bash
   cd /Users/wspl-0335/AgentOS/AgentOSV4
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Launch the FastAPI application:
   ```bash
   python app.py
   ```
   *Alternatively, run with uvicorn directly:*
   ```bash
   uvicorn app:app --port 8000 --host 0.0.0.0 --reload
   ```

4. Access the Interactive Dashboard:
   Open your browser and navigate to: [http://localhost:8000](http://localhost:8000)

---

## 5. Architectural Upgrade & Future Migration Paths

AetherOS is designed with modular abstractions to support enterprise scale:

### Kafka/Message Queue Migration
The in-memory `EventBus` (`events/bus.py`) can be replaced with a distributed message queue (e.g. Apache Kafka, RabbitMQ, or Redis Streams):
- **Publisher**: The `EventBus.publish()` method can write events to a specific Kafka topic (e.g., `agent-telemetry-events`).
- **Consumer**: Distributed websocket worker instances can run Kafka consumers listening to partitions and streaming events to target client sessions.

### Temporal Migration for Distributed Workflows
For multi-hour execution runs, human-in-the-loop approvals, or heavy state resilience:
- **Scheduler**: Replace the `DAGScheduler` loop with a **Temporal Workflow**.
- **Executor**: Implement the `TaskExecutor` as a **Temporal Activity**. Temporal handles backoffs, activity timeouts, distributed workers, and persistent execution state checkpoints automatically.

### Model Context Protocol (MCP) Integration
AetherOS is ready to support the Model Context Protocol:
- **Registry**: Extend the `ToolRegistry` to query remote MCP servers.
- **Client**: Integrate an MCP client that dynamically loads tools and handles payload execution, expanding the platform's capabilities to include external database connections, terminal commands, or file system modifications.
