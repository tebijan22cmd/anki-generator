#!/bin/bash
# Launcher: finds/creates the venv, frees or picks a port, starts the server, opens the browser.
cd "$(dirname "$0")" || exit 1
ROOT="$PWD"
PORT="${PORT:-8000}"
PY=".venv/bin/python"

# 1. Environment
if [ ! -x "$PY" ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv || { echo "Could not create .venv (is python3 installed?)"; exit 1; }
fi
if ! "$PY" -c "import flask, genanki, pypdf, docx, pptx, PIL" >/dev/null 2>&1; then
  echo "Installing dependencies..."
  "$PY" -m pip install -q -r requirements.txt || { echo "pip install failed"; exit 1; }
fi

is_ours() { curl -fs --max-time 1 "http://127.0.0.1:$1/api/health" 2>/dev/null | grep -q '"anki-generator"'; }
listener() { lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1; }

# 2. Port handling
for _ in $(seq 1 20); do
  pid="$(listener "$PORT")"
  [ -z "$pid" ] && break
  if is_ours "$PORT"; then
    echo "Anki Generator already running at http://127.0.0.1:$PORT"
    open "http://127.0.0.1:$PORT"
    exit 0
  fi
  # Stale copy of this project (old code, no health endpoint)? Replace it.
  if [ "$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" = "$ROOT" ]; then
    echo "Stopping stale server (pid $pid) on port $PORT"
    kill "$pid"; sleep 1
    [ -n "$(listener "$PORT")" ] && kill -9 "$pid" 2>/dev/null
    continue
  fi
  echo "Port $PORT is used by another program; trying $((PORT + 1))"
  PORT=$((PORT + 1))
done
export PORT
URL="http://127.0.0.1:$PORT"

# 3. Open the browser only once the server really answers
(
  for _ in $(seq 1 75); do
    if is_ours "$PORT"; then open "$URL"; exit 0; fi
    sleep 0.2
  done
  echo "Server did not become ready on $URL"
) &

echo "Starting Anki Generator on $URL (Ctrl+C to stop)"
exec "$PY" app.py
