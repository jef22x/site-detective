#!/usr/bin/env bash
# SiteDetective launcher for Linux (developed on CachyOS/Arch).
# Linux counterpart of SiteDetective.bat.
#
#   ./sitedetective.sh          start the app (if needed) and open the UI
#   ./sitedetective.sh stop     stop the server
#   ./sitedetective.sh status   report whether it is serving
#   ./sitedetective.sh update   re-sync dependencies and the Chromium build
#   ./sitedetective.sh logs     follow the server log
#
# Deliberate differences from the .bat, all for the same reason — a launcher
# that hides why it failed is worse than no launcher:
#   * dependencies are synced on first setup or on `update`, never on every
#     launch, so an installed app still starts with no network;
#   * liveness is a request to /api/status, not "is the port occupied", so
#     another process on 8321 cannot be mistaken for SiteDetective;
#   * the server's output goes to a log file, and a failed start prints it;
#   * every failure says what actually failed.
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")" || exit 1

PORT="${SITEDETECTIVE_PORT:-8321}"
URL="http://127.0.0.1:${PORT}"
VENV=".venv"
PY="${VENV}/bin/python"
LOG_DIR="logs"
LOG="${LOG_DIR}/server.log"
PIDFILE="${LOG_DIR}/server.pid"
MIN_PYTHON="3.11"
START_TIMEOUT=45   # seconds to wait for the first successful /api/status

die() { printf '\n\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }
note() { printf '\033[36m==>\033[0m %s\n' "$1"; }

# --- state -----------------------------------------------------------------

# True only when SiteDetective itself answers. A stray process holding the
# port fails this, which is the whole point.
is_serving() {
    curl -fsS --max-time 3 "${URL}/api/status" 2>/dev/null \
        | grep -q 'unread_notifications'
}

server_pid() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null)" || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && printf '%s' "$pid"
}

# --- setup -----------------------------------------------------------------

find_python() {
    # Newest suitable interpreter, so a system default older than the floor
    # does not silently produce a venv that fails later at import time.
    local candidate
    for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

ensure_venv() {
    [ -x "$PY" ] && return 0

    local python
    python="$(find_python)" || die \
"no Python ${MIN_PYTHON}+ found on PATH.
SiteDetective needs Python ${MIN_PYTHON} or newer. On CachyOS/Arch:
    sudo pacman -S python"

    note "First-time setup: creating the virtual environment with $python"
    "$python" -m venv "$VENV" || die \
"could not create the virtual environment in ${VENV}/.
Check that $python has the venv module and that this directory is writable."
    sync_deps
}

sync_deps() {
    # "." installs the runtime only; ".[dev]" adds pytest/httpx/ruff. The
    # launcher never installs the dev extras on its own — a user who only
    # wants the UI has no use for the test tooling.
    local spec="${1:-.}"
    note "Installing dependencies from ${spec}"
    "$PY" -m pip install --quiet --upgrade pip \
        || note "could not upgrade pip; continuing with the version already installed"
    "$PY" -m pip install -e "$spec" || die \
"dependency installation failed (see the pip output above).
If you are offline, connect and re-run:  ./sitedetective.sh update"

    # No install-deps here on purpose: that subcommand only knows Debian
    # package names and does nothing useful on Arch/CachyOS.
    note "Installing the Chromium build Playwright drives"
    "$PY" -m playwright install chromium || die \
"could not install Chromium for Playwright.
Re-run when you have a connection:  ./sitedetective.sh update"
}

preflight() {
    local output
    if ! output="$("$PY" -c 'import app.main' 2>&1)"; then
        printf '%s\n' "$output" >&2
        die \
"the app failed to import — this is a broken install, not a PATH problem.
Try:  ./sitedetective.sh update"
    fi
}

# --- lifecycle -------------------------------------------------------------

start_server() {
    mkdir -p "$LOG_DIR"

    # One launcher at a time, so a double click cannot start two servers and
    # leave the loser dying of a bind error somewhere out of sight.
    if command -v flock >/dev/null 2>&1; then
        exec 9>"${LOG_DIR}/launcher.lock"
        flock -w 60 9 || die "another launcher is still starting up; try again in a moment."
    fi

    is_serving && return 0

    ensure_venv
    preflight

    note "Starting SiteDetective on port ${PORT}"
    : > "$LOG"
    # Single worker: run-slot accounting and live progress live in process
    # memory, so more than one would double-book runs.
    # nohup rather than setsid: setsid only forks when it is not already a
    # process group leader, so $! would sometimes be setsid's pid and not the
    # server's. nohup keeps $! honest and still survives the terminal closing.
    # 9>&- closes the launcher's lock fd in the child. Without it the server
    # inherits the descriptor and holds the flock for its whole life, so every
    # later `start` blocks for the full timeout and then reports a phantom
    # "another launcher is starting up".
    nohup "$PY" -m uvicorn app.main:app \
        --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 9>&- &
    printf '%s' "$!" > "$PIDFILE"

    local waited=0
    while [ "$waited" -lt "$START_TIMEOUT" ]; do
        is_serving && { note "Serving at ${URL}"; return 0; }
        # If the process is already gone the log holds the real reason.
        if ! server_pid >/dev/null; then
            printf '\n--- last 30 lines of %s ---\n' "$LOG" >&2
            tail -n 30 "$LOG" >&2
            die "the server exited during start-up (see the log above)."
        fi
        sleep 1
        waited=$((waited + 1))
    done

    printf '\n--- last 30 lines of %s ---\n' "$LOG" >&2
    tail -n 30 "$LOG" >&2
    die "the server did not answer within ${START_TIMEOUT}s (see the log above)."
}

stop_server() {
    local pid
    if ! pid="$(server_pid)"; then
        rm -f "$PIDFILE"
        if is_serving; then
            die \
"something is serving on ${PORT} but it was not started by this script,
so it will not be killed from here. Find it with:  ss -ltnp | grep ${PORT}"
        fi
        note "Not running."
        return 0
    fi

    note "Stopping SiteDetective (pid ${pid})"
    kill "$pid" 2>/dev/null
    local waited=0
    while [ "$waited" -lt 10 ] && kill -0 "$pid" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        note "Still running after 10s; sending SIGKILL"
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$PIDFILE"
    note "Stopped."
}

open_ui() {
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 &
    else
        note "Open ${URL} in your browser."
    fi
}

# --- commands --------------------------------------------------------------

command -v curl >/dev/null 2>&1 || die \
"curl is required to check whether the app is up. On CachyOS/Arch:
    sudo pacman -S curl"

case "${1:-start}" in
    start)
        start_server
        open_ui
        ;;
    stop)
        stop_server
        ;;
    status)
        if is_serving; then
            note "Serving at ${URL} (pid $(server_pid || printf 'unknown — not started by this script'))"
        else
            note "Not serving on port ${PORT}."
            exit 1
        fi
        ;;
    update)
        shift
        ensure_venv
        # `update --dev` adds pytest/httpx/ruff for working on SiteDetective.
        if [ "${1:-}" = "--dev" ]; then
            sync_deps ".[dev]"
        else
            sync_deps
        fi
        preflight
        note "Up to date."
        ;;
    logs)
        [ -f "$LOG" ] || die "no log yet at ${LOG} — start the app first."
        tail -f "$LOG"
        ;;
    *)
        die "unknown command '${1}'. Use: start | stop | status | update | logs"
        ;;
esac
