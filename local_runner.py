import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from monitor import check_tickets, send_telegram


WATCH_GITHUB_SCHEDULE = os.getenv("WATCH_GITHUB_SCHEDULE", "1") == "1"
WATCH_GITHUB_REPO = os.getenv("WATCH_GITHUB_REPO", "austinjbarrow2000/ticketsbot")
WATCH_GITHUB_WORKFLOW = os.getenv("WATCH_GITHUB_WORKFLOW", "monitor.yml")
WATCH_GITHUB_MAX_DELAY_MINUTES = int(os.getenv("WATCH_GITHUB_MAX_DELAY_MINUTES", "12"))


def sleep_to_next_run(interval_seconds):
    now = time.time()
    next_tick = ((int(now) // interval_seconds) + 1) * interval_seconds
    sleep_for = max(0, next_tick - now)
    time.sleep(sleep_for)


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

        if age_minutes > WATCH_GITHUB_MAX_DELAY_MINUTES:
            if not state["schedule_alert_open"]:
                message = (
                    "<b>GitHub schedule watchdog:</b> No scheduled run detected within "
                    f"{WATCH_GITHUB_MAX_DELAY_MINUTES} minutes. "
                    f"Last scheduled run was {int(age_minutes)} minutes ago."
                )
                if send_telegram(message):
                    print("Schedule watchdog alert sent.", flush=True)
                state["schedule_alert_open"] = True
        elif state["schedule_alert_open"]:
            message = (
                "<b>GitHub schedule watchdog:</b> Scheduled runs are healthy again."
            )
            if send_telegram(message):
                print("Schedule watchdog recovery sent.", flush=True)
            state["schedule_alert_open"] = False
    except Exception as exc:
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] Watchdog check failed: {exc}",
            flush=True,
        )


def run_loop(interval_seconds):
    state = {"schedule_alert_open": False}

    print(
        f"[{datetime.now().isoformat(timespec='seconds')}] Local monitor started. "
        f"Running every {interval_seconds} seconds. Press Ctrl+C to stop.",
        flush=True,
    )

    if WATCH_GITHUB_SCHEDULE:
        print(
            "GitHub schedule watchdog enabled for "
            f"{WATCH_GITHUB_REPO} / {WATCH_GITHUB_WORKFLOW}. "
            f"Alert threshold: {WATCH_GITHUB_MAX_DELAY_MINUTES} minutes.",
            flush=True,
        )

    while True:
        start = time.time()
        started_at = datetime.now().isoformat(timespec="seconds")
        print(f"[{started_at}] Starting monitor check...", flush=True)

        try:
            check_tickets()
        except Exception as exc:
            # Keep the local runner alive even if one check fails.
            print(
                f"[{datetime.now().isoformat(timespec='seconds')}] Runner error: {exc}",
                flush=True,
            )

        elapsed = time.time() - start
        print(
            f"[{datetime.now().isoformat(timespec='seconds')}] Check finished in {elapsed:.1f}s.",
            flush=True,
        )

        check_github_schedule_health(state)

        if interval_seconds <= 1:
            time.sleep(1)
        else:
            sleep_to_next_run(interval_seconds)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run monitor checks continuously on a fixed interval."
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        help="How often to run checks (default: 60).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.interval_seconds < 1:
        print("interval-seconds must be >= 1", file=sys.stderr)
        sys.exit(2)

    try:
        run_loop(args.interval_seconds)
    except KeyboardInterrupt:
        print(
            f"\n[{datetime.now().isoformat(timespec='seconds')}] Local monitor stopped.",
            flush=True,
        )


if __name__ == "__main__":
    main()
