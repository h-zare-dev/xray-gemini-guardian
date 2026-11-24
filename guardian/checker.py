"""Headful Chrome + local bundled Xray; legacy Gemini evidence checks are reused."""
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from legacy.src.vless import outbound_from_url, xray_config

# Original audit modules use their historical absolute imports.
import sys
_legacy_path = str(Path(__file__).resolve().parent.parent / "legacy" / "src")
if _legacy_path not in sys.path:
    sys.path.insert(0, _legacy_path)
from session_audit import check_gemini, consolidate, make_run_directory  # noqa: E402


ACCOUNT_RESTRICTED = re.compile(
    r"review service restriction|entire service unavailable|"
    r"access to a service or feature has been restricted|"
    r"account has tried to override or circumvent abuse protections", re.I
)
VERIFICATION = re.compile(
    r"verify your info to continue|scan the qr code with your phone|"
    r"verify it.s you|verify your identity|unusual activity|"
    r"2.step verification|تأیید هویت", re.I
)
ACCOUNT_STATES = {"ACCOUNT_RESTRICTED", "VERIFICATION_REQUIRED", "LOGIN_REQUIRED"}


@dataclass
class ProbeResult:
    status: str
    detail: str
    attempts: int
    screenshot: str = ""


def detect_account_issue(page) -> tuple[str, str] | None:
    url = page.url.lower()
    try:
        body = page.locator("body").inner_text(timeout=1800)
    except Exception:
        body = ""
    if "myaccount.google.com/restrictions" in url or ACCOUNT_RESTRICTED.search(body):
        return "ACCOUNT_RESTRICTED", "Google account access to Gemini is restricted; review it manually."
    if VERIFICATION.search(body) or "/signin/challenge" in url:
        return "VERIFICATION_REQUIRED", "Google requests an interactive security verification."
    if "accounts.google.com" in url:
        return "LOGIN_REQUIRED", "Google session expired; manual login required."
    return None


def wait_port(port: int, proc: subprocess.Popen, seconds: int) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@contextlib.contextmanager
def xray_socks(uri: str, target_port: int, local_port: int, startup: int):
    outbound = outbound_from_url(uri)
    with tempfile.TemporaryDirectory(prefix="guardian-xray-") as directory:
        conf = xray_config(outbound, target_port, local_port)
        conf["inbounds"][0]["listen"] = "127.0.0.1"
        conf_path = Path(directory) / "config.json"
        conf_path.write_text(json.dumps(conf), encoding="utf8")
        conf_path.chmod(0o600)
        proc = subprocess.Popen(
            ["xray", "run", "-c", str(conf_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        try:
            if not wait_port(local_port, proc, startup):
                raise RuntimeError("Bundled Xray failed to open local SOCKS port.")
            yield
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


class BrowserChecker:
    def __init__(self, settings: dict[str, str], report_dir: Path):
        self.settings = settings
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.profile = Path(settings["PROFILE_DIR"])
        self.profile.mkdir(parents=True, exist_ok=True)
        self.args = SimpleNamespace(
            ready_wait=int(settings["READY_WAIT"]),
            timeout=int(settings["TIMEOUT"]),
            answer_wait=int(settings["ANSWER_WAIT"]),
            response_stable=float(settings["RESPONSE_STABLE"])
        )

    def probe(self, target, index: int, stop_requested: Callable[[], bool]) -> ProbeResult:
        from playwright.sync_api import sync_playwright

        attempts = []
        image = ""
        local_port = int(self.settings["SOCKS_PORT"])
        repeat = int(self.settings["RETRIES"])
        for attempt in range(1, repeat + 1):
            if stop_requested():
                return ProbeResult("STOPPED", "Manually stopped", len(attempts), image)
            status, detail = "INCONCLUSIVE", "No response"
            try:
                # Close Chrome completely between targets, so its existing
                # WebSockets or HTTP sessions cannot bleed into another IP.
                with xray_socks(target.uri, target.port, local_port,
                                int(self.settings["XRAY_STARTUP_TIMEOUT"])):
                    with sync_playwright() as playwright:
                        context = playwright.chromium.launch_persistent_context(
                            user_data_dir=str(self.profile),
                            channel="chrome",
                            headless=False,
                            proxy={"server": f"socks5://127.0.0.1:{local_port}"},
                            viewport={"width": 1360, "height": 900},
                            args=["--disable-quic", "--no-first-run"],
                            timeout=30000,
                        )
                        try:
                            page = context.pages[0] if context.pages else context.new_page()
                            page.set_default_timeout(12000)
                            try:
                                status, detail = check_gemini(page, self.args,
                                                              f"{index}_{target.port}_{attempt}")
                                issue = detect_account_issue(page)
                                if issue:
                                    status, detail = issue
                            finally:
                                # Even if Gemini navigation or the UI throws,
                                # capture what the headful browser actually showed.
                                image_path = self.report_dir / f"{index:04d}_{target.port}_try{attempt}.png"
                                try:
                                    page.screenshot(path=str(image_path), timeout=10000, full_page=True)
                                    image = str(image_path)
                                except Exception:
                                    pass
                        finally:
                            context.close()
            except Exception as exc:
                # Do not include user credentials or proxy URLs in diagnostic messages.
                status = "TEST_ERROR"
                detail = f"{type(exc).__name__}; inspect container logs and screenshot."
            attempts.append((status, detail))
            if status in {"WEB_OK", "REGION_BLOCKED", *ACCOUNT_STATES}:
                break
            if attempt < repeat and not stop_requested():
                # Cooperative cancellation during retry delay.
                delay = float(self.settings["RETRY_DELAY"])
                end = time.monotonic() + delay
                while time.monotonic() < end and not stop_requested():
                    time.sleep(min(0.5, end - time.monotonic()))

        states = [status for status, _ in attempts]
        for issue in ("ACCOUNT_RESTRICTED", "VERIFICATION_REQUIRED", "LOGIN_REQUIRED"):
            if issue in states:
                return ProbeResult(issue, next(detail for st, detail in attempts if st == issue),
                                   len(attempts), image)
        status, detail = consolidate(attempts)
        if states and states[-1] == "TEST_ERROR" and status == "INCONCLUSIVE":
            detail = "Xray/Chrome could not complete this test; no IP verdict."
        return ProbeResult(status, detail, len(attempts), image)
