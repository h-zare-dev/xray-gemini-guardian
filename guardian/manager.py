"""Single-flight background runner. All production routing stays untouched."""
from __future__ import annotations

import asyncio
import collections
import threading
import time
from pathlib import Path

from .checker import ACCOUNT_STATES, BrowserChecker
from .config import REPORT_DIR
from .sources import targets_for_run
from .storage import Store


STATUS_ICON = {
    "WEB_OK": "✅", "REGION_BLOCKED": "🌍", "ACCOUNT_RESTRICTED": "🚫",
    "LOGIN_REQUIRED": "🔐", "VERIFICATION_REQUIRED": "🛡",
    "RETRYABLE_NETWORK": "🔌", "TEST_ERROR": "⚠️",
    "INCONCLUSIVE": "❔", "NO_EDITOR": "❔", "STOPPED": "⏹",
}


class RunManager:
    def __init__(self, store: Store, bot, admin_id: int, loop: asyncio.AbstractEventLoop):
        self.store = store
        self.bot = bot
        self.admin_id = admin_id
        self.loop = loop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._login_proc = None
        self._last_scheduled = 0.0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def login_active(self) -> bool:
        return bool(self._login_proc and self._login_proc.poll() is None)

    def tell(self, message: str):
        try:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_message(chat_id=self.admin_id, text=message),
                self.loop
            ).result(timeout=15)
        except Exception:
            # Audit persists in SQLite even if Telegram delivery fails.
            pass

    def send_photo(self, image: str, caption: str):
        try:
            async def send():
                with open(image, "rb") as photo:
                    await self.bot.send_photo(chat_id=self.admin_id, photo=photo,
                                              caption=caption[:1024])
            asyncio.run_coroutine_threadsafe(send(), self.loop).result(timeout=45)
        except Exception:
            pass

    def start(self, targets=None) -> tuple[bool, str]:
        with self._lock:
            if self.login_active:
                return False, "🔐 ابتدا پنجره ورود به Google را ببندید."
            if self.running:
                return False, "⏳ یک آزمون در حال اجراست."
            try:
                selected = list(targets) if targets is not None else targets_for_run(self.store)
            except ValueError as exc:
                return False, str(exc)
            if not selected:
                return False, "هیچ خروجی برای تست انتخاب نشده است."
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(selected,),
                                            daemon=True, name="guardian-audit")
            self._thread.start()
            return True, f"🚀 تست {len(selected)} خروجی شروع شد."

    def stop(self):
        if self.running:
            self._stop.set()
            return "⏹ درخواست توقف ثبت شد. تست جاری تکمیل و سپس متوقف می‌شود."
        return "هیچ آزمونی در حال اجرا نیست."

    def _run(self, targets):
        settings = self.store.settings()
        run_id = self.store.begin_run(len(targets))
        folder = REPORT_DIR / f"run_{run_id:06d}"
        checker = BrowserChecker(settings, folder)
        state, note = "completed", ""
        counts = collections.Counter()
        try:
            self.tell(f"🧪 Gemini Guardian | گزارش #{run_id}\nتعداد خروجی: {len(targets)}\n"
                      "در حال بررسی واقعی پاسخ Gemini Web…")
            for index, target in enumerate(targets, 1):
                if self._stop.is_set():
                    state, note = "stopped", "Stopped by administrator"
                    break
                result = checker.probe(target, index, self._stop.is_set)
                if result.status == "STOPPED":
                    state, note = "stopped", "Stopped by administrator"
                    break
                counts[result.status] += 1
                self.store.add_result(run_id, index, target.name, target.port, result.status,
                                      result.detail, result.attempts, result.screenshot)
                icon = STATUS_ICON.get(result.status, "🔎")
                if result.status != "WEB_OK" or index % 5 == 0 or index == len(targets):
                    self.tell(f"{icon} [{index}/{len(targets)}] {target.name[:42]} : {target.port}\n"
                              f"نتیجه: {result.status} | تلاش: {result.attempts}")
                policy = settings.get("SCREENSHOT_SEND", "on_error")
                if result.screenshot and (policy == "all" or
                    (policy == "on_error" and result.status != "WEB_OK")):
                    self.send_photo(result.screenshot,
                                    f"📸 #{run_id} | {target.name[:40]} | {result.status}")
                if result.status in ACCOUNT_STATES and settings.get("STOP_ON_ACCOUNT_ISSUE") == "true":
                    state, note = "account_attention", result.status
                    self.tell("🛑 آزمون متوقف شد: مشکل حساب یا ورود Google.\n"
                              "این خطا نشانه خراب بودن IP نیست. لطفاً در مرورگر وضعیت حساب را بررسی کنید.")
                    break
                if index < len(targets):
                    delay = float(settings["PAUSE"])
                    if self._stop.wait(delay):
                        state, note = "stopped", "Stopped by administrator"
                        break
        except Exception as exc:
            state, note = "error", type(exc).__name__
            self.tell(f"⚠️ آزمون با خطای سیستمی {note} متوقف شد. نتیجه قبلی ذخیره است.")
        finally:
            self.store.finish_run(run_id, state, note)
            summary = " | ".join(f"{STATUS_ICON.get(k,'•')}{k}: {v}" for k, v in counts.items())
            self.tell(f"🏁 گزارش #{run_id}\nوضعیت: {state}\n{summary or 'بدون نتیجه'}\n"
                      "📋 گزارش کامل و تصاویر از منوی «گزارش‌ها» در دسترس است.")

    def start_login(self) -> tuple[bool, str]:
        import subprocess
        from .config import PROFILE_DIR
        with self._lock:
            if self.running:
                return False, "ابتدا آزمون جاری را متوقف کنید."
            if self.login_active:
                return True, "مرورگر ورود Google همین حالا باز است."
            self._login_proc = subprocess.Popen(
                ["/usr/bin/google-chrome", f"--user-data-dir={PROFILE_DIR}",
                 "--no-first-run", "--no-proxy-server", "--no-sandbox", "--new-window",
                 "https://gemini.google.com/app"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True, ("🔐 مرورگر بدون پروکسی باز شد. از noVNC و SSH tunnel وارد شوید؛ "
                          "بعد از ورود «بستن مرورگر» را بزنید تا پروفایل ذخیره شود.")

    def stop_login(self) -> str:
        import os
        import signal
        with self._lock:
            if not self.login_active:
                return "پنجره ورود باز نیست."
            proc = self._login_proc
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self._login_proc = None
            return "✅ مرورگر بسته شد؛ نشست Google روی دیسک کانتینر ذخیره می‌ماند."

    def maybe_schedule(self) -> bool:
        minutes = int(self.store.get("SCHEDULE_MINUTES"))
        if minutes == 0 or self.running or self.login_active:
            return False
        now = time.monotonic()
        # Restart of container must not launch a cycle immediately.
        if not self._last_scheduled:
            self._last_scheduled = now
            return False
        if now - self._last_scheduled < minutes * 60:
            return False
        self._last_scheduled = now
        started, _ = self.start()
        return started
