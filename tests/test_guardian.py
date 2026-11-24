import base64
import tempfile
import unittest
from pathlib import Path

from guardian.config import validate_setting
from guardian.sources import (parse_subscription, ports_from_spec, targets_for_run,
                              validate_subscription_url)
from guardian.storage import Store
from guardian.checker import detect_account_issue


VLESS = "vless://00000000-0000-0000-0000-000000000001@example.com:443?type=tcp&security=none#Test"


class SettingsTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate_setting("ANSWER_WAIT", "90"), "90")
        self.assertEqual(validate_setting("STOP_ON_ACCOUNT_ISSUE", "TRUE"), "true")
        with self.assertRaises(ValueError):
            validate_setting("RETRIES", "100")
        with self.assertRaises(ValueError):
            validate_setting("VLESS_URL", VLESS)
        with self.assertRaises(ValueError):
            validate_setting("SCHEDULE_MINUTES", "1")

    def test_ports(self):
        self.assertEqual(ports_from_spec("18881-18883,18882,20001"),
                         [18881, 18882, 18883, 20001])
        with self.assertRaises(ValueError):
            ports_from_spec("70000")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "guardian.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_ports_and_run(self):
        self.store.set("VLESS_URL", VLESS)
        self.store.add_ports([18881, 18882])
        self.store.toggle_port(18882)
        targets = targets_for_run(self.store)
        self.assertEqual([(t.port, t.name) for t in targets], [(18881, "VLESS_18881")])
        run_id = self.store.begin_run(1)
        self.store.add_result(run_id, 1, "a", 18881, "WEB_OK", "ok", 1, "")
        self.store.finish_run(run_id, "completed")
        self.assertEqual(self.store.latest_run()["completed"], 1)
        self.assertEqual(self.store.results(run_id)[0]["status"], "WEB_OK")

    def test_subscription_cache(self):
        self.store.add_subscription("https://example.com/sub")
        sub_id = self.store.list_subscriptions()[0]["id"]
        targets, skipped = parse_subscription(VLESS + "\nss://bad")
        self.assertEqual(skipped, 1)
        self.store.save_subscription_targets(sub_id, targets)
        self.store.set("MODE", "subscription")
        self.assertEqual(targets_for_run(self.store)[0].name, "Test")
        self.store.remove_subscription(sub_id)
        self.assertEqual(self.store.cached_targets(), [])

    def test_local_url_rejected(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/x", "https://localhost/x"):
            with self.assertRaises(ValueError):
                validate_subscription_url(url)


class FakeBody:
    def __init__(self, text): self.text = text
    def inner_text(self, timeout=0): return self.text


class FakePage:
    def __init__(self, url, body):
        self.url = url
        self.body = body
    def locator(self, selector):
        return FakeBody(self.body)


class ClassificationTests(unittest.TestCase):
    def test_google_account_restricted(self):
        page = FakePage("https://myaccount.google.com/restrictions/280",
                        "Entire service unavailable")
        self.assertEqual(detect_account_issue(page)[0], "ACCOUNT_RESTRICTED")

    def test_google_verification_not_bad_ip(self):
        page = FakePage("https://accounts.google.com/v3/signin/challenge",
                        "Verify your info to continue")
        self.assertEqual(detect_account_issue(page)[0], "VERIFICATION_REQUIRED")

    def test_no_false_positive(self):
        page = FakePage("https://gemini.google.com/app", "How can I help?")
        self.assertIsNone(detect_account_issue(page))


if __name__ == "__main__":
    unittest.main()
