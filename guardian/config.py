"""Bot-owned settings: only Telegram bootstrap credentials belong in .env."""
from __future__ import annotations

import os
from pathlib import Path


DATA_DIR = Path(os.environ.get("GUARDIAN_DATA_DIR", "/data"))
DATABASE = DATA_DIR / "guardian.sqlite3"
PROFILE_DIR = DATA_DIR / "chrome-profile"
REPORT_DIR = DATA_DIR / "reports"

# Based on the tested desktop xray-gemini-browser-audit defaults.
# Credentials and source URLs are set in Telegram, never written to .env.
DEFAULT_SETTINGS = {
    "MODE": "vless",
    "VLESS_URL": "",
    "RETRIES": "3",
    "ANSWER_WAIT": "70",
    "TIMEOUT": "55",
    "READY_WAIT": "15",
    "RESPONSE_STABLE": "4",
    "RETRY_DELAY": "7",
    "PAUSE": "8",
    "SOCKS_PORT": "18998",
    "XRAY_STARTUP_TIMEOUT": "20",
    "SCREENSHOT_SEND": "on_error",
    "STOP_ON_ACCOUNT_ISSUE": "true",
    "SCHEDULE_MINUTES": "0",
    "CLOSE_BROWSER": "true",
    "LOGIN_DIRECT": "manual",
    "CHROME_PATH": "/usr/bin/google-chrome",
    "PROFILE_DIR": str(PROFILE_DIR),
    "OUTPUT_DIR": str(REPORT_DIR),
    "XRAY_IMAGE": "bundled:26.3.27",
}

EDITABLE_SETTINGS = {
    "RETRIES": ("🔁 تعداد تلاش", "تعداد دفعات امتحان هر پورت؛ ۱ تا ۵"),
    "ANSWER_WAIT": ("💬 انتظار پاسخ (ثانیه)", "زمان انتظار برای پاسخ واقعی Gemini؛ ۱۵ تا ۱۸۰"),
    "TIMEOUT": ("🌐 مهلت بارگذاری (ثانیه)", "مهلت مسیریابی صفحه؛ ۵ تا ۱۸۰"),
    "READY_WAIT": ("⌛ مهلت آماده‌شدن (ثانیه)", "بودجه پیش از ارسال پیام؛ ۵ تا ۹۰"),
    "RESPONSE_STABLE": ("🧩 پایداری پاسخ (ثانیه)", "مدت ثابت‌ماندن متن خروجی؛ ۰ تا ۲۰"),
    "RETRY_DELAY": ("⏳ مکث بین تلاش‌ها", "ثانیه؛ ۰ تا ۶۰"),
    "PAUSE": ("⏸ مکث بین پورت‌ها", "ثانیه؛ ۰ تا ۱۲۰"),
    "SOCKS_PORT": ("🔌 پورت داخلی SOCKS", "۱۰۲۴ تا ۶۵۵۳۵، فقط داخل کانتینر"),
    "XRAY_STARTUP_TIMEOUT": ("🚀 مهلت راه‌اندازی Xray", "ثانیه؛ ۵ تا ۶۰"),
    "SCREENSHOT_SEND": ("📸 ارسال اسکرین‌شات", "none / on_error / all"),
    "STOP_ON_ACCOUNT_ISSUE": ("🛑 توقف در خطای حساب", "true / false؛ پیشنهاد: true"),
    "SCHEDULE_MINUTES": ("🕒 اجرای دوره‌ای", "۰ یعنی خاموش، یا ۶۰ تا ۱۰۰۸۰ دقیقه"),
}

BOUNDS = {
    "RETRIES": (1, 5), "ANSWER_WAIT": (15, 180), "TIMEOUT": (5, 180),
    "READY_WAIT": (5, 90), "RESPONSE_STABLE": (0, 20),
    "RETRY_DELAY": (0, 60), "PAUSE": (0, 120),
    "SOCKS_PORT": (1024, 65535), "XRAY_STARTUP_TIMEOUT": (5, 60),
    "SCHEDULE_MINUTES": (0, 10080),
}


def validate_setting(key: str, value: str) -> str:
    value = value.strip()
    if key not in EDITABLE_SETTINGS:
        raise ValueError("این تنظیم از طریق ربات قابل تغییر نیست.")
    if key in BOUNDS:
        try:
            number = int(value) if key not in {"RESPONSE_STABLE", "RETRY_DELAY", "PAUSE"} else float(value)
        except ValueError as exc:
            raise ValueError("یک عدد معتبر وارد کنید.") from exc
        lo, hi = BOUNDS[key]
        if not lo <= number <= hi or (key == "SCHEDULE_MINUTES" and number and number < 60):
            raise ValueError(f"مقدار باید بین {lo} و {hi} باشد (زمان‌بندی: صفر یا حداقل ۶۰).")
        return str(number)
    if key == "SCREENSHOT_SEND" and value not in {"none", "on_error", "all"}:
        raise ValueError("مجاز: none / on_error / all")
    if key == "STOP_ON_ACCOUNT_ISSUE" and value.lower() not in {"true", "false"}:
        raise ValueError("مجاز: true / false")
    return value.lower() if key == "STOP_ON_ACCOUNT_ISSUE" else value


def bootstrap_credentials() -> tuple[str, tuple[int, ...]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    # A new multi-admin list takes precedence over the legacy single-admin key.
    raw = (os.environ.get("TELEGRAM_ADMIN_IDS", "").strip()
           or os.environ.get("TELEGRAM_ADMIN_ID", "").strip())
    if not token or not raw:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_IDS (or TELEGRAM_ADMIN_ID) in .env")
    ids = []
    for item in raw.split(","):
        value = item.strip()
        if not value or not value.isascii() or not value.isdecimal():
            raise RuntimeError("Admin IDs must be positive numeric Telegram user IDs separated by commas")
        admin_id = int(value)
        if admin_id <= 0:
            raise RuntimeError("Admin IDs must be positive")
        if admin_id not in ids:
            ids.append(admin_id)
    if len(ids) > 20:
        raise RuntimeError("A maximum of 20 Telegram administrators is supported")
    return token, tuple(ids)
