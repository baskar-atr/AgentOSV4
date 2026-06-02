#!/usr/bin/env bash
# Start AgentOS backend + verify API before opening browser
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

echo "==> AgentOS — stopping anything on port $PORT..."
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti :"$PORT" 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo "    Killing stale process(es): $PIDS"
    kill $PIDS 2>/dev/null || true
    sleep 1
    PIDS=$(lsof -ti :"$PORT" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
      echo "    Force-killing remaining: $PIDS"
      kill -9 $PIDS 2>/dev/null || true
      sleep 1
    fi
  fi
fi

echo "==> Checking Python dependencies..."
python3 -c "import fastapi, uvicorn" 2>/dev/null || {
  echo "    Installing requirements..."
  python3 -m pip install -r requirements.txt
}

echo "==> Starting server on http://127.0.0.1:$PORT"
echo "    Open: http://localhost:$PORT"
echo "    Press Ctrl+C to stop"
echo ""

python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT" --reload &
UV_PID=$!
trap 'kill $UV_PID 2>/dev/null' EXIT

echo "==> Waiting for API (builders: llm, mcp, tools)..."
for _ in $(seq 1 30); do
  HEALTH=$(curl -sf "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)
  if echo "$HEALTH" | grep -q '"llms"'; then
    echo "    Ready: $HEALTH"
    break
  fi
  sleep 0.5
done
if ! echo "$HEALTH" | grep -q '"llms"'; then
  echo "    WARNING: /api/health missing builders fields — another app may still own port $PORT."
  echo "    Run: lsof -ti :$PORT | xargs kill -9   then ./start.sh again"
fi

wait $UV_PID
