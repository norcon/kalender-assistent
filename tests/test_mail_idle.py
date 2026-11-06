import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import mail_idle


class FakeSocket:
    def pending(self):
        return 0


class FakeImap:
    def __init__(self, lines, capabilities=("IMAP4REV1", "IDLE")):
        self.lines = list(lines)
        self.sent = []
        self.capabilities = capabilities
        self.sock = FakeSocket()

    def _new_tag(self):
        return b"A1"

    def send(self, data):
        self.sent.append(data)

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def login(self, *args):
        return "OK", []

    def select(self, *args, **kwargs):
        return "OK", []


SETTINGS = SimpleNamespace(
    imap_host="imap.example.org", imap_port=993, imap_username="u",
    imap_password="p", imap_folder="INBOX",
)


class IdleTests(unittest.TestCase):
    def watcher(self):
        return mail_idle.IdleWatcher(lambda: SETTINGS, lambda: None, threading.Event())

    def test_reports_new_mail_and_ends_idle_cleanly(self):
        client = FakeImap([b"+ idling\r\n", b"* 7 EXISTS\r\n", b"A1 OK IDLE terminated\r\n"])
        with patch.object(mail_idle.select, "select", return_value=([client.sock], [], [])):
            self.assertTrue(self.watcher()._idle_once(client))
        self.assertEqual(client.sent, [b"A1 IDLE\r\n", b"DONE\r\n"])

    def test_closed_connection_raises(self):
        client = FakeImap([b"+ idling\r\n"])
        with patch.object(mail_idle.select, "select", return_value=([client.sock], [], [])):
            with self.assertRaises(ConnectionError):
                self.watcher()._idle_once(client)

    def test_server_without_idle_falls_back_to_polling(self):
        watcher = self.watcher()
        with (
            patch.object(mail_idle.imaplib, "IMAP4_SSL", return_value=FakeImap([], ("IMAP4REV1",))),
            self.assertLogs("src.mail_idle", level="INFO"),
        ):
            watcher.run()
        self.assertEqual(watcher.state["status"], "unsupported")

    def test_new_mail_callback_and_reconnect_on_settings_change(self):
        events = []
        current = {"settings": SETTINGS}
        watcher = mail_idle.IdleWatcher(lambda: current["settings"], lambda: events.append("neu"), threading.Event())
        client = FakeImap([b"+ idling\r\n", b"* 8 EXISTS\r\n", b"A1 OK done\r\n"])

        original = watcher._idle_once

        def idle_once(c):
            result = original(c)
            current["settings"] = SimpleNamespace(**{**vars(SETTINGS), "imap_folder": "Kalender"})
            return result

        with (
            patch.object(mail_idle.imaplib, "IMAP4_SSL", return_value=client),
            patch.object(mail_idle.select, "select", return_value=([client.sock], [], [])),
            patch.object(watcher, "_idle_once", side_effect=idle_once),
            self.assertLogs("src.mail_idle", level="INFO") as logs,
        ):
            self.assertTrue(watcher._session(SETTINGS))
        self.assertEqual(events, ["neu"])
        self.assertTrue(any("verbindet neu" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
