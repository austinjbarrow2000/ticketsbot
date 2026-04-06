import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import monitor


class TestMonitor(unittest.TestCase):
    @patch("monitor.requests.post")
    def test_send_telegram_posts_expected_message(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        mock_post.return_value = mock_response

        with patch.object(monitor, "TELEGRAM_TOKEN", "test-token"), patch.object(
            monitor, "TELEGRAM_CHAT_ID", "12345"
        ):
            sent = monitor.send_telegram("Test message")

        self.assertTrue(sent)

        mock_post.assert_called_once_with(
            "https://api.telegram.org/bottest-token/sendMessage",
            json={
                "chat_id": "12345",
                "text": "Test message",
                "parse_mode": "HTML",
            },
            timeout=15,
        )

    @unittest.skipUnless(
        os.getenv("RUN_TELEGRAM_INTEGRATION_TEST") == "1",
        "Set RUN_TELEGRAM_INTEGRATION_TEST=1 to run real Telegram send test",
    )
    def test_send_telegram_real_integration(self):
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.assertIsNotNone(token, "TELEGRAM_TOKEN must be set")
        self.assertIsNotNone(chat_id, "TELEGRAM_CHAT_ID must be set")

        with patch.object(monitor, "TELEGRAM_TOKEN", token), patch.object(
            monitor, "TELEGRAM_CHAT_ID", chat_id
        ):
            sent = monitor.send_telegram("[ticketsbot] integration test message")

        self.assertTrue(sent)

    @patch("monitor.send_telegram")
    @patch("monitor.should_send_daily_status", return_value=False)
    @patch("monitor.sync_playwright")
    def test_check_tickets_sends_telegram_when_ticket_button_found(
        self, mock_sync_playwright, _mock_daily_status, mock_send_telegram
    ):
        playwright = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()

        mock_sync_playwright.return_value.__enter__.return_value = playwright
        playwright.chromium.launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = page

        # Simulate ticket action buttons visible on page with one available ticket.
        ticket_button = MagicMock()
        ticket_button.inner_text.return_value = "Regular Entrance Ticket 2 Select"
        sold_out_button = MagicMock()
        sold_out_button.inner_text.return_value = "VIP Entrance Ticket 0 Select"
        page.query_selector_all.return_value = [ticket_button, sold_out_button]

        mock_send_telegram.side_effect = [True, True]

        monitor.check_tickets()

        self.assertEqual(mock_send_telegram.call_count, 2)

        first_message = mock_send_telegram.call_args_list[0].args[0]
        second_message = mock_send_telegram.call_args_list[1].args[0]

        self.assertIn("TICKET REVEALED", first_message)
        self.assertIn(monitor.URL, first_message)
        self.assertIn("Ticket details", second_message)
        self.assertIn("Regular Entrance Ticket", second_message)
        self.assertIn("2 available", second_message)
        self.assertNotIn("VIP Entrance Ticket", second_message)

    @patch("monitor.send_telegram")
    @patch("monitor.should_send_daily_status", return_value=True)
    @patch("monitor.sync_playwright")
    def test_check_tickets_sends_daily_message_when_sold_out(
        self, mock_sync_playwright, _mock_daily_status, mock_send_telegram
    ):
        playwright = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()

        mock_sync_playwright.return_value.__enter__.return_value = playwright
        playwright.chromium.launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = page

        page.query_selector_all.return_value = []
        page.inner_text.return_value = (
            "Regular Entrance Ticket\n0\nVIP Entrance Ticket\n0"
        )

        mock_send_telegram.return_value = True

        monitor.check_tickets()

        mock_send_telegram.assert_called_once()
        sent_message = mock_send_telegram.call_args.args[0]
        self.assertIn("Daily check", sent_message)
        self.assertIn("No tickets found today", sent_message)

    def test_extract_available_ticket_details_parses_positive_counts(self):
        button1 = MagicMock()
        button1.inner_text.return_value = "Regular Entrance Ticket 2 Select"
        button2 = MagicMock()
        button2.inner_text.return_value = "VIP Entrance Ticket 0 Select"

        details = monitor.extract_available_ticket_details([button1, button2])

        self.assertEqual(details, [("Regular Entrance Ticket", 2)])

    def test_parse_ticket_inventory_from_text(self):
        page_text = """
        Regular Entrance Ticket 0
        VIP Entrance Ticket 2
        Locker L - with code 1
        21 Sold
        """

        details = monitor.parse_ticket_inventory_from_text(page_text)

        self.assertEqual(
            details,
            [
                ("Regular Entrance Ticket", 0),
                ("VIP Entrance Ticket", 2),
                ("Locker L - with code", 1),
            ],
        )

    def test_should_send_daily_status_true_at_target_time(self):
        with patch.object(monitor, "DAILY_STATUS_ENABLED", "1"), patch.object(
            monitor, "DAILY_STATUS_HOUR", 20
        ), patch.object(monitor, "DAILY_STATUS_TZ", "America/New_York"):
            target_time = datetime(
                2026, 4, 6, 20, 0, tzinfo=ZoneInfo("America/New_York")
            )
            self.assertTrue(monitor.should_send_daily_status(now=target_time))

    def test_should_send_daily_status_false_outside_target_time(self):
        with patch.object(monitor, "DAILY_STATUS_ENABLED", "1"), patch.object(
            monitor, "DAILY_STATUS_HOUR", 20
        ), patch.object(monitor, "DAILY_STATUS_TZ", "America/New_York"):
            non_target_time = datetime(
                2026, 4, 6, 19, 55, tzinfo=ZoneInfo("America/New_York")
            )
            self.assertFalse(monitor.should_send_daily_status(now=non_target_time))

    @unittest.skipUnless(
        os.getenv("RUN_TELEGRAM_WEBSITE_INTEGRATION_TEST") == "1",
        "Set RUN_TELEGRAM_WEBSITE_INTEGRATION_TEST=1 for live website + Telegram integration test",
    )
    def test_live_website_parsing_and_two_telegram_messages(self):
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.assertIsNotNone(token, "TELEGRAM_TOKEN must be set")
        self.assertIsNotNone(chat_id, "TELEGRAM_CHAT_ID must be set")

        with monitor.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.goto(monitor.URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(5000)
            inventory = monitor.extract_ticket_inventory(page)
            browser.close()

        self.assertGreater(
            len(inventory), 0, "No ticket inventory parsed from live page"
        )
        available = [(name, count) for name, count in inventory if count > 0]

        if available:
            first_message = (
                "<b>[integration] 🚨 TICKET REVEALED!</b>\n"
                "Live website parsing found available tickets."
            )
            details_for_message = available
        else:
            first_message = (
                "<b>[integration] No tickets currently available</b>\n"
                "Live website parsing ran successfully."
            )
            details_for_message = inventory

        detail_lines = [
            f"- {name}: {count}" for name, count in details_for_message[:10]
        ]
        second_message = "<b>[integration] Live ticket snapshot:</b>\n" + "\n".join(
            detail_lines
        )

        with patch.object(monitor, "TELEGRAM_TOKEN", token), patch.object(
            monitor, "TELEGRAM_CHAT_ID", chat_id
        ):
            sent_first = monitor.send_telegram(first_message)
            sent_second = monitor.send_telegram(second_message)

        self.assertTrue(sent_first)
        self.assertTrue(sent_second)

    @unittest.skipUnless(
        os.getenv("RUN_TELEGRAM_FULL_INTEGRATION_TEST") == "1",
        "Set RUN_TELEGRAM_FULL_INTEGRATION_TEST=1 for full live Telegram message integration",
    )
    def test_live_website_sends_status_detail_and_daily_messages(self):
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.assertIsNotNone(token, "TELEGRAM_TOKEN must be set")
        self.assertIsNotNone(chat_id, "TELEGRAM_CHAT_ID must be set")

        with monitor.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.goto(monitor.URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(5000)
            inventory = monitor.extract_ticket_inventory(page)
            browser.close()

        available = [(name, count) for name, count in inventory if count > 0]

        if available:
            status_message = (
                "<b>[integration] 🚨 TICKET REVEALED!</b>\n"
                "Live website parsing found available tickets."
            )
            detail_message = monitor.build_ticket_detail_message(available)
            daily_message = "<b>[integration] Daily check:</b> Tickets are currently available. Monitoring is running."
        else:
            status_message = (
                "<b>[integration] No tickets currently available</b>\n"
                "Live website parsing ran successfully."
            )
            details_for_message = (
                inventory[:10] if inventory else [("No inventory rows parsed", 0)]
            )
            detail_lines = [f"- {name}: {count}" for name, count in details_for_message]
            detail_message = "<b>[integration] Live ticket snapshot:</b>\n" + "\n".join(
                detail_lines
            )
            daily_message = "<b>[integration] Daily check:</b> No tickets found today. Monitoring is running."

        with patch.object(monitor, "TELEGRAM_TOKEN", token), patch.object(
            monitor, "TELEGRAM_CHAT_ID", chat_id
        ):
            sent_status = monitor.send_telegram(status_message)
            sent_detail = monitor.send_telegram(detail_message)
            sent_daily = monitor.send_telegram(daily_message)

        self.assertTrue(sent_status)
        self.assertTrue(sent_detail)
        self.assertTrue(sent_daily)


if __name__ == "__main__":
    unittest.main()
