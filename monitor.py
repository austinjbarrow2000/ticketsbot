import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright
import requests

# --- CONFIGURATION ---
URL = "https://resale.paylogic.com/4f4cb390559b41f49892d0a3214d067d/"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DAILY_STATUS_ENABLED = os.getenv("DAILY_STATUS_ENABLED", "1")
DAILY_STATUS_HOUR = int(os.getenv("DAILY_STATUS_HOUR", "20"))
DAILY_STATUS_TZ = os.getenv("DAILY_STATUS_TZ", "America/New_York")
CHECK_MAX_RETRIES = int(os.getenv("CHECK_MAX_RETRIES", "3"))
CHECK_INITIAL_BACKOFF_SECONDS = float(os.getenv("CHECK_INITIAL_BACKOFF_SECONDS", "2"))
TELEGRAM_MAX_RETRIES = int(os.getenv("TELEGRAM_MAX_RETRIES", "3"))
TELEGRAM_INITIAL_BACKOFF_SECONDS = float(
    os.getenv("TELEGRAM_INITIAL_BACKOFF_SECONDS", "1")
)
ALERT_DEDUPE_MINUTES = int(os.getenv("ALERT_DEDUPE_MINUTES", "30"))

INVENTORY_LINE_PATTERN = re.compile(r"^(?P<name>.+?)\s+(?P<count>\d+)$")

_last_ticket_alert_signature = None
_last_ticket_alert_sent_at = 0.0
_last_daily_status_date = None


def parse_ticket_inventory_from_text(page_text):
    details = []
    seen = set()
    lines = [" ".join(raw_line.split()) for raw_line in page_text.splitlines()]
    lines = [line for line in lines if line]

    def add_detail(name, count):
        lowered = name.lower()
        if "ticket" not in lowered and "locker" not in lowered:
            return
        if len(name) < 3:
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        details.append((name, count))

    for index, line in enumerate(lines):
        # Format 1: "Regular Entrance Ticket 2"
        inline_match = INVENTORY_LINE_PATTERN.match(line)
        if inline_match:
            add_detail(
                inline_match.group("name").strip(" -"), int(inline_match.group("count"))
            )
            continue

        # Format 2: name on one line, count on the next line.
        lowered = line.lower()
        if ("ticket" in lowered or "locker" in lowered) and index + 1 < len(lines):
            next_line = lines[index + 1]
            if next_line.isdigit():
                add_detail(line.strip(" -"), int(next_line))

    return details


def parse_market_summary_from_text(page_text):
    available = None
    sold = None

    lines = [" ".join(raw_line.split()) for raw_line in page_text.splitlines()]
    lines = [line for line in lines if line]

    for index, line in enumerate(lines):
        lowered = line.lower()

        if lowered == "available" and index > 0 and lines[index - 1].isdigit():
            available = int(lines[index - 1])
            continue

        if lowered == "sold" and index > 0 and lines[index - 1].isdigit():
            sold = int(lines[index - 1])
            continue

        inline_available = re.match(r"^(\d+)\s+available$", lowered)
        if inline_available:
            available = int(inline_available.group(1))
            continue

        inline_sold = re.match(r"^(\d+)\s+sold$", lowered)
        if inline_sold:
            sold = int(inline_sold.group(1))

    return {"available": available, "sold": sold}


def extract_ticket_inventory(page):
    page_text = page.inner_text("body")
    return parse_ticket_inventory_from_text(page_text)


def extract_market_summary(page):
    page_text = page.inner_text("body")
    return parse_market_summary_from_text(page_text)


def extract_available_ticket_details(buttons):
    details = []
    seen = set()
    for btn in buttons:
        raw_text = btn.inner_text() or ""
        text = " ".join(raw_text.split())
        if not text:
            continue

        cleaned = re.sub(r"\b(select|add|choose)\b", "", text, flags=re.IGNORECASE)
        cleaned = " ".join(cleaned.split())
        match = re.match(r"^(?P<name>.+?)\s+(?P<count>\d+)$", cleaned)
        if not match:
            continue

        name = match.group("name").strip(" -")
        count = int(match.group("count"))

        if count <= 0:
            continue
        if len(name) < 3:
            continue

        key = (name.lower(), count)
        if key in seen:
            continue
        seen.add(key)
        details.append((name, count))

    return details


def build_ticket_detail_message(ticket_details):
    if ticket_details:
        detail_lines = [
            f"- {name}: {count} available" for name, count in ticket_details
        ]
        return "<b>Ticket details:</b>\n" + "\n".join(detail_lines)
    return "<b>Ticket details:</b>\nCould not parse ticket names/counts from the page, but at least one ticket action is available."


def should_send_daily_status(now=None):
    if DAILY_STATUS_ENABLED != "1":
        return False

    if now is None:
        now = datetime.now(ZoneInfo(DAILY_STATUS_TZ))

    return now.hour == DAILY_STATUS_HOUR and now.minute < 5


def should_send_daily_status_once_per_day(now=None):
    global _last_daily_status_date

    if now is None:
        now = datetime.now(ZoneInfo(DAILY_STATUS_TZ))

    if not should_send_daily_status(now=now):
        return False

    today = now.date().isoformat()
    if _last_daily_status_date == today:
        return False

    _last_daily_status_date = today
    return True


def should_send_ticket_alert(signature, now_ts=None):
    global _last_ticket_alert_signature
    global _last_ticket_alert_sent_at

    if now_ts is None:
        now_ts = time.time()

    dedupe_window = max(0, ALERT_DEDUPE_MINUTES) * 60
    if (
        _last_ticket_alert_signature == signature
        and now_ts - _last_ticket_alert_sent_at < dedupe_window
    ):
        return False

    _last_ticket_alert_signature = signature
    _last_ticket_alert_sent_at = now_ts
    return True


