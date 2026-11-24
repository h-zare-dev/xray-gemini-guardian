"""Evidence-aware, per-port progress and full-report formatting."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


GOOD = {"WEB_OK"}
BLOCKED = {"REGION_BLOCKED", "REGION_BLOCKED_CONFIRMED"}
PENDING = {"NOT_TESTED"}

STATUS_ICON = {
    "WEB_OK": "✅",
    "REGION_BLOCKED": "🚫",
    "REGION_BLOCKED_CONFIRMED": "🚫",
    "ACCOUNT_RESTRICTED": "🔐",
    "LOGIN_REQUIRED": "🔐",
    "VERIFICATION_REQUIRED": "🛡",
    "CONNECTION_FAILED_CONFIRMED": "🔌",
    "RETRYABLE_NETWORK": "🔌",
    "TEST_ERROR": "⚠️",
    "SUBMIT_FAILED": "⚠️",
    "INCONCLUSIVE": "❔",
    "NO_EDITOR": "❔",
    "RESPONSE_CANDIDATE": "❔",
    "NOT_TESTED": "⏳",
}


def bucket(status: str) -> str:
    if status in GOOD:
        return "ok"
    if status in BLOCKED:
        return "blocked"
    if status in PENDING:
        return "pending"
    # Google account challenges, network failures and unknown outcomes are
    # NOT evidence of a permanently bad exit IP.
    return "review"


@dataclass(frozen=True)
class Summary:
    total: int
    done: int
    ok: int
    blocked: int
    review: int
    pending: int


def summary(run: dict, rows: list[dict]) -> Summary:
    counts = Counter(bucket(row["status"]) for row in rows)
    total = int(run["total"])
    done = counts["ok"] + counts["blocked"] + counts["review"]
    # Legacy runs have no target manifest: reconstruct the unknown remainder
    # from the run's total, without guessing which ports were pending.
    pending = max(0, total - done)
    return Summary(total, done, counts["ok"], counts["blocked"],
                   counts["review"], pending)


def progress_message(index: int, target, result, counts: Counter, total: int) -> str:
    ok, blocked, review = (counts[k] for k in ("ok", "blocked", "review"))
    pct = round(index * 100 / total) if total else 0
    icon = STATUS_ICON.get(result.status, "🔎")
    name = str(target.name)[:42]
    detail = str(result.detail).replace("\n", " ")[:130]
    return (
        f"{icon} [{index}/{total} | {pct}%] پورت {target.port}\n"
        f"🔹 {name}\n"
        f"نتیجه: {result.status} | تلاش: {result.attempts}\n"
        f"{detail}\n"
        f"📊 ✅ سالم: {ok} | 🚫 منطقه‌ای: {blocked} | "
        f"⚠️ بررسی: {review} | ⏳ مانده: {max(0, total-index)}"
    )


def full_report_chunks(rows: list[dict], max_chars: int = 3000) -> list[str]:
    """Every planned port appears exactly once, including untested ports."""
    lines = [
        f"{idx:03d}. {STATUS_ICON.get(row['status'], '🔎')} "
        f"{row['port']} — {row['status']} "
        f"(تلاش {row.get('attempts', 0)})"
        for idx, row in enumerate(rows, 1)
    ]
    chunks = []
    current = []
    size = 0
    for line in lines:
        extra = len(line) + 1
        if current and size + extra > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def filtered(rows: list[dict], group: str) -> list[dict]:
    if group == "all":
        return rows
    if group not in {"ok", "blocked", "review", "pending"}:
        raise ValueError("Unknown report filter")
    return [row for row in rows if bucket(row["status"]) == group]
