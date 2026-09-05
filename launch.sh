#!/bin/bash
# Run from this script's own directory so code and storage stay together.
DIR="$(cd "$(dirname "$0")" && pwd)"

# Resolve real path (works under WSL: J:\Mara == /mnt/j/Mara).
case "$DIR" in
  /mnt/*) REAL_DIR="$DIR" ;;
  /*) REAL_DIR="$DIR" ;;
  *) REAL_DIR="$PWD/$DIR" ;;
esac
cd "$REAL_DIR"

PORT="${1:-8000}"

# Kill any stale server already squatting the port before starting fresh.
# Multiple strategies so it works on Linux WSL, git-bash, and msys2.
kill_stale() {
  if [ -f "/tmp/mara_pid_${PORT}" ]; then
    OLDPID="$(cat "/tmp/mara_pid_${PORT}")"
    if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
      kill "$OLDPID" 2>/dev/null || true
    fi
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${PORT}/tcp" >/dev/null 2>&1
  fi
  # Python fallback: find and kill whatever holds the port (works on Windows too,
  # where fuser may be absent).
  python3 - "$PORT" <<'PY' 2>/dev/null || true
import sys, socket
port = int(sys.argv[1])
try:
    import psutil
    for c in psutil.net_connections(kind='inet'):
        if c.laddr and c.laddr.port == port and c.pid:
            try:
                psutil.Process(c.pid).kill()
            except Exception:
                pass
except Exception:
    pass
PY
}
kill_stale
sleep 1

set -m
setsid nohup env ELOISE_STORAGE_DIR="$REAL_DIR/storage" python3 -m uvicorn main:app --port "$PORT" </dev/null >"/tmp/mara_srv_${PORT}.log" 2>&1 &
echo "$!" > "/tmp/mara_pid_${PORT}"
echo "launched $PORT from $REAL_DIR"
exit 0