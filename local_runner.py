import argparse
import atexit
import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from monitor import check_tickets, send_telegram


WATCH_GITHUB_SCHEDULE = os.getenv("WATCH_GITHUB_SCHEDULE", "1") == "1"
WATCH_GITHUB_REPO = os.getenv("WATCH_GITHUB_REPO", "austinjbarrow2000/ticketsbot")
WATCH_GITHUB_WORKFLOW = os.getenv("WATCH_GITHUB_WORKFLOW", "monitor.yml")
WATCH_GITHUB_MAX_DELAY_MINUTES = int(os.getenv("WATCH_GITHUB_MAX_DELAY_MINUTES", "12"))
STATE_FILE = os.getenv("LOCAL_MONITOR_STATE_FILE", "local_monitor_state.json")
MAX_HISTORY = int(os.getenv("LOCAL_MONITOR_MAX_HISTORY", "500"))
GIT_STATE_SYNC_ENABLED = os.getenv("GIT_STATE_SYNC_ENABLED", "1") == "1"

_runtime = {"state": None, "finalized": False}


def sleep_with_jitter(interval_seconds, jitter_seconds):
    jitter = 0
    if jitter_seconds > 0:
        jitter = random.randint(-jitter_seconds, jitter_seconds)

    sleep_for = max(1, interval_seconds + jitter)
    time.sleep(sleep_for)


