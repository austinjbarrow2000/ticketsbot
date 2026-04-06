import os
import time
from playwright.sync_api import sync_playwright
import requests

# --- CONFIGURATION ---
URL = "https://resale.paylogic.com/4f4cb390559b41f49892d0a3214d067d/"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send Telegram: {e}")


def check_tickets():
    with sync_playwright() as p:
        # Launching browser
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking Paylogic...")
            page.goto(URL, wait_until="networkidle", timeout=60000)

            # Wait for the ticket list to render
            page.wait_for_timeout(5000)

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

            if tickets_found:
                msg = f"<b>🚨 TICKET REVEALED!</b>\n\nNew tickets appear to be available at the link below:\n<a href='{URL}'>Click here to buy!</a>"
                print("Success: Tickets found!")
                send_telegram(msg)
            else:
                print("Status: All tickets still sold out.")

        except Exception as e:
            print(f"Error during execution: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    check_tickets()
