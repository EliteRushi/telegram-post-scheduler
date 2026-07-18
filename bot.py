"""
Telegram Post Scheduler Bot
Owner-only bot for scheduling posts to a Telegram channel.
Runs in two modes:
  - Interactive bot mode (default): python bot.py
  - Scheduler mode (GitHub Actions cron): python bot.py --scheduler
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import pytz
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("scheduler-bot")

# ---------------------------------------------------------------------------
# Config & Secrets
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALLOWED_USERS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USERS", "").split(",")
    if uid.strip()
}
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()

CONFIG_FILE = "config.json"


def load_config() -> Dict[str, Any]:
    """Load non-secret configuration from config.json."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("Could not load %s (%s). Using defaults.", CONFIG_FILE, e)
        return {
            "timezone": "Asia/Kolkata",
            "storage_file": "posts.json",
            "max_posts_per_schedule": 100,
            "default_intervals": {
                "seconds": [30, 60, 120],
                "minutes": [1, 5, 15, 30, 60],
                "hours": [1, 2, 6, 12, 24],
                "days": [1, 2, 7, 30],
            },
        }


CONFIG = load_config()
TIMEZONE = pytz.timezone(CONFIG.get("timezone", "Asia/Kolkata"))
STORAGE_FILE = CONFIG.get("storage_file", "posts.json")
MAX_POSTS = int(CONFIG.get("max_posts_per_schedule", 100))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class PostStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class ScheduleStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MediaType(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    NONE = "none"


# Conversation states
(
    ASK_COUNT,
    ASK_CUSTOM_COUNT,
    ASK_MEDIA,
    ASK_URL,
    ASK_BTN_TEXT,
    ASK_CAPTION,
    ASK_UNIT,
    ASK_INTERVAL,
    ASK_DATE,
    ASK_TIME,
    CONFIRM,
    EDIT_PICK,
) = range(12)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class Storage:
    """Thread-safe (single process) JSON storage for schedules and posts."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _read(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {"schedules": []}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "schedules" not in data:
                    data["schedules"] = []
                return data
        except json.JSONDecodeError:
            logger.error("posts.json corrupted. Backing up and starting fresh.")
            backup = f"{self.path}.corrupt.{int(datetime.now().timestamp())}"
            try:
                os.rename(self.path, backup)
            except OSError:
                pass
            return {"schedules": []}

    def _write(self, data: Dict[str, Any]) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def all_schedules(self) -> List[Dict[str, Any]]:
        return self._read().get("schedules", [])

    def get_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        for s in self.all_schedules():
            if s["schedule_id"] == schedule_id:
                return s
        return None

    def add_schedule(self, schedule: Dict[str, Any]) -> None:
        data = self._read()
        data["schedules"].append(schedule)
        self._write(data)

    def update_schedule(self, schedule: Dict[str, Any]) -> None:
        data = self._read()
        for i, s in enumerate(data["schedules"]):
            if s["schedule_id"] == schedule["schedule_id"]:
                schedule["updated_at"] = datetime.now(TIMEZONE).isoformat()
                data["schedules"][i] = schedule
                self._write(data)
                return

    def delete_schedule(self, schedule_id: str) -> bool:
        data = self._read()
        before = len(data["schedules"])
        data["schedules"] = [s for s in data["schedules"] if s["schedule_id"] != schedule_id]
        self._write(data)
        return len(data["schedules"]) < before


storage = Storage(STORAGE_FILE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def owner_only(func):
    """Decorator: restrict handler to OWNER_ID only."""

   async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
    user = update.effective_user

    if not user or user.id not in ALLOWED_USERS:
        logger.warning(
            "Rejected unauthorized user: %s",
            user.id if user else "unknown"
        )
        return ConversationHandler.END if False else None

    return await func(update, context, *args, **kwargs)

    return wrapper


def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def parse_date(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s.strip(), "%d-%m-%Y")
    except ValueError:
        return None


def parse_time(s: str) -> Optional[tuple]:
    try:
        t = datetime.strptime(s.strip(), "%H:%M")
        return t.hour, t.minute
    except ValueError:
        return None


def interval_to_seconds(unit: str, value: int) -> int:
    return {
        "seconds": value,
        "minutes": value * 60,
        "hours": value * 3600,
        "days": value * 86400,
    }[unit]


def now_tz() -> datetime:
    return datetime.now(TIMEZONE)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Continue", callback_data="menu:continue")],
            [InlineKeyboardButton("📅 Schedule Posts", callback_data="menu:schedule")],
            [InlineKeyboardButton("📋 List Posts", callback_data="menu:list")],
            [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
        ]
    )


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------
@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 <b>Telegram Post Scheduler</b>\n\n"
        "Schedule posts to your channel with media, captions, and inline buttons.\n\n"
        "Choose an option below to begin."
    )
    await update.effective_message.reply_html(text, reply_markup=main_menu_kb())


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>Commands</b>\n"
        "/start - Main menu\n"
        "/help - This help\n"
        "/schedule - Start scheduling wizard\n"
        "/list - List all schedules\n"
        "/delete - Delete a schedule\n"
        "/edit - Edit a schedule\n"
        "/pause - Pause a schedule\n"
        "/resume - Resume a schedule\n"
        "/status - Bot & scheduler status\n"
    )
    await update.effective_message.reply_html(text)


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    schedules = storage.all_schedules()
    total = len(schedules)
    pending = sum(
        1
        for s in schedules
        for p in s.get("posts", [])
        if p.get("status") == PostStatus.PENDING.value
    )
    sent = sum(
        1
        for s in schedules
        for p in s.get("posts", [])
        if p.get("status") == PostStatus.SENT.value
    )
    text = (
        f"<b>Status</b>\n"
        f"Timezone: <code>{TIMEZONE}</code>\n"
        f"Channel: <code>{CHANNEL_ID}</code>\n"
        f"Schedules: <b>{total}</b>\n"
        f"Pending posts: <b>{pending}</b>\n"
        f"Sent posts: <b>{sent}</b>\n"
        f"Now: <code>{now_tz().strftime('%d-%m-%Y %H:%M:%S')}</code>"
    )
    await update.effective_message.reply_html(text)


@owner_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    schedules = storage.all_schedules()
    if not schedules:
        await update.effective_message.reply_text("No schedules yet. Use /schedule to create one.")
        return
    lines = ["<b>Your Schedules</b>\n"]
    for s in schedules:
        posts = s.get("posts", [])
        pending = sum(1 for p in posts if p["status"] == PostStatus.PENDING.value)
        lines.append(
            f"• <code>{s['schedule_id'][:8]}</code> | {s.get('status','pending')} | "
            f"{len(posts)} posts ({pending} pending) | start: {s.get('start_time','?')}"
        )
    await update.effective_message.reply_html("\n".join(lines))


@owner_only
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: /delete <schedule_id_prefix>")
        return
    prefix = args[0]
    for s in storage.all_schedules():
        if s["schedule_id"].startswith(prefix):
            storage.delete_schedule(s["schedule_id"])
            await update.effective_message.reply_text(f"Deleted schedule {s['schedule_id'][:8]}.")
            return
    await update.effective_message.reply_text("Schedule not found.")


@owner_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_schedule_status(update, context, ScheduleStatus.PAUSED)


@owner_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_schedule_status(update, context, ScheduleStatus.ACTIVE)


async def _set_schedule_status(update, context, new_status: ScheduleStatus) -> None:
    args = context.args
    if not args:
        await update.effective_message.reply_text(
            f"Usage: /{new_status.value} <schedule_id_prefix>"
        )
        return
    prefix = args[0]
    for s in storage.all_schedules():
        if s["schedule_id"].startswith(prefix):
            s["status"] = new_status.value
            # Pause/resume individual pending posts
            for p in s["posts"]:
                if new_status == ScheduleStatus.PAUSED and p["status"] == PostStatus.PENDING.value:
                    p["status"] = PostStatus.PAUSED.value
                elif new_status == ScheduleStatus.ACTIVE and p["status"] == PostStatus.PAUSED.value:
                    p["status"] = PostStatus.PENDING.value
            storage.update_schedule(s)
            await update.effective_message.reply_text(
                f"Schedule {s['schedule_id'][:8]} → {new_status.value}"
            )
            return
    await update.effective_message.reply_text("Schedule not found.")


@owner_only
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "To edit: /delete <id> then /schedule to recreate.\n"
        "Full in-place editing is not yet supported."
    )


# ---------------------------------------------------------------------------
# Menu callback (from /start buttons)
# ---------------------------------------------------------------------------
@owner_only
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    if action == "schedule" or action == "continue":
        await q.message.reply_text("Starting scheduling wizard… use /schedule")
    elif action == "list":
        await cmd_list(update, context)
    elif action == "help":
        await cmd_help(update, context)


# ---------------------------------------------------------------------------
# Scheduling Wizard (ConversationHandler)
# ---------------------------------------------------------------------------
@owner_only
async def wiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["wizard"] = {"posts": [], "current": 0, "total": 0}
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1", callback_data="cnt:1"),
                InlineKeyboardButton("2", callback_data="cnt:2"),
                InlineKeyboardButton("3", callback_data="cnt:3"),
            ],
            [
                InlineKeyboardButton("5", callback_data="cnt:5"),
                InlineKeyboardButton("10", callback_data="cnt:10"),
                InlineKeyboardButton("Custom", callback_data="cnt:custom"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cnt:cancel")],
        ]
    )
    await update.effective_message.reply_text("How many posts do you want to schedule?", reply_markup=kb)
    return ASK_COUNT


async def wiz_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":", 1)[1]
    if choice == "cancel":
        await q.message.reply_text("Cancelled.")
        return ConversationHandler.END
    if choice == "custom":
        await q.message.reply_text(f"Enter number of posts (1-{MAX_POSTS}):")
        return ASK_CUSTOM_COUNT
    total = int(choice)
    return await _begin_posts(update, context, total)


async def wiz_custom_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        total = int(update.message.text.strip())
        if total < 1 or total > MAX_POSTS:
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"Enter a valid number 1-{MAX_POSTS}:")
        return ASK_CUSTOM_COUNT
    return await _begin_posts(update, context, total)


async def _begin_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, total: int) -> int:
    context.user_data["wizard"]["total"] = total
    context.user_data["wizard"]["current"] = 1
    context.user_data["wizard"]["draft"] = {}
    await update.effective_message.reply_text(
        f"📸 Post 1/{total}\nSTEP 1: Send an image or video (or /skip)"
    )
    return ASK_MEDIA


async def wiz_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    draft = context.user_data["wizard"]["draft"]
    if msg.text and msg.text.strip().lower() == "/skip":
        draft["media_type"] = MediaType.NONE.value
        draft["file_id"] = None
    elif msg.photo:
        draft["media_type"] = MediaType.PHOTO.value
        draft["file_id"] = msg.photo[-1].file_id
    elif msg.video:
        draft["media_type"] = MediaType.VIDEO.value
        draft["file_id"] = msg.video.file_id
    else:
        await msg.reply_text("Please send a photo, video, or /skip.")
        return ASK_MEDIA
    await msg.reply_text("STEP 2: Enter the product URL (https://…)")
    return ASK_URL


async def wiz_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip()
    if not is_valid_url(url):
        await update.message.reply_text("Invalid URL. Try again (must start with http/https):")
        return ASK_URL
    context.user_data["wizard"]["draft"]["button_url"] = url
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Buy Now", callback_data="btn:Buy Now"),
                InlineKeyboardButton("Order", callback_data="btn:Order"),
            ],
            [InlineKeyboardButton("View Product", callback_data="btn:View Product")],
        ]
    )
    await update.message.reply_text(
        "STEP 3: Enter button text (or pick one below):", reply_markup=kb
    )
    return ASK_BTN_TEXT


async def wiz_btn_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        text = update.callback_query.data.split(":", 1)[1]
        target_msg = update.callback_query.message
    else:
        text = update.message.text.strip()
        target_msg = update.message
    if not text or len(text) > 64:
        await target_msg.reply_text("Button text must be 1-64 chars:")
        return ASK_BTN_TEXT
    context.user_data["wizard"]["draft"]["button_text"] = text
    await target_msg.reply_text("STEP 4: Enter caption (HTML supported):")
    return ASK_CAPTION


async def wiz_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    caption = update.message.text or ""
    if len(caption) > 1024:
        await update.message.reply_text("Caption too long (max 1024). Try again:")
        return ASK_CAPTION
    wiz = context.user_data["wizard"]
    draft = wiz["draft"]
    draft["caption"] = caption
    draft["post_id"] = str(uuid.uuid4())
    draft["status"] = PostStatus.PENDING.value
    draft["sent_time"] = None
    wiz["posts"].append(draft)
    idx = wiz["current"]
    await update.message.reply_text(f"✅ Post #{idx} saved")
    if idx < wiz["total"]:
        wiz["current"] += 1
        wiz["draft"] = {}
        await update.message.reply_text(
            f"📸 Post {wiz['current']}/{wiz['total']}\nSTEP 1: Send image/video (or /skip)"
        )
        return ASK_MEDIA
    return await _ask_unit(update, context)


async def _ask_unit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Seconds", callback_data="unit:seconds"),
                InlineKeyboardButton("Minutes", callback_data="unit:minutes"),
            ],
            [
                InlineKeyboardButton("Hours", callback_data="unit:hours"),
                InlineKeyboardButton("Days", callback_data="unit:days"),
            ],
        ]
    )
    await update.effective_message.reply_text("Select interval unit:", reply_markup=kb)
    return ASK_UNIT


async def wiz_unit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    unit = q.data.split(":", 1)[1]
    context.user_data["wizard"]["unit"] = unit
    presets = CONFIG.get("default_intervals", {}).get(unit, [1, 5, 10])
    rows = [
        [InlineKeyboardButton(str(v), callback_data=f"iv:{v}") for v in presets[:3]],
    ]
    if len(presets) > 3:
        rows.append([InlineKeyboardButton(str(v), callback_data=f"iv:{v}") for v in presets[3:6]])
    await q.message.reply_text(
        f"Enter interval value in {unit} (or tap a preset):",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return ASK_INTERVAL


async def wiz_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        val_str = update.callback_query.data.split(":", 1)[1]
        target = update.callback_query.message
    else:
        val_str = update.message.text.strip()
        target = update.message
    try:
        value = int(val_str)
        if value <= 0:
            raise ValueError
    except ValueError:
        await target.reply_text("Enter a positive integer:")
        return ASK_INTERVAL
    context.user_data["wizard"]["interval_value"] = value
    await target.reply_text("Enter Start Date (DD-MM-YYYY):")
    return ASK_DATE


async def wiz_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("Invalid date. Use DD-MM-YYYY:")
        return ASK_DATE
    context.user_data["wizard"]["start_date"] = d
    await update.message.reply_text("Enter Start Time (HH:MM, 24h, Asia/Kolkata):")
    return ASK_TIME


async def wiz_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = parse_time(update.message.text)
    if not t:
        await update.message.reply_text("Invalid time. Use HH:MM:")
        return ASK_TIME
    wiz = context.user_data["wizard"]
    d = wiz["start_date"]
    start_dt = TIMEZONE.localize(datetime(d.year, d.month, d.day, t[0], t[1]))
    wiz["start_dt"] = start_dt
    unit = wiz["unit"]
    val = wiz["interval_value"]
    summary = (
        f"<b>Confirm Schedule</b>\n"
        f"Posts: <b>{len(wiz['posts'])}</b>\n"
        f"Start Date: <code>{start_dt.strftime('%d-%m-%Y')}</code>\n"
        f"Start Time: <code>{start_dt.strftime('%H:%M')}</code> ({TIMEZONE})\n"
        f"Interval: <b>{val} {unit}</b>\n"
        f"Channel: <code>{CHANNEL_ID}</code>"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Schedule", callback_data="cf:ok"),
                InlineKeyboardButton("❌ Cancel", callback_data="cf:cancel"),
            ]
        ]
    )
    await update.message.reply_html(summary, reply_markup=kb)
    return CONFIRM


async def wiz_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    if action == "cancel":
        await q.message.reply_text("Cancelled.")
        context.user_data.pop("wizard", None)
        return ConversationHandler.END
    wiz = context.user_data["wizard"]
    schedule = {
        "schedule_id": str(uuid.uuid4()),
        "owner_id": OWNER_ID,
        "channel_id": CHANNEL_ID,
        "created_at": now_tz().isoformat(),
        "updated_at": now_tz().isoformat(),
        "start_time": wiz["start_dt"].isoformat(),
        "interval": {"unit": wiz["unit"], "value": wiz["interval_value"]},
        "status": ScheduleStatus.ACTIVE.value,
        "posts": wiz["posts"],
    }
    storage.add_schedule(schedule)
    await q.message.reply_text(
        f"✅ Schedule saved: {schedule['schedule_id'][:8]}\n"
        f"GitHub Actions will publish posts automatically."
    )
    context.user_data.pop("wizard", None)
    return ConversationHandler.END


async def wiz_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("wizard", None)
    await update.effective_message.reply_text("Wizard cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Scheduler (runs from GitHub Actions)
# ---------------------------------------------------------------------------
async def send_post(app: Application, channel_id: str, post: Dict[str, Any]) -> bool:
    """Send a single post to the channel."""
    try:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(post["button_text"], url=post["button_url"])]]
        )
        media_type = post.get("media_type", MediaType.NONE.value)
        caption = post.get("caption", "")
        if media_type == MediaType.PHOTO.value and post.get("file_id"):
            await app.bot.send_photo(
                chat_id=channel_id,
                photo=post["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        elif media_type == MediaType.VIDEO.value and post.get("file_id"):
            await app.bot.send_video(
                chat_id=channel_id,
                video=post["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        else:
            await app.bot.send_message(
                chat_id=channel_id,
                text=caption or post["button_text"],
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
                disable_web_page_preview=False,
            )
        return True
    except TelegramError as e:
        logger.error("Telegram send failed: %s", e)
        return False


def post_due_time(schedule: Dict[str, Any], index: int) -> datetime:
    """Compute due time for post at given index."""
    start = datetime.fromisoformat(schedule["start_time"])
    if start.tzinfo is None:
        start = TIMEZONE.localize(start)
    secs = interval_to_seconds(schedule["interval"]["unit"], schedule["interval"]["value"])
    return start + timedelta(seconds=secs * index)


async def run_scheduler() -> None:
    """Scheduler entry: check due posts and publish."""
    if not BOT_TOKEN or not CHANNEL_ID:
        logger.error("Missing BOT_TOKEN or CHANNEL_ID. Aborting scheduler.")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    await app.initialize()
    now = now_tz()
    logger.info("Scheduler run @ %s", now.isoformat())
    schedules = storage.all_schedules()
    changed = False
    for schedule in schedules:
        if schedule.get("status") not in (ScheduleStatus.ACTIVE.value, ScheduleStatus.PENDING.value):
            continue
        posts = schedule.get("posts", [])
        for idx, post in enumerate(posts):
            if post["status"] != PostStatus.PENDING.value:
                continue
            due = post_due_time(schedule, idx)
            if due <= now:
                logger.info("Sending post %s (due %s)", post["post_id"][:8], due.isoformat())
                ok = await send_post(app, schedule["channel_id"], post)
                if ok:
                    post["status"] = PostStatus.SENT.value
                    post["sent_time"] = now.isoformat()
                    changed = True
                else:
                    logger.warning("Post %s failed; will retry next run.", post["post_id"][:8])
        # Mark schedule complete if all posts done
        if posts and all(p["status"] != PostStatus.PENDING.value for p in posts):
            if any(p["status"] == PostStatus.SENT.value for p in posts):
                schedule["status"] = ScheduleStatus.COMPLETED.value
                changed = True
        if changed:
            storage.update_schedule(schedule)
    await app.shutdown()
    logger.info("Scheduler done.")


# ---------------------------------------------------------------------------
# Bot runner
# ---------------------------------------------------------------------------
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("schedule", wiz_start)],
        states={
            ASK_COUNT: [CallbackQueryHandler(wiz_count, pattern=r"^cnt:")],
            ASK_CUSTOM_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_custom_count)],
            ASK_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO, wiz_media),
                CommandHandler("skip", wiz_media),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_media),
            ],
            ASK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_url)],
            ASK_BTN_TEXT: [
                CallbackQueryHandler(wiz_btn_text, pattern=r"^btn:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_btn_text),
            ],
            ASK_CAPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_caption)],
            ASK_UNIT: [CallbackQueryHandler(wiz_unit, pattern=r"^unit:")],
            ASK_INTERVAL: [
                CallbackQueryHandler(wiz_interval, pattern=r"^iv:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_interval),
            ],
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_date)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, wiz_time)],
            CONFIRM: [CallbackQueryHandler(wiz_confirm, pattern=r"^cf:")],
        },
        fallbacks=[CommandHandler("cancel", wiz_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    return app


def run_bot() -> None:
    if not BOT_TOKEN or not OWNER_ID:
        logger.error("BOT_TOKEN and OWNER_ID are required.")
        sys.exit(1)
    app = build_application()
    logger.info("Bot started. Owner=%s Channel=%s TZ=%s", OWNER_ID, CHANNEL_ID, TIMEZONE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduler", action="store_true", help="Run scheduler once and exit")
    args = parser.parse_args()
    if args.scheduler:
        asyncio.run(run_scheduler())
    else:
        run_bot()


if __name__ == "__main__":
    main()