def build_ticket_signature(ticket_details):
    if not ticket_details:
        return "tickets-found-unknown-details"

    normalized = sorted(
        (name.lower().strip(), int(count)) for name, count in ticket_details
    )
    return "|".join(f"{name}:{count}" for name, count in normalized)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    attempts = max(1, TELEGRAM_MAX_RETRIES)

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            return bool(data.get("ok", False))
        except Exception as e:
            if attempt == attempts:
                print(f"Failed to send Telegram: {e}")
                return False

            backoff = TELEGRAM_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            jitter = random.uniform(0, 0.5)
            wait_seconds = backoff + jitter
            print(
                f"Telegram send failed (attempt {attempt}/{attempts}): {e}. Retrying in {wait_seconds:.1f}s..."
            )
            time.sleep(wait_seconds)


def check_tickets(verbose=True):
    result = {
        "checked_at": datetime.now(ZoneInfo(DAILY_STATUS_TZ)).isoformat(
            timespec="seconds"
        ),
        "success": False,
        "error": None,
        "tickets_found": False,
        "inventory": [],
        "inventory_available": [],
        "summary": {"available": None, "sold": None},
        "alerts": {
            "ticket_alert_sent": False,
            "detail_alert_sent": False,
            "daily_status_sent": False,
            "duplicate_suppressed": False,
        },
    }

    with sync_playwright() as p:
        browser = None

        try:
            # Launching browser
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            if verbose:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking Paylogic...")
            last_error = None
            inventory = []
            inventory_available = []
            available_buttons = []
            summary = {"available": None, "sold": None}
            attempts = max(1, CHECK_MAX_RETRIES)

            for attempt in range(1, attempts + 1):
                try:
                    page.goto(URL, wait_until="networkidle", timeout=60000)

                    # Wait for the ticket list to render
                    page.wait_for_timeout(5000)

                    page_text = page.inner_text("body")
                    inventory = parse_ticket_inventory_from_text(page_text)
                    summary = parse_market_summary_from_text(page_text)
                    inventory_available = [
                        (name, count) for name, count in inventory if count > 0
                    ]

                    # Paylogic usually shows a "Select" button or a quantity dropdown when available
                    # We check for any button that isn't disabled and isn't a 'Sold Out' label
                    available_buttons = page.query_selector_all(
                        "button:not([disabled])"
                    )
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    if attempt == attempts:
                        raise

                    backoff = CHECK_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    jitter = random.uniform(0, 1.0)
                    wait_seconds = backoff + jitter
                    if verbose:
                        print(
                            f"Check attempt {attempt}/{attempts} failed: {e}. Retrying in {wait_seconds:.1f}s..."
                        )
                    time.sleep(wait_seconds)

            if last_error is not None:
                raise last_error

            # Filter out common UI buttons like "Language" or "Back"
            # Most ticket buttons contain "Select", "Add", or "Choose"
            tickets_found = False
            for btn in available_buttons:
                text = btn.inner_text().lower()
                if "select" in text or "add" in text or "choose" in text:
                    tickets_found = True
                    break

            if inventory_available:
                tickets_found = True

            result["success"] = True
            result["tickets_found"] = tickets_found
            result["inventory"] = inventory
            result["inventory_available"] = inventory_available
            result["summary"] = summary

            if tickets_found:
                msg = f"<b>🚨 TICKET REVEALED!</b>\n\nNew tickets appear to be available at the link below:\n<a href='{URL}'>Click here to buy!</a>"
                ticket_details = (
                    inventory_available
                    or extract_available_ticket_details(available_buttons)
                )
                detail_msg = build_ticket_detail_message(ticket_details)
                ticket_signature = build_ticket_signature(ticket_details)

                if verbose:
                    print("Success: Tickets found!")
                if should_send_ticket_alert(ticket_signature):
                    if send_telegram(msg):
                        result["alerts"]["ticket_alert_sent"] = True
                        if verbose:
                            print("Status: Telegram notification sent.")
                    else:
                        if verbose:
                            print("Status: Telegram notification failed.")

                    if send_telegram(detail_msg):
                        result["alerts"]["detail_alert_sent"] = True
                        if verbose:
                            print("Status: Telegram detail notification sent.")
                    else:
                        if verbose:
                            print("Status: Telegram detail notification failed.")
                else:
                    result["alerts"]["duplicate_suppressed"] = True
                    if verbose:
                        print("Status: Duplicate ticket alert suppressed.")

                if should_send_daily_status_once_per_day():
                    daily_msg = "<b>Daily check:</b> Tickets are currently available. Monitoring is running."
                    if send_telegram(daily_msg):
                        result["alerts"]["daily_status_sent"] = True
                        if verbose:
                            print("Status: Daily check message sent.")
                    else:
                        if verbose:
                            print("Status: Daily check message failed.")
            else:
                if verbose:
                    print("Status: All tickets still sold out.")
                if should_send_daily_status_once_per_day():
                    daily_msg = "<b>Daily check:</b> No tickets found today. Monitoring is running."
                    if send_telegram(daily_msg):
                        result["alerts"]["daily_status_sent"] = True
                        if verbose:
                            print("Status: Daily check message sent.")
                    else:
                        if verbose:
                            print("Status: Daily check message failed.")

        except Exception as e:
            result["error"] = str(e)
            if verbose:
                print(f"Error during execution: {e}")
        finally:
            if browser is not None:
                browser.close()

    return result


if __name__ == "__main__":
    check_tickets()