def format_timedelta(delta_seconds):
    if delta_seconds is None:
        return "n/a"

    total = int(max(0, delta_seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "total_checks": 0,
            "successful_checks": 0,
            "failed_checks": 0,
            "last_result": None,
            "history": [],
            "last_inventory": [],
            "last_inventory_change_at": None,
            "last_available_change_at": None,
            "last_ticket_seen_at": None,
            "watchdog": {
                "schedule_alert_open": False,
                "last_schedule_run_at": None,
                "last_schedule_age_minutes": None,
                "last_schedule_error": None,
            },
            "git_sync": {
                "last_start_pull": None,
                "last_finish_push": None,
                "last_error": None,
            },
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
            data.setdefault("history", [])
            data.setdefault("watchdog", {})
            data.setdefault("git_sync", {})
            data["watchdog"].setdefault("schedule_alert_open", False)
            data["watchdog"].setdefault("last_schedule_run_at", None)
            data["watchdog"].setdefault("last_schedule_age_minutes", None)
            data["watchdog"].setdefault("last_schedule_error", None)
            data["git_sync"].setdefault("last_start_pull", None)
            data["git_sync"].setdefault("last_finish_push", None)
            data["git_sync"].setdefault("last_error", None)
            return data
    except Exception:
        return {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "total_checks": 0,
            "successful_checks": 0,
            "failed_checks": 0,
            "last_result": None,
            "history": [],
            "last_inventory": [],
            "last_inventory_change_at": None,
            "last_available_change_at": None,
            "last_ticket_seen_at": None,
            "watchdog": {
                "schedule_alert_open": False,
                "last_schedule_run_at": None,
                "last_schedule_age_minutes": None,
                "last_schedule_error": None,
            },
            "git_sync": {
                "last_start_pull": None,
                "last_finish_push": None,
                "last_error": None,
            },
        }


def save_state(state):
    state["history"] = state["history"][-MAX_HISTORY:]
    with open(STATE_FILE, "w", encoding="utf-8") as file_obj:
        json.dump(state, file_obj, indent=2)


def git_run(args):
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def ensure_git_repo():
    result = git_run(["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def sync_state_from_git_on_start(state):
    if not GIT_STATE_SYNC_ENABLED:
        return
    if not ensure_git_repo():
        state["git_sync"]["last_error"] = "Not inside a git repository"
        return

    pull = git_run(["pull", "--rebase", "--autostash"])
    if pull.returncode != 0:
        err = (pull.stderr or pull.stdout).strip()
        state["git_sync"]["last_error"] = f"Startup pull failed: {err}"
        return False
    else:
        state["git_sync"]["last_start_pull"] = datetime.now().isoformat(
            timespec="seconds"
        )
        state["git_sync"]["last_error"] = None
        return True


def sync_state_to_git_on_finish(state, reason):
    if not GIT_STATE_SYNC_ENABLED:
        return
    if not ensure_git_repo():
        state["git_sync"]["last_error"] = "Not inside a git repository"
        return

    add = git_run(["add", "--", STATE_FILE])
    if add.returncode != 0:
        err = (add.stderr or add.stdout).strip()
        state["git_sync"]["last_error"] = f"git add failed: {err}"
        return

    diff = git_run(["diff", "--cached", "--quiet", "--", STATE_FILE])
    if diff.returncode == 0:
        state["git_sync"]["last_finish_push"] = datetime.now().isoformat(
            timespec="seconds"
        )
        state["git_sync"]["last_error"] = None
        return

    commit_message = (
        f"chore: update local monitor state ({reason}) "
        f"{datetime.now().isoformat(timespec='seconds')}"
    )
    commit = git_run(["commit", "-m", commit_message, "--", STATE_FILE])
    if commit.returncode != 0:
        err = (commit.stderr or commit.stdout).strip()
        state["git_sync"]["last_error"] = f"git commit failed: {err}"
        return

    pull = git_run(["pull", "--rebase", "--autostash"])
    if pull.returncode != 0:
        err = (pull.stderr or pull.stdout).strip()
        state["git_sync"]["last_error"] = f"Finish pull failed: {err}"
        return

    push = git_run(["push"])
    if push.returncode != 0:
        err = (push.stderr or push.stdout).strip()
        state["git_sync"]["last_error"] = f"git push failed: {err}"
        return

    state["git_sync"]["last_finish_push"] = datetime.now().isoformat(timespec="seconds")
    state["git_sync"]["last_error"] = None


def finalize_and_sync(reason):
    if _runtime["finalized"]:
        return
    _runtime["finalized"] = True

    state = _runtime.get("state")
    if state is None:
        return

    try:
        save_state(state)
        sync_state_to_git_on_finish(state, reason)
        save_state(state)
    except Exception as exc:
        state.setdefault("git_sync", {})
        state["git_sync"]["last_error"] = f"Finalization error: {exc}"
        save_state(state)


def _signal_handler(signum, _frame):
    finalize_and_sync(f"signal-{signum}")
    raise SystemExit(0)


def update_state_with_result(state, result, duration_seconds):
    checked_at = result.get("checked_at", datetime.now().isoformat(timespec="seconds"))
    inventory = result.get("inventory", [])
    inventory_available = result.get("inventory_available", [])

    state["total_checks"] = state.get("total_checks", 0) + 1
    if result.get("success"):
        state["successful_checks"] = state.get("successful_checks", 0) + 1
    else:
        state["failed_checks"] = state.get("failed_checks", 0) + 1

    history_row = {
        "checked_at": checked_at,
        "success": bool(result.get("success")),
        "duration_seconds": round(duration_seconds, 2),
        "tickets_found": bool(result.get("tickets_found")),
        "available_count": int(sum(count for _, count in inventory_available)),
        "error": result.get("error"),
    }
    state["history"].append(history_row)

    previous_inventory = state.get("last_inventory", [])
    if previous_inventory != inventory:
        state["last_inventory_change_at"] = checked_at

    previous_available_total = int(
        sum(
            count
            for _, count in previous_inventory
            if isinstance(count, int) and count > 0
        )
    )
    current_available_total = int(sum(count for _, count in inventory_available))
    if previous_available_total != current_available_total:
        state["last_available_change_at"] = checked_at

    if current_available_total > 0:
        state["last_ticket_seen_at"] = checked_at

    state["last_inventory"] = inventory
    state["last_result"] = {
        "checked_at": checked_at,
        "success": bool(result.get("success")),
        "error": result.get("error"),
        "tickets_found": bool(result.get("tickets_found")),
        "summary": result.get("summary", {}),
        "inventory": inventory,
        "inventory_available": inventory_available,
        "alerts": result.get("alerts", {}),
        "duration_seconds": round(duration_seconds, 2),
    }


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _avg_duration(state):
    durations = [
        item.get("duration_seconds", 0)
        for item in state.get("history", [])
        if item.get("duration_seconds")
    ]
    if not durations:
        return None
    return sum(durations) / len(durations)


def render_dashboard(state, interval_seconds, jitter_seconds):
    now = datetime.now()
    last = state.get("last_result") or {}
    summary = last.get("summary", {})
    inventory = last.get("inventory", [])
    alerts = last.get("alerts", {})

    success_count = state.get("successful_checks", 0)
    total_count = state.get("total_checks", 0)
    success_rate = (100.0 * success_count / total_count) if total_count else 0.0
    avg_duration = _avg_duration(state)

    inventory_change_at = _parse_iso(state.get("last_inventory_change_at"))
    available_change_at = _parse_iso(state.get("last_available_change_at"))
    last_ticket_seen_at = _parse_iso(state.get("last_ticket_seen_at"))

    inventory_change_age = (
        (now - inventory_change_at).total_seconds() if inventory_change_at else None
    )
    available_change_age = (
        (now - available_change_at).total_seconds() if available_change_at else None
    )
    last_ticket_seen_age = (
        (now - last_ticket_seen_at).total_seconds() if last_ticket_seen_at else None
    )

    watchdog = state.get("watchdog", {})
    git_sync = state.get("git_sync", {})

    lines = []
    lines.append("\033[2J\033[H")
    lines.append("Ticket Monitor Dashboard")
    lines.append("=" * 80)
    lines.append(f"Now: {now.isoformat(timespec='seconds')}")
    lines.append(
        f"Cadence: every {interval_seconds}s with +/- {jitter_seconds}s jitter | State file: {STATE_FILE}"
    )
    lines.append("")

    lines.append("State Sync")
    lines.append("-" * 80)
    lines.append(f"Git sync enabled: {GIT_STATE_SYNC_ENABLED}")
    lines.append(f"Last startup pull: {git_sync.get('last_start_pull')}")
    lines.append(f"Last finish push: {git_sync.get('last_finish_push')}")
    if git_sync.get("last_error"):
        lines.append(f"Last sync error: {git_sync.get('last_error')}")
    lines.append("")

    status = "OK" if last.get("success") else "ERROR"
    lines.append("Latest Check")
    lines.append("-" * 80)
    lines.append(
        f"Status: {status} | Checked at: {last.get('checked_at', 'n/a')} | Duration: {last.get('duration_seconds', 'n/a')}s"
    )
    lines.append(
        f"Tickets found: {last.get('tickets_found', False)} | Event available: {summary.get('available')} | Event sold: {summary.get('sold')}"
    )
    if last.get("error"):
        lines.append(f"Error: {last.get('error')}")
    lines.append(
        "Alerts: "
        f"ticket={alerts.get('ticket_alert_sent', False)} "
        f"detail={alerts.get('detail_alert_sent', False)} "
        f"daily={alerts.get('daily_status_sent', False)} "
        f"suppressed={alerts.get('duplicate_suppressed', False)}"
    )
    lines.append("")

    lines.append("Ticket Inventory")
    lines.append("-" * 80)
    if inventory:
        for name, count in inventory:
            status_text = "AVAILABLE" if int(count) > 0 else "SOLD OUT"
            lines.append(f"{count:>4} | {status_text:<9} | {name}")
    else:
        lines.append("No inventory captured yet.")
    lines.append("")

    lines.append("Realtime Stats")
    lines.append("-" * 80)
    lines.append(
        f"Checks: total={total_count} success={success_count} failed={state.get('failed_checks', 0)} success_rate={success_rate:.1f}%"
    )
    lines.append(
        f"Average check duration: {avg_duration:.2f}s"
        if avg_duration is not None
        else "Average check duration: n/a"
    )
    lines.append(
        f"Time since any inventory change: {format_timedelta(inventory_change_age)}"
    )
    lines.append(
        f"Time since available-count change: {format_timedelta(available_change_age)}"
    )
    lines.append(
        f"Time since any ticket was available: {format_timedelta(last_ticket_seen_age)}"
    )
    lines.append("")

    lines.append("GitHub Schedule Watchdog")
    lines.append("-" * 80)
    lines.append(
        f"Enabled: {WATCH_GITHUB_SCHEDULE} | Alert open: {watchdog.get('schedule_alert_open', False)}"
    )
    lines.append(
        f"Last scheduled run: {watchdog.get('last_schedule_run_at')} | Age: {watchdog.get('last_schedule_age_minutes')} minutes"
    )
    if watchdog.get("last_schedule_error"):
        lines.append(f"Last watchdog error: {watchdog.get('last_schedule_error')}")
    lines.append("")

    lines.append("Recent Checks")
    lines.append("-" * 80)
    for item in state.get("history", [])[-8:]:
        marker = "OK" if item.get("success") else "ERR"
        lines.append(
            f"{item.get('checked_at')} | {marker:<3} | {item.get('duration_seconds', 0):>5}s | "
            f"tickets_found={item.get('tickets_found')} | available_total={item.get('available_count')}"
        )

    output = "\n".join(lines)
    print(output, end="\n", flush=True)


def get_latest_scheduled_run_timestamp():
    command = [
        "gh",
        "api",
        f"repos/{WATCH_GITHUB_REPO}/actions/workflows/{WATCH_GITHUB_WORKFLOW}/runs?event=schedule&per_page=1",
        "--jq",
        ".workflow_runs[0].created_at",
    ]
    env = os.environ.copy()
    env["GH_PAGER"] = "cat"
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, env=env
    )

    value = result.stdout.strip().strip('"')
    if not value or value == "null":
        raise ValueError("No scheduled runs found")

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def check_github_schedule_health(state):
    if not WATCH_GITHUB_SCHEDULE:
        return

    try:
        latest_run = get_latest_scheduled_run_timestamp()
        now_utc = datetime.now(timezone.utc)
        age_minutes = (now_utc - latest_run).total_seconds() / 60
        state["watchdog"]["last_schedule_run_at"] = latest_run.isoformat(
            timespec="seconds"
        )
        state["watchdog"]["last_schedule_age_minutes"] = int(age_minutes)
        state["watchdog"]["last_schedule_error"] = None

        if age_minutes > WATCH_GITHUB_MAX_DELAY_MINUTES:
            if not state["watchdog"]["schedule_alert_open"]:
                message = (
                    "<b>GitHub schedule watchdog:</b> No scheduled run detected within "
                    f"{WATCH_GITHUB_MAX_DELAY_MINUTES} minutes. "
                    f"Last scheduled run was {int(age_minutes)} minutes ago."
                )
                send_telegram(message)
                state["watchdog"]["schedule_alert_open"] = True
        elif state["watchdog"]["schedule_alert_open"]:
            message = (
                "<b>GitHub schedule watchdog:</b> Scheduled runs are healthy again."
            )
            send_telegram(message)
            state["watchdog"]["schedule_alert_open"] = False
    except Exception as exc:
        state["watchdog"]["last_schedule_error"] = str(exc)


def run_loop(interval_seconds, jitter_seconds):
    state = load_state()
    _runtime["state"] = state
    state.setdefault("watchdog", {})
    state["watchdog"].setdefault("schedule_alert_open", False)
    state["watchdog"].setdefault("last_schedule_run_at", None)
    state["watchdog"].setdefault("last_schedule_age_minutes", None)
    state["watchdog"].setdefault("last_schedule_error", None)
    state.setdefault("git_sync", {})
    state["git_sync"].setdefault("last_start_pull", None)
    state["git_sync"].setdefault("last_finish_push", None)
    state["git_sync"].setdefault("last_error", None)

    sync_state_from_git_on_start(state)
    if GIT_STATE_SYNC_ENABLED:
        reloaded = load_state()
        state.update(reloaded)
    state.setdefault("watchdog", {})
    state["watchdog"].setdefault("schedule_alert_open", False)
    state["watchdog"].setdefault("last_schedule_run_at", None)
    state["watchdog"].setdefault("last_schedule_age_minutes", None)
    state["watchdog"].setdefault("last_schedule_error", None)
    state.setdefault("git_sync", {})
    state["git_sync"].setdefault("last_start_pull", None)
    state["git_sync"].setdefault("last_finish_push", None)
    state["git_sync"].setdefault("last_error", None)
    save_state(state)

    render_dashboard(state, interval_seconds, jitter_seconds)

    while True:
        start = time.time()

        try:
            result = check_tickets(verbose=False)
        except Exception as exc:
            result = {
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "success": False,
                "error": str(exc),
                "tickets_found": False,
                "inventory": [],
                "inventory_available": [],
                "summary": {"available": None, "sold": None},
                "alerts": {},
            }

        elapsed = time.time() - start
        update_state_with_result(state, result, elapsed)

        check_github_schedule_health(state)
        save_state(state)
        render_dashboard(state, interval_seconds, jitter_seconds)

        if interval_seconds <= 1:
            time.sleep(1)
        else:
            sleep_with_jitter(interval_seconds, jitter_seconds)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run monitor checks continuously on a fixed interval."
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=150,
        help="Base interval between checks (default: 150).",
    )
    parser.add_argument(
        "--jitter-seconds",
        type=int,
        default=30,
        help="Random jitter added/subtracted each cycle (default: 30).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.interval_seconds < 1:
        print("interval-seconds must be >= 1", file=sys.stderr)
        sys.exit(2)

    if args.jitter_seconds < 0:
        print("jitter-seconds must be >= 0", file=sys.stderr)
        sys.exit(2)

    atexit.register(lambda: finalize_and_sync("atexit"))
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        run_loop(args.interval_seconds, args.jitter_seconds)
    except KeyboardInterrupt:
        finalize_and_sync("keyboard-interrupt")
        print("\033[2J\033[H", end="", flush=True)
        print(
            f"\n[{datetime.now().isoformat(timespec='seconds')}] Local monitor stopped.",
            flush=True,
        )
    except Exception:
        finalize_and_sync("exception")
        raise


if __name__ == "__main__":
    main()
