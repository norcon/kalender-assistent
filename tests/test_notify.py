import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

import httpx

from src import notify
from src.config import Settings


def make_settings(**values):
    base = Settings.from_env()
    return replace(base, **values)


def ok_response():
    response = MagicMock()
    response.raise_for_status.return_value = None
    return response


class ChannelTests(unittest.TestCase):
    def test_channels_need_complete_settings(self):
        self.assertEqual(notify.channels(make_settings()), [])
        settings = make_settings(
            ntfy_url="https://ntfy.sh/familie-xyz", pushover_token="t", pushover_user="u",
            homeassistant_url="http://ha:8123", homeassistant_token="x", homeassistant_service="mobile_app_a",
            smtp_host="smtp.example.org", notify_email_to="a@example.org",
        )
        self.assertEqual(notify.channels(settings), ["ntfy", "Pushover", "Home Assistant", "E-Mail"])
        self.assertEqual(notify.channels(make_settings(pushover_token="t")), [])

    def test_ntfy_json_publish_with_click_link(self):
        settings = make_settings(ntfy_url="https://ntfy.example.org/familie-xyz", ntfy_token="tk", app_url="http://192.168.1.5:8080/")
        with patch.object(notify.httpx, "post", return_value=ok_response()) as post, self.assertLogs("src.notify", "INFO"):
            result = notify.send(settings, "Titel ä", "Text", path="/archiv", urgent=True)
        self.assertEqual(result, {"ntfy": "ok"})
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://ntfy.example.org")
        self.assertEqual(kwargs["json"]["topic"], "familie-xyz")
        self.assertEqual(kwargs["json"]["title"], "Titel ä")
        self.assertEqual(kwargs["json"]["click"], "http://192.168.1.5:8080/archiv")
        self.assertEqual(kwargs["json"]["priority"], 4)
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer tk"})

    def test_pushover_and_homeassistant_requests(self):
        settings = make_settings(pushover_token="t", pushover_user="u", homeassistant_url="http://ha:8123/",
                                 homeassistant_token="x", homeassistant_service="notify.mobile_app_a")
        with patch.object(notify.httpx, "post", return_value=ok_response()) as post, self.assertLogs("src.notify", "INFO"):
            notify.send(settings, "T", "M")
        urls = [call.args[0] for call in post.call_args_list]
        self.assertEqual(urls, ["https://api.pushover.net/1/messages.json", "http://ha:8123/api/services/notify/mobile_app_a"])
        self.assertEqual(post.call_args_list[0].kwargs["data"]["user"], "u")
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer x")

    def test_email_uses_starttls_and_login(self):
        settings = make_settings(smtp_host="smtp.example.org", smtp_port=587, smtp_username="me", smtp_password="pw",
                                 smtp_from="Kalender <me@example.org>", notify_email_to="a@example.org")
        client = MagicMock()
        client.__enter__.return_value = client
        with patch.object(notify.smtplib, "SMTP", return_value=client) as smtp, self.assertLogs("src.notify", "INFO"):
            self.assertEqual(notify.send(settings, "Betreff", "Inhalt"), {"E-Mail": "ok"})
        smtp.assert_called_once_with("smtp.example.org", 587, timeout=notify.TIMEOUT)
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("me", "pw")
        mail = client.send_message.call_args.args[0]
        self.assertEqual(mail["Subject"], "Betreff")
        self.assertEqual(mail["To"], "a@example.org")

    def test_one_failing_channel_does_not_stop_the_others(self):
        settings = make_settings(ntfy_url="https://ntfy.sh/x", pushover_token="t", pushover_user="u")
        failing = MagicMock()
        failing.raise_for_status.side_effect = httpx.HTTPStatusError("x", request=MagicMock(), response=MagicMock(status_code=403))
        with patch.object(notify.httpx, "post", side_effect=[failing, ok_response()]), self.assertLogs("src.notify", "INFO") as logs:
            result = notify.send(settings, "T", "M")
        self.assertEqual(result, {"ntfy": "HTTPStatusError 403", "Pushover": "ok"})
        self.assertTrue(any("fehlgeschlagen" in line for line in logs.output))

    def test_ntfy_url_without_topic_is_reported(self):
        settings = make_settings(ntfy_url="https://ntfy.sh/")
        with self.assertLogs("src.notify", "INFO"):
            self.assertEqual(notify.send(settings, "T", "M"), {"ntfy": "ValueError"})


if __name__ == "__main__":
    unittest.main()
