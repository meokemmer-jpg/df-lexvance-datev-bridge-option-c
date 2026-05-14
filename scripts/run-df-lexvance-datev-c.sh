#!/bin/bash
# DF-LEXVANCE-DATEV-BRIDGE-OPTION-C Wrapper [CRUX-MK]
# K16 Concurrent-Spawn-Mutex (lock_stale_age=1h per CRIT-W7-3 + PID-Liveness per EF38)
# Pattern aus rules/df-akzeptanz-kriterien.md K16

set -euo pipefail

LOCK_DIR="${DF_DATEV_LOCK_DIR:-/tmp/df-lexvance-datev-bridge-option-c.lock}"
LOCK_AGE_LIMIT_S="${DF_DATEV_LOCK_AGE_LIMIT_S:-3600}"  # 1h Default (CRIT-W7-3)

# Stale-Lock Auto-Claim mit PID-Liveness-Check (EF38)
if [ -d "$LOCK_DIR" ]; then
  LOCK_MTIME=$(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  LOCK_AGE_S=$(( NOW - LOCK_MTIME ))
  PID_FILE="$LOCK_DIR/pid"
  PID_LIVE=0
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo 0)
    if [ "$OLD_PID" -gt 0 ] && kill -0 "$OLD_PID" 2>/dev/null; then
      PID_LIVE=1
    fi
  fi

  if [ "$PID_LIVE" -eq 0 ]; then
    echo "[K16] PID nicht alive (PID=$(cat "$PID_FILE" 2>/dev/null || echo "?"), age=${LOCK_AGE_S}s) -> auto-claim" >&2
    rm -rf "$LOCK_DIR"
  elif [ "$LOCK_AGE_S" -gt "$LOCK_AGE_LIMIT_S" ]; then
    echo "[K16] Stale lock (age=${LOCK_AGE_S}s, PID alive but exceeded 1h) -> SKIP, alert needed" >&2
    exit 4
  else
    echo "[K16-VETO] Concurrent DF-LEXVANCE-DATEV-C instance detected (PID=$OLD_PID, age=${LOCK_AGE_S}s)" >&2
    exit 3
  fi
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[K16-VETO] mkdir-Lock-Acquire fehlgeschlagen (Race-Loser)" >&2
  exit 3
fi

echo "$$" > "$LOCK_DIR/pid"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$LOCK_DIR/started_at"

trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

PYTHON="${DF_DATEV_PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Aktiviere venv falls vorhanden (K15 python_env_pinned)
if [ -f ".venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  source ".venv/bin/activate"
fi

exec "$PYTHON" -m src.engine "$@"
