import os
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

INVENTORY_LINE_PATTERN = re.compile(r"^(?P<name>.+?)\s+(?P<count>\d+)$")


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


def extract_ticket_inventory(page):
    page_text = page.inner_text("body")
    return parse_ticket_inventory_from_text(page_text)


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


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        return bool(data.get("ok", False))
    except Exception as e:
        print(f"Failed to send Telegram: {e}")
        return False


def check_tickets():
    with sync_playwright() as p:
        browser = None

        try:
            # Launching browser
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking Paylogic...")
            page.goto(URL, wait_until="networkidle", timeout=60000)

            # Wait for the ticket list to render
            page.wait_for_timeout(5000)

            inventory = extract_ticket_inventory(page)
            inventory_available = [
                (name, count) for name, count in inventory if count > 0
            ]

            # Paylogic usually shows a "Select" button or a quantity dropdown when available
            # We check for any button that isn't disabled and isn't a 'Sold Out' label
            available_buttons = page.query_selector_all("button:not([disabled])")

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

            if tickets_found:
                msg = f"<b>🚨 TICKET REVEALED!</b>\n\nNew tickets appear to be available at the link below:\n<a href='{URL}'>Click here to buy!</a>"
                ticket_details = (
                    inventory_available
                    or extract_available_ticket_details(available_buttons)
                )
                detail_msg = build_ticket_detail_message(ticket_details)

                print("Success: Tickets found!")
                if send_telegram(msg):
                    print("Status: Telegram notification sent.")
                else:
                    print("Status: Telegram notification failed.")

                if send_telegram(detail_msg):
                    print("Status: Telegram detail notification sent.")
                else:
                    print("Status: Telegram detail notification failed.")

                if should_send_daily_status():
                    daily_msg = "<b>Daily check:</b> Tickets are currently available. Monitoring is running."
                    if send_telegram(daily_msg):
                        print("Status: Daily check message sent.")
                    else:
                        print("Status: Daily check message failed.")
            else:
                print("Status: All tickets still sold out.")
                if should_send_daily_status():
                    daily_msg = "<b>Daily check:</b> No tickets found today. Monitoring is running."
                    if send_telegram(daily_msg):
                        print("Status: Daily check message sent.")
                    else:
                        print("Status: Daily check message failed.")

        except Exception as e:
            print(f"Error during execution: {e}")
        finally:
            if browser is not None:
                browser.close()


if __name__ == "__main__":
    check_tickets()
