"""Multi-admin allowlist and per-port notification fan-out regression tests."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

from guardian.bot import GuardianBot
from guardian.config import bootstrap_credentials
from guardian.manager import RunManager


class BootstrapTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_ADMIN_IDS": "101, 202,101,303",
        "TELEGRAM_ADMIN_ID": "999",
    }, clear=True)
    def test_new_multi_admin_setting_takes_precedence_and_deduplicates(self):
        self.assertEqual(bootstrap_credentials(), ("test-token", (101, 202, 303)))

    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ADMIN_ID": "101"
    }, clear=True)
    def test_legacy_single_admin_still_works(self):
        self.assertEqual(bootstrap_credentials(), ("test-token", (101,)))

    def test_missing_or_invalid_admins_fail_closed(self):
        for value in ("", "101,", "101,abc", "101,-2", "0", "101,,202", "۱۲۳"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {
                    "TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ADMIN_IDS": value,
                }, clear=True):
                    with self.assertRaises(RuntimeError):
                        bootstrap_credentials()

    @patch.dict(os.environ, {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_ADMIN_IDS": ",".join(str(i) for i in range(1, 22)),
    }, clear=True)
    def test_unbounded_admin_broadcast_is_rejected(self):
        with self.assertRaises(RuntimeError):
            bootstrap_credentials()


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.photos = []

    async def send_message(self, chat_id, text):
        if chat_id == 202:
            raise RuntimeError("Admin has not pressed Start")
        self.messages.append((chat_id, text))

    async def send_photo(self, chat_id, photo, caption):
        if chat_id == 202:
            raise RuntimeError("Admin has not pressed Start")
        self.photos.append((chat_id, photo.read(), caption))


class PermissionAndBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_chat_allowlist_has_no_owner_or_roles(self):
        guardian = GuardianBot.__new__(GuardianBot)
        guardian.admin_ids = frozenset((101, 202))

        async def check(user_id, chat_type):
            reply = AsyncMock()
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=user_id),
                effective_chat=SimpleNamespace(type=chat_type),
                callback_query=None,
                message=SimpleNamespace(reply_text=reply),
            )
            return await guardian.allowed(update), reply

        for user_id in (101, 202):
            allowed, reply = await check(user_id, ChatType.PRIVATE)
            self.assertTrue(allowed)
            reply.assert_not_awaited()

        for user_id, chat_type in ((303, ChatType.PRIVATE), (101, ChatType.GROUP)):
            allowed, reply = await check(user_id, chat_type)
            self.assertFalse(allowed)
            reply.assert_awaited_once()

    async def test_every_admin_gets_text_and_photo_even_if_one_chat_fails(self):
        telegram = FakeTelegram()
        manager = RunManager(None, telegram, (101, 202, 303), asyncio.get_running_loop())
        with self.assertLogs("guardian.manager", level="WARNING") as captured:
            await manager._send_text_to_admins("✅ [3/45] WEB_OK")
        self.assertEqual(telegram.messages, [
            (101, "✅ [3/45] WEB_OK"), (303, "✅ [3/45] WEB_OK"),
        ])
        self.assertEqual(len(captured.records), 1)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "shot.png"
            path.write_bytes(b"image-bytes")
            with self.assertLogs("guardian.manager", level="WARNING") as captured:
                await manager._send_photo_to_admins(str(path), "Test screenshot")
        self.assertEqual(telegram.photos, [
            (101, b"image-bytes", "Test screenshot"),
            (303, b"image-bytes", "Test screenshot"),
        ])
        self.assertEqual(len(captured.records), 1)


if __name__ == "__main__":
    unittest.main()
