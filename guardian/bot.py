"""Persian inline-button Telegram control panel; private admins only."""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
from urllib.parse import urlparse

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup as K
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from telegram.ext import MessageHandler, filters

from .config import EDITABLE_SETTINGS, REPORT_DIR, bootstrap_credentials, validate_setting
from .manager import RunManager, STATUS_ICON
from .reporting import filtered, summary
from .sources import (fetch_subscription, parse_subscription, ports_from_spec,
                      safe_source_label, targets_for_run, validate_subscription_url)
from .storage import Store
from legacy.src.vless import outbound_from_url

log = logging.getLogger(__name__)
SETTINGS_KEYS = list(EDITABLE_SETTINGS)


def markup(*rows):
    return K([[B(text, callback_data=action) for text, action in row] for row in rows])


def short(text, maximum=60):
    return text if len(text) <= maximum else text[:maximum - 1] + "…"


class GuardianBot:
    def __init__(self):
        token, admin_ids = bootstrap_credentials()
        self.admin_ids = frozenset(admin_ids)
        self.store = Store()
        self.manager: RunManager | None = None
        self.app = (Application.builder().token(token)
                    .post_init(self.post_init).build())
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("menu", self.start))
        self.app.add_handler(CommandHandler("cancel", self.cancel))
        self.app.add_handler(CallbackQueryHandler(self.button))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.input))
        self.app.add_error_handler(self.error_handler)

    async def post_init(self, app: Application):
        self.manager = RunManager(self.store, app.bot, tuple(sorted(self.admin_ids)),
                                  asyncio.get_running_loop())
        if app.job_queue:
            app.job_queue.run_repeating(self.schedule_tick, interval=60, first=60)
        # No full VLESS URI, subscription secret, password, or Google cookie is logged.
        for admin_id in self.admin_ids:
            try:
                await app.bot.send_message(
                    chat_id=admin_id,
                    text="🛡 Gemini Guardian روشن شد. برای مدیریت /start را بزنید."
                )
            except Exception:
                # Each admin must press Start first; one unavailable chat must
                # not prevent other administrators from using the bot.
                log.info("Startup message unavailable for admin %d; send /start first.", admin_id)

    async def schedule_tick(self, context: ContextTypes.DEFAULT_TYPE):
        if self.manager:
            await asyncio.to_thread(self.manager.maybe_schedule)

    async def allowed(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if user and user.id in self.admin_ids and chat and chat.type == ChatType.PRIVATE:
            return True
        if update.callback_query:
            await update.callback_query.answer("⛔ دسترسی مجاز نیست.", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ این ربات خصوصی است.")
        return False

    async def view(self, update: Update, text: str, keyboard=None):
        # Reuse one message as a panel. Ask questions in a fresh message.
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode="HTML",
                    disable_web_page_preview=True)
                return
            except Exception:
                pass
        await update.effective_chat.send_message(
            text, reply_markup=keyboard, parse_mode="HTML",
            disable_web_page_preview=True)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.allowed(update):
            return
        await self.home(update)

    async def home(self, update: Update):
        mode = self.store.get("MODE")
        ports = self.store.list_ports()
        subs = self.store.list_subscriptions()
        latest = self.store.latest_run()
        running = self.manager.running if self.manager else False
        text = (
            "🛡 <b>XRAY GEMINI GUARDIAN</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ حالت: <b>{mode.upper()}</b>\n"
            f"🔌 پورت‌ها: <b>{sum(v for _, v in ports)} فعال / {len(ports)} کل</b>\n"
            f"🔗 سابسکریپشن‌ها: <b>{len(subs)}</b>\n"
            f"🧪 آزمون: <b>{'در حال اجرا ⏳' if running else 'آماده ✅'}</b>\n"
            f"📊 آخرین گزارش: <b>#{latest['id'] if latest else '—'}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ملاک قبولی: <b>پاسخ واقعی Gemini Web</b>، نه صرفاً ping."
        )
        await self.view(update, text, markup(
            [("🚀 تست همه خروجی‌ها", "run:all"), ("⏹ توقف", "run:stop")],
            [("🔌 مدیریت پورت‌ها", "ports:0"), ("🔗 منابع و سابسکریپشن", "sources")],
            [("⚙️ تنظیمات تست", "settings:0"), ("📊 گزارش‌ها و تصاویر", "reports")],
            [("🔐 ورود به Google", "login"), ("📡 وضعیت سیستم", "status")],
        ))

    async def status(self, update: Update):
        latest = self.store.latest_run()
        mode = self.store.get("MODE")
        try:
            count = len(targets_for_run(self.store))
        except ValueError:
            count = 0
        text = (
            "📡 <b>وضعیت Gemini Guardian</b>\n\n"
            f"🔹 حالت: {mode}\n"
            f"🔹 خروجی‌های قابل‌تست: {count}\n"
            f"🔹 آزمون فعال: {'بله' if self.manager.running else 'خیر'}\n"
            f"🔹 مرورگر ورود: {'باز' if self.manager.login_active else 'بسته'}\n"
            f"🔹 زمان‌بندی: هر {self.store.get('SCHEDULE_MINUTES')} دقیقه (۰ = خاموش)\n"
            f"🔹 آخرین آزمون: {latest['state'] if latest else 'هنوز اجرا نشده'}\n\n"
            "⚠️ مشکل حساب Google نتیجه‌ای درباره سلامت IP نیست.\n"
            "🔒 هیچ دستور تغییری به Xray یا PasarGuard نود ارسال نمی‌شود."
        )
        await self.view(update, text, markup(
            [("🚀 شروع", "run:all"), ("⏹ توقف", "run:stop")],
            [("🏠 منوی اصلی", "home")]
        ))

    async def ports(self, update: Update, offset: int):
        ports = self.store.list_ports()
        offset = max(0, min(offset, max(0, len(ports) - 1)))
        rows = []
        for port, enabled in ports[offset:offset + 8]:
            rows.append([(f"{'🟢' if enabled else '⚪'} {port}", f"ports:toggle:{port}"),
                         ("🗑", f"ports:delete:{port}")])
        page_controls = []
        if offset > 0:
            page_controls.append(("◀️ قبلی", f"ports:{max(0, offset - 8)}"))
        if offset + 8 < len(ports):
            page_controls.append(("بعدی ▶️", f"ports:{offset + 8}"))
        if page_controls:
            rows.append(page_controls)
        rows.extend([
            [("➕ افزودن پورت/رنج", "ports:add"), ("✏️ جایگزینی لیست", "ports:replace")],
            [("🧪 تست یک پورت", "ports:test"), ("🏠 منوی اصلی", "home")],
        ])
        await self.view(update,
            "🔌 <b>مدیریت پورت‌ها</b>\n\n"
            f"تعداد: {len(ports)} | فعال: {sum(v for _, v in ports)}\n"
            "روی شماره پورت بزنید تا فعال/غیرفعال شود. 🗑 برای حذف.\n\n"
            "نمونه ورودی: <code>18881-18890,25001,25003</code>\n"
            "در حالت VLESS، یک لینک با پورت‌های مختلف تست می‌شود.",
            markup(*rows))

    async def sources(self, update: Update):
        mode = self.store.get("MODE")
        subs = self.store.list_subscriptions()
        has_vless = bool(self.store.get("VLESS_URL"))
        rows = [
            [("🔘 VLESS" + (" ✅" if mode == "vless" else ""), "sources:mode:vless"),
             ("🔘 Subscription" + (" ✅" if mode == "subscription" else ""), "sources:mode:subscription")],
            [("🔑 تنظیم لینک VLESS", "sources:vless"), ("➕ سابسکریپشن", "sources:add")],
            [("🔄 به‌روزرسانی منابع", "sources:refresh"), ("📃 فهرست لینک‌ها", "sources:list")],
            [("🏠 منوی اصلی", "home")],
        ]
        await self.view(update,
            "🔗 <b>منابع تست</b>\n\n"
            f"حالت انتخابی: <b>{mode}</b>\n"
            f"VLESS: <b>{'ثبت شده ✅' if has_vless else 'ثبت نشده ❌'}</b>\n"
            f"اشتراک‌ها: <b>{len(subs)}</b>\n"
            "لینک‌های حساس در پنل نمایش داده نمی‌شوند.\n"
            "در حالت Subscription، هر VLESS ذخیره‌شده پورت خودش را دارد.\n"
            "💡 سابسکریپشن را بعد از افزودن Refresh کنید.",
            markup(*rows))

    async def subscriptions(self, update: Update):
        subs = self.store.list_subscriptions()
        rows = []
        for sub in subs[:20]:
            label = short(safe_source_label(sub["url"]), 24)
            rows.append([(f"🔗 #{sub['id']} {label} ({sub['nodes']})", "noop"),
                         ("🗑", f"sources:confirmdelete:{sub['id']}")])
        rows.extend([[("🔄 Refresh", "sources:refresh"), ("➕ افزودن", "sources:add")],
                     [("🔙 برگشت", "sources")]])
        await self.view(update,
            "📃 <b>سابسکریپشن‌ها</b>\n"
            "تعداد روبه‌روی هر لینک، نود VLESS قابل‌تستِ کش‌شده است.\n"
            "برای امنیت، فقط دامنه و بخشی از مسیر نشان داده می‌شود.",
            markup(*rows))

    async def settings(self, update: Update, page: int):
        page = max(0, min(page, (len(SETTINGS_KEYS) - 1) // 6))
        keys = SETTINGS_KEYS[page * 6:(page + 1) * 6]
        rows = [[(EDITABLE_SETTINGS[key][0], f"settings:edit:{key}")] for key in keys]
        rows.append([
            ("◀️", f"settings:{max(0, page - 1)}"),
            (f"{page + 1}/{(len(SETTINGS_KEYS) + 5)//6}", "noop"),
            ("▶️", f"settings:{min((len(SETTINGS_KEYS)-1)//6, page+1)}"),
        ])
        rows.append([("🏠 منوی اصلی", "home")])
        values = "\n".join(
            f"• {html.escape(EDITABLE_SETTINGS[k][0])}: <code>{html.escape(self.store.get(k))}</code>"
            for k in keys)
        await self.view(update, "⚙️ <b>تنظیمات تست</b>\n\n" + values +
                        "\n\nبرای تغییر، روی دکمه همان گزینه بزنید. همه تغییرها پایدارند.",
                        markup(*rows))

    async def login(self, update: Update):
        await self.view(update,
            "🔐 <b>ورود تصویری به Google</b>\n\n"
            "۱) از ربات «بازکردن مرورگر» را بزنید.\n"
            "۲) از لپ‌تاپتان این SSH Tunnel را اجرا کنید:\n"
            "<code>ssh -L 6080:127.0.0.1:6080 root@YOUR_SERVER_IP</code>\n"
            "۳) در مرورگر لپ‌تاپ <code>http://127.0.0.1:6080/vnc.html</code> را باز کنید.\n"
            "۴) خودتان به Google وارد شوید و وضعیت Gemini را ببینید.\n"
            "۵) «بستن و ذخیره نشست» را بزنید، بعد تست‌ها را شروع کنید.\n\n"
            "🔒 پورت ۶۰۸۰ فقط روی localhost سرور منتشر شده.\n"
            "⚠️ در صورت نمایش «Review service restriction»، ابتدا مشکل رسمی حساب را حل کنید.",
            markup(
                [("🟢 بازکردن مرورگر", "login:open")],
                [("🔴 بستن و ذخیره نشست", "login:close")],
                [("🏠 منوی اصلی", "home")]))

    async def reports(self, update: Update, run_id: int | None = None,
                      group: str = "all", page: int = 0):
        run = self.store.run(run_id) if run_id else self.store.latest_run()
        if not run:
            await self.view(update, "📊 هنوز گزارشی ثبت نشده است.",
                            markup([("🏠 منوی اصلی", "home")]))
            return
        results = self.store.run_entries(run["id"])
        stats = summary(run, results)
        try:
            selected = filtered(results, group)
        except ValueError:
            group, selected = "all", results
        per_page = 12
        last_page = max(0, (len(selected) - 1) // per_page)
        page = max(0, min(page, last_page))
        visible = selected[page * per_page:(page + 1) * per_page]
        sample = "\n".join(
            f"{STATUS_ICON.get(r['status'],'🔎')} "
            f"<code>{r['port']}</code> {html.escape(r['status'])} "
            f"(تلاش {r['attempts']})"
            for r in visible
        ) or "در این دسته پورتی وجود ندارد."
        image_rows = []
        for r in visible:
            if r["screenshot"]:
                image_rows.append((f"📸 {r['port']}",
                                   f"reports:image:{run['id']}:{r['sequence']}"))
        photos = [image_rows[i:i + 3] for i in range(0, min(len(image_rows), 6), 3)]
        controls = []
        if page:
            controls.append(("◀️ قبلی", f"reports:list:{run['id']}:{group}:{page-1}"))
        controls.append((f"صفحه {page+1}/{last_page+1}", "noop"))
        if page < last_page:
            controls.append(("بعدی ▶️", f"reports:list:{run['id']}:{group}:{page+1}"))
        buttons = [
            [("📋 همه", f"reports:list:{run['id']}:all:0"),
             ("✅ سالم", f"reports:list:{run['id']}:ok:0")],
            [("🚫 منطقه‌ای", f"reports:list:{run['id']}:blocked:0"),
             ("⚠️ بررسی", f"reports:list:{run['id']}:review:0")],
            [("⏳ تست‌نشده", f"reports:list:{run['id']}:pending:0")],
            controls,
        ] + photos + [
            [("📄 دانلود CSV", f"reports:csv:{run['id']}"),
             ("📚 تاریخچه", "reports:history")],
            [("🔄 تازه‌سازی", f"reports:list:{run['id']}:{group}:{page}"),
             ("🏠 منوی اصلی", "home")]
        ]
        await self.view(update,
            f"📊 <b>گزارش #{run['id']}</b>\n"
            f"وضعیت: {html.escape(run['state'])}\n"
            f"شروع: <code>{run['started_at']}</code>\n"
            f"🧪 تست‌شده: {stats.done}/{stats.total}\n"
            f"✅ سالم: {stats.ok} | 🚫 منطقه‌ای: {stats.blocked}\n"
            f"⚠️ نیازمند بررسی: {stats.review} | ⏳ باقی: {stats.pending}\n\n"
            f"<b>فهرست {html.escape(group)} ({len(selected)} مورد):</b>\n"
            + sample,
            markup(*buttons))

    async def history(self, update: Update):
        rows = [[(f"📊 #{r['id']} | {r['state']} | {r['completed']}/{r['total']}",
                  f"reports:view:{r['id']}")] for r in self.store.history()]
        rows.append([("🔙 آخرین گزارش", "reports")])
        await self.view(update, "📚 <b>تاریخچه ده اجرای آخر</b>\nگزارش موردنظر را انتخاب کنید.",
                        markup(*rows))

    async def send_csv(self, update: Update, run_id: int | None = None):
        run = self.store.run(run_id) if run_id else self.store.latest_run()
        if not run:
            await update.effective_chat.send_message("گزارشی موجود نیست.")
            return
        buffer = io.StringIO()
        fieldnames = ["sequence", "name", "port", "status", "detail", "attempts", "created_at"]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(self.store.run_entries(run["id"]))
        document = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))
        document.name = f"guardian-run-{run['id']}.csv"
        await update.effective_chat.send_document(
            document=document, filename=document.name,
            caption=f"📊 Gemini Guardian #{run['id']}")

    async def send_latest_photo(self, update: Update, run_id: int | None = None,
                                sequence: int | None = None):
        run = self.store.run(run_id) if run_id else self.store.latest_run()
        if not run:
            await update.effective_chat.send_message("هنوز تصویری نیست.")
            return
        images = [r for r in self.store.results(run["id"]) if r["screenshot"]]
        if not images:
            await update.effective_chat.send_message("در این گزارش تصویری ذخیره نشده.")
            return
        image = next((r for r in images if r["sequence"] == sequence), None) if sequence else images[-1]
        if image is None:
            await update.effective_chat.send_message("تصویر انتخاب‌شده وجود ندارد.")
            return
        try:
            with open(image["screenshot"], "rb") as f:
                await update.effective_chat.send_photo(
                    photo=f, caption=f"📸 {image['name']} | {image['status']}")
        except OSError:
            await update.effective_chat.send_message("تصویر روی دیسک پیدا نشد.")

    async def prompt(self, update: Update, context, field: str, text: str):
        context.user_data["pending"] = field
        await update.effective_chat.send_message(
            text + "\n\nبرای لغو /cancel را بزنید.", parse_mode="HTML")

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.allowed(update):
            return
        query = update.callback_query
        action = query.data or ""
        await query.answer()
        if action == "noop":
            return
        if action == "home":
            return await self.home(update)
        if action == "status":
            return await self.status(update)
        if action == "login":
            return await self.login(update)
        if action == "login:open":
            _, message = await asyncio.to_thread(self.manager.start_login)
            await update.effective_chat.send_message(message)
            return await self.login(update)
        if action == "login:close":
            message = await asyncio.to_thread(self.manager.stop_login)
            await update.effective_chat.send_message(message)
            return await self.login(update)
        if action == "run:all":
            okay, message = await asyncio.to_thread(self.manager.start)
            await update.effective_chat.send_message(message)
            return await self.home(update)
        if action == "run:stop":
            message = self.manager.stop()
            await update.effective_chat.send_message(message)
            return await self.home(update)
        if action == "reports":
            return await self.reports(update)
        if action == "reports:history":
            return await self.history(update)
        if action.startswith("reports:list:"):
            fields = action.split(":")
            if len(fields) == 5:
                return await self.reports(update, int(fields[2]), fields[3], int(fields[4]))
        if action.startswith("reports:view:"):
            return await self.reports(update, int(action.rsplit(":", 1)[1]))
        if action == "reports:csv":
            return await self.send_csv(update)
        if action.startswith("reports:csv:"):
            return await self.send_csv(update, int(action.rsplit(":", 1)[1]))
        if action == "reports:photo":
            return await self.send_latest_photo(update)
        if action.startswith("reports:image:"):
            _, _, run_id, sequence = action.split(":")
            return await self.send_latest_photo(update, int(run_id), int(sequence))
        if action.startswith("ports:"):
            sub = action.split(":")
            if len(sub) == 2 and sub[1].isdigit():
                return await self.ports(update, int(sub[1]))
            if action in ("ports:add", "ports:replace", "ports:remove", "ports:test"):
                field = action.replace(":", ".")
                desc = {
                    "ports:add": "➕ پورت یا رنج را وارد کنید؛ مثال <code>18881-18890,22001</code>",
                    "ports:replace": "✏️ لیست کامل پورت‌های جدید را وارد کنید؛ این کار جایگزین لیست فعلی می‌شود.",
                    "ports:remove": "🗑 پورت یا رنج را وارد کنید.",
                    "ports:test": "🧪 شماره یک پورت فعال را وارد کنید.",
                }[action]
                return await self.prompt(update, context, field, desc)
            if len(sub) == 3 and sub[1] in {"toggle", "delete"}:
                port = int(sub[2])
                if sub[1] == "toggle":
                    self.store.toggle_port(port)
                else:
                    self.store.remove_port(port)
                return await self.ports(update, 0)
        if action == "sources":
            return await self.sources(update)
        if action == "sources:list":
            return await self.subscriptions(update)
        if action == "sources:vless":
            return await self.prompt(update, context, "sources.vless",
                                     "🔑 لینک کامل <code>vless://</code> را ارسال کنید. "
                                     "پیام دریافتی تا حد امکان حذف می‌شود.")
        if action == "sources:add":
            return await self.prompt(update, context, "sources.add",
                                     "🔗 لینک HTTP(S) سابسکریپشن را ارسال کنید.")
        if action.startswith("sources:mode:"):
            mode = action.rsplit(":", 1)[1]
            if mode in {"vless", "subscription"}:
                self.store.set("MODE", mode)
            return await self.sources(update)
        if action == "sources:refresh":
            await update.effective_chat.send_message("🔄 در حال دریافت و اعتبارسنجی سابسکریپشن‌ها…")
            total, failed = 0, 0
            for sub in self.store.list_subscriptions():
                try:
                    raw = await asyncio.to_thread(fetch_subscription, sub["url"])
                    targets, skipped = parse_subscription(raw)
                    self.store.save_subscription_targets(sub["id"], targets)
                    total += len(targets)
                except Exception:
                    failed += 1
            await update.effective_chat.send_message(
                f"✅ {total} نود VLESS معتبر ذخیره شد. ⚠️ منابع ناموفق: {failed}")
            return await self.sources(update)
        if action.startswith("sources:confirmdelete:"):
            sub_id = int(action.rsplit(":", 1)[1])
            return await self.view(update, f"🗑 حذف سابسکریپشن #{sub_id} و فهرست نودهای ذخیره‌شده آن؟",
                markup([("✅ حذف", f"sources:delete:{sub_id}"),
                        ("❌ انصراف", "sources:list")]))
        if action.startswith("sources:delete:"):
            self.store.remove_subscription(int(action.rsplit(":", 1)[1]))
            return await self.subscriptions(update)
        if action.startswith("settings:"):
            sub = action.split(":")
            if len(sub) == 2 and sub[1].isdigit():
                return await self.settings(update, int(sub[1]))
            if len(sub) == 3 and sub[1] == "edit" and sub[2] in EDITABLE_SETTINGS:
                key = sub[2]
                title, desc = EDITABLE_SETTINGS[key]
                return await self.prompt(
                    update, context, "settings." + key,
                    f"{html.escape(title)}\n{html.escape(desc)}\n"
                    f"مقدار فعلی: <code>{html.escape(self.store.get(key))}</code>")
        await query.message.reply_text("دستور ناشناخته؛ /start")

    async def input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.allowed(update):
            return
        pending = context.user_data.get("pending")
        if not pending:
            await self.home(update)
            return
        value = (update.message.text or "").strip()
        is_secret = pending in {"sources.vless", "sources.add"}
        if is_secret:
            try:
                await update.message.delete()
            except Exception:
                pass
        try:
            if pending == "ports.add":
                added = self.store.add_ports(ports_from_spec(value))
                text = f"✅ {added} پورت جدید اضافه شد."
            elif pending == "ports.replace":
                ports = ports_from_spec(value)
                self.store.replace_ports(ports)
                text = f"✅ فهرست با {len(ports)} پورت جایگزین شد."
            elif pending == "ports.remove":
                ports = ports_from_spec(value)
                for port in ports:
                    self.store.remove_port(port)
                text = f"✅ {len(ports)} پورت از لیست حذف شد (در صورت وجود)."
            elif pending == "ports.test":
                port = int(value)
                targets = [t for t in targets_for_run(self.store) if t.port == port]
                if not targets:
                    raise ValueError("پورت فعال و قابل‌تست پیدا نشد.")
                okay, text = await asyncio.to_thread(self.manager.start, targets[:1])
            elif pending == "sources.vless":
                outbound_from_url(value)
                self.store.set("VLESS_URL", value)
                text = "✅ لینک VLESS ذخیره شد؛ مقدار حساس در پنل نمایش داده نمی‌شود."
            elif pending == "sources.add":
                url = await asyncio.to_thread(validate_subscription_url, value)
                if len(self.store.list_subscriptions()) >= 20:
                    raise ValueError("حداکثر ۲۰ سابسکریپشن مجاز است.")
                self.store.add_subscription(url)
                text = "✅ آدرس ثبت شد. اکنون «به‌روزرسانی منابع» را بزنید."
            elif pending.startswith("settings."):
                key = pending.split(".", 1)[1]
                parsed = validate_setting(key, value)
                self.store.set(key, parsed)
                text = f"✅ {EDITABLE_SETTINGS[key][0]} = {parsed}"
            else:
                raise ValueError("فرم منقضی شده است.")
        except (ValueError, OverflowError) as exc:
            # Do not print untrusted URI content in a Telegram error.
            await update.effective_chat.send_message(f"⚠️ {short(str(exc), 180)}\nدوباره تلاش کنید یا /cancel.")
            return
        context.user_data.pop("pending", None)
        await update.effective_chat.send_message(text)
        await self.home(update)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.allowed(update):
            return
        context.user_data.pop("pending", None)
        await update.message.reply_text("✅ عملیات لغو شد.")
        await self.home(update)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        # Exception details might contain subscription tokens; log class only.
        log.error("Telegram handler failed: %s", type(context.error).__name__)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await update.effective_chat.send_message(
                    "⚠️ خطای داخلی رخ داد. گزارش‌های ذخیره‌شده محفوظ‌اند؛ /start")
            except Exception:
                pass

    def run(self):
        self.app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
