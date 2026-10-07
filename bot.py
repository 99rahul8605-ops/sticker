import asyncio
import hashlib
import html
import io
import logging
import os
import re
import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ChatPermissions,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedSticker,
    InputTextMessageContent,
    Message,
    Update,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MONGO_URI = os.environ.get("MONGO_URI", "").strip()
DB_NAME = os.environ.get("DB_NAME", "sticker_guard").strip()
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0") or 0)
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
_raw_webhook_url = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
if _raw_webhook_url and "://" not in _raw_webhook_url:
    _raw_webhook_url = f"https://{_raw_webhook_url}"
WEBHOOK_URL = _raw_webhook_url or (f"https://{RENDER_EXTERNAL_HOSTNAME}" if RENDER_EXTERNAL_HOSTNAME else "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
WEBHOOK_ENABLED = bool(WEBHOOK_URL and WEBHOOK_SECRET)
PORT = int(os.environ.get("PORT", "10000"))
STARTED_AT = datetime.now(timezone.utc)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing")
if not BOT_OWNER_ID:
    raise RuntimeError("BOT_OWNER_ID is missing or invalid")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("sticker_guard")

mongo = AsyncMongoClient(MONGO_URI)
db = mongo[DB_NAME]

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

BOT_ID = 0
BOT_USERNAME = ""

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
PACK_RE = re.compile(r"(?:https?://)?t\.me/(?:addstickers|addemoji)/([A-Za-z0-9_]+)", re.I)
CODE_RE = re.compile(r"^[A-Z0-9_]{3,12}$")
DEFAULT_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TUTORIAL_TTL = 300
INLINE_HELP_MARKER = "[StickerGuard:help]"
INLINE_INVALID_MARKER = "[StickerGuard:invalid]"

# NudeNet runs locally on the bot server. The 320px bundled model is used by
# default to keep CPU/RAM usage reasonable on small Render instances.
NSFW_BLOCK_LABELS = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
}
NSFW_DEFAULT_THRESHOLD = 0.85
NSFW_DEFAULT_MUTE_SECONDS = 3600
NSFW_NOTICE_TTL = 60
NSFW_MAX_FILE_BYTES = 10 * 1024 * 1024
NSFW_CACHE_VERSION = "nudenet-320-v1"

# In-memory burst limiter and tutorial anti-spam state. Mongo stores long-term stats.
rate_windows: dict[tuple[int, int], deque[float]] = defaultdict(deque)
tutorial_last_sent: dict[tuple[int, int], float] = {}
tutorial_message_ids: dict[tuple[int, int], int] = {}
_nsfw_detector: Any | None = None
_nsfw_detector_failed = False
_nsfw_load_lock = asyncio.Lock()
_nsfw_inference_lock = asyncio.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_key() -> str:
    return utcnow().strftime("%Y-%m-%d")


def clean_pack_input(value: str) -> str:
    value = value.strip()
    match = PACK_RE.search(value)
    return match.group(1) if match else value.split()[0]


def command_arg(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def generate_unique_group_code(length: int = 5) -> str:
    for _ in range(100):
        code = "".join(secrets.choice(DEFAULT_CODE_ALPHABET) for _ in range(length))
        if not await db.groups.find_one({"group_code": code}, {"_id": 1}):
            return code
    raise RuntimeError("Could not generate a unique group code")


async def init_db() -> None:
    await mongo.admin.command({"ping": 1})
    await db.groups.create_index([("chat_id", ASCENDING)], unique=True)
    await db.groups.create_index([("group_code", ASCENDING)], unique=True, sparse=True)
    await db.users.create_index([("user_id", ASCENDING)], unique=True)
    await db.packs.create_index([("name", ASCENDING)], unique=True)
    await db.group_packs.create_index([("chat_id", ASCENDING), ("pack_name", ASCENDING)], unique=True)
    await db.managers.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
    await db.usage.create_index([("chat_id", ASCENDING), ("kind", ASCENDING), ("key", ASCENDING)], unique=True)
    await db.daily.create_index([("day", ASCENDING), ("chat_id", ASCENDING)], unique=True)
    await db.activity.create_index([("created_at", DESCENDING)])
    await db.activity.create_index("expire_at", expireAfterSeconds=0)
    await db.errors.create_index([("created_at", DESCENDING)])
    await db.errors.create_index("expire_at", expireAfterSeconds=0)
    await db.nsfw_cache.create_index([("media_key", ASCENDING)], unique=True)
    await db.nsfw_cache.create_index([("updated_at", DESCENDING)])

    # Existing groups get safe defaults without requiring a DB reset.
    await db.groups.update_many(
        {"nsfw_enabled": {"$exists": False}},
        {"$set": {
            "nsfw_enabled": True,
            "nsfw_action": "mute",
            "nsfw_mute_seconds": NSFW_DEFAULT_MUTE_SECONDS,
            "nsfw_threshold": NSFW_DEFAULT_THRESHOLD,
        }},
    )

    # Backfill codes for groups created by older versions of the bot.
    async for row in db.groups.find({"group_code": {"$exists": False}}, {"chat_id": 1}):
        while True:
            code = await generate_unique_group_code()
            try:
                await db.groups.update_one(
                    {"chat_id": row["chat_id"], "group_code": {"$exists": False}},
                    {"$set": {"group_code": code}},
                )
                break
            except DuplicateKeyError:
                continue


async def ensure_group(chat: Any) -> dict[str, Any]:
    now = utcnow()
    existing = await db.groups.find_one({"chat_id": chat.id})
    if existing:
        updates = {
            "title": getattr(chat, "title", None) or str(chat.id),
            "username": getattr(chat, "username", None),
            "active": True,
            "last_seen": now,
        }
        if not existing.get("group_code"):
            while True:
                code = await generate_unique_group_code()
                try:
                    await db.groups.update_one({"chat_id": chat.id}, {"$set": {**updates, "group_code": code}})
                    break
                except DuplicateKeyError:
                    continue
        else:
            await db.groups.update_one({"chat_id": chat.id}, {"$set": updates})
        return await db.groups.find_one({"chat_id": chat.id}) or {}

    while True:
        code = await generate_unique_group_code()
        doc = {
            "chat_id": chat.id,
            "title": getattr(chat, "title", None) or str(chat.id),
            "username": getattr(chat, "username", None),
            "active": True,
            "last_seen": now,
            "created_at": now,
            "group_code": code,
            "limit_count": 5,
            "limit_window": 30,
            "nsfw_enabled": True,
            "nsfw_action": "mute",
            "nsfw_mute_seconds": NSFW_DEFAULT_MUTE_SECONDS,
            "nsfw_threshold": NSFW_DEFAULT_THRESHOLD,
            "stats": {
                "inline_queries": 0,
                "stickers_sent": 0,
                "deleted_stickers": 0,
                "deleted_inline": 0,
                "rate_deleted": 0,
                "packs_added": 0,
                "packs_removed": 0,
                "nsfw_deleted": 0,
                "nsfw_actions": 0,
            },
        }
        try:
            await db.groups.insert_one(doc)
            return doc
        except DuplicateKeyError:
            # Another update may have created this chat at the same time.
            existing = await db.groups.find_one({"chat_id": chat.id})
            if existing:
                return existing


async def touch_dm_user(user: Any) -> None:
    if not user:
        return
    now = utcnow()
    await db.users.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "last_seen": now,
            },
            "$setOnInsert": {"first_seen": now},
        },
        upsert=True,
    )


async def log_activity(event: str, *, chat: Any | None = None, user: Any | None = None, details: str = "") -> None:
    now = utcnow()
    await db.activity.insert_one(
        {
            "event": event,
            "chat_id": getattr(chat, "id", None),
            "chat_title": getattr(chat, "title", None),
            "user_id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "details": details[:500],
            "created_at": now,
            "expire_at": now + timedelta(days=30),
        }
    )


async def bump_group(chat_id: int, **fields: int) -> None:
    if not fields:
        return
    await db.groups.update_one(
        {"chat_id": chat_id},
        {"$inc": {f"stats.{key}": value for key, value in fields.items()}, "$set": {"last_seen": utcnow()}},
    )


async def bump_daily(chat_id: int, **fields: int) -> None:
    if not fields:
        return
    await db.daily.update_one(
        {"day": today_key(), "chat_id": chat_id},
        {"$inc": fields, "$setOnInsert": {"created_at": utcnow()}},
        upsert=True,
    )


async def bump_usage(chat_id: int, kind: str, key: str) -> None:
    if not key:
        return
    await db.usage.update_one(
        {"chat_id": chat_id, "kind": kind, "key": key},
        {"$inc": {"count": 1}, "$set": {"updated_at": utcnow()}},
        upsert=True,
    )


async def get_member_status(chat_id: int, user_id: int) -> ChatMemberStatus | None:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status
    except (TelegramBadRequest, TelegramForbiddenError):
        return None


async def is_owner(chat_id: int, user_id: int) -> bool:
    return await get_member_status(chat_id, user_id) == ChatMemberStatus.CREATOR


async def can_manage(chat_id: int, user_id: int) -> bool:
    status = await get_member_status(chat_id, user_id)
    if status == ChatMemberStatus.CREATOR:
        return True
    if status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return bool(await db.managers.find_one({"chat_id": chat_id, "user_id": user_id}))


async def require_group(message: Message) -> bool:
    if message.chat.type not in GROUP_TYPES:
        await message.answer("Use this command inside a group.")
        return False
    await ensure_group(message.chat)
    return True


async def require_manager(message: Message) -> bool:
    if not await require_group(message):
        return False
    if not message.from_user or not await can_manage(message.chat.id, message.from_user.id):
        await message.answer("❌ Only the group owner or an authorized admin can do this.")
        return False
    return True


async def require_owner(message: Message) -> bool:
    if not await require_group(message):
        return False
    if not message.from_user or not await is_owner(message.chat.id, message.from_user.id):
        await message.answer("❌ Only the group owner can do this.")
        return False
    return True


async def safe_delete(message: Message) -> bool:
    try:
        await message.delete()
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def delete_message_later(chat_id: int, message_id: int, delay: int = TUTORIAL_TTL) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


def nsfw_settings(group: dict[str, Any]) -> tuple[bool, str, int, float]:
    enabled = bool(group.get("nsfw_enabled", True))
    action = str(group.get("nsfw_action", "mute"))
    mute_seconds = int(group.get("nsfw_mute_seconds", NSFW_DEFAULT_MUTE_SECONDS))
    threshold = float(group.get("nsfw_threshold", NSFW_DEFAULT_THRESHOLD))
    return enabled, action, mute_seconds, threshold


def _build_nsfw_detector() -> Any:
    from nudenet import NudeDetector
    return NudeDetector()


async def get_nsfw_detector() -> Any | None:
    global _nsfw_detector, _nsfw_detector_failed
    if _nsfw_detector is not None:
        return _nsfw_detector
    if _nsfw_detector_failed:
        return None
    async with _nsfw_load_lock:
        if _nsfw_detector is not None:
            return _nsfw_detector
        if _nsfw_detector_failed:
            return None
        try:
            _nsfw_detector = await asyncio.to_thread(_build_nsfw_detector)
            log.info("NSFW detector loaded (NudeNet bundled 320px model)")
        except Exception as exc:
            _nsfw_detector_failed = True
            log.exception("Could not load NSFW detector: %s", exc)
            return None
    return _nsfw_detector


def nsfw_scan_target(message: Message) -> tuple[str, str, str] | None:
    # cache_key, Telegram file_id, source description
    if message.photo:
        media = message.photo[-1]
        return f"photo:{media.file_unique_id}", media.file_id, "photo"

    if message.sticker:
        sticker = message.sticker
        key = f"sticker:{sticker.file_unique_id}"
        if not sticker.is_animated and not sticker.is_video:
            return key, sticker.file_id, "sticker"
        thumb = sticker.thumbnail
        if thumb:
            return key, thumb.file_id, "sticker_thumbnail"
        return None

    if message.document:
        doc = message.document
        mime = (doc.mime_type or "").lower()
        if not mime.startswith("image/"):
            return None
        if doc.file_size and doc.file_size > NSFW_MAX_FILE_BYTES:
            return None
        return f"document:{doc.file_unique_id}", doc.file_id, "image_document"

    if message.animation:
        anim = message.animation
        thumb = anim.thumbnail
        if thumb:
            return f"animation:{anim.file_unique_id}", thumb.file_id, "animation_thumbnail"
        return None

    return None


async def download_telegram_file(file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    if not tg_file.file_path:
        raise RuntimeError("Telegram returned no file path")
    buffer = io.BytesIO()
    await bot.download_file(tg_file.file_path, destination=buffer)
    return buffer.getvalue()


async def scan_message_nsfw(message: Message, threshold: float) -> dict[str, Any] | None:
    target = nsfw_scan_target(message)
    if not target:
        return None
    media_key, file_id, source = target

    cached = await db.nsfw_cache.find_one(
        {"media_key": media_key, "version": NSFW_CACHE_VERSION},
        {"max_score": 1, "label": 1, "source": 1},
    )
    if cached is not None:
        score = float(cached.get("max_score", 0.0))
        return {
            "unsafe": score >= threshold,
            "score": score,
            "label": cached.get("label") or "",
            "source": cached.get("source") or source,
            "cached": True,
            "media_key": media_key,
        }

    detector = await get_nsfw_detector()
    if detector is None:
        return None

    try:
        data = await download_telegram_file(file_id)
        if len(data) > NSFW_MAX_FILE_BYTES:
            return None
        async with _nsfw_inference_lock:
            detections = await asyncio.to_thread(detector.detect, data, score_threshold=0.25)
    except Exception as exc:
        log.warning("NSFW scan failed for %s: %s", media_key, exc)
        return None

    blocked = [d for d in detections if str(d.get("class", "")) in NSFW_BLOCK_LABELS]
    if blocked:
        strongest = max(blocked, key=lambda item: float(item.get("score", 0.0)))
        max_score = float(strongest.get("score", 0.0))
        label = str(strongest.get("class", ""))
    else:
        max_score = 0.0
        label = ""

    await db.nsfw_cache.update_one(
        {"media_key": media_key},
        {"$set": {
            "media_key": media_key,
            "version": NSFW_CACHE_VERSION,
            "max_score": max_score,
            "label": label,
            "source": source,
            "updated_at": utcnow(),
        }},
        upsert=True,
    )
    return {
        "unsafe": max_score >= threshold,
        "score": max_score,
        "label": label,
        "source": source,
        "cached": False,
        "media_key": media_key,
    }


async def send_nsfw_notice(message: Message, action_text: str) -> None:
    try:
        sent = await bot.send_message(
            message.chat.id,
            f"🛡️ <b>Unsafe media removed.</b> {html.escape(action_text)}\n"
            "This automated check can make mistakes; the event was logged.",
        )
        asyncio.create_task(delete_message_later(message.chat.id, sent.message_id, NSFW_NOTICE_TTL))
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def apply_nsfw_violation(
    message: Message,
    group: dict[str, Any],
    result: dict[str, Any],
    *,
    punish_user: bool = True,
) -> None:
    await safe_delete(message)
    await bump_group(message.chat.id, nsfw_deleted=1)
    await bump_daily(message.chat.id, nsfw_deleted=1)

    # If an unsafe sticker was ever present in an approved snapshot, remove it
    # from the stored snapshot so the bot stops offering it in inline results.
    if message.sticker:
        await db.packs.update_many(
            {"stickers.file_unique_id": message.sticker.file_unique_id},
            {"$pull": {"stickers": {"file_unique_id": message.sticker.file_unique_id}}},
        )

    user = message.from_user
    score = float(result.get("score", 0.0))
    label = str(result.get("label", "unknown"))
    await log_activity(
        "nsfw_removed",
        chat=message.chat,
        user=user,
        details=f"label={label} score={score:.3f} source={result.get('source', '')}",
    )

    if not user or user.is_bot or not punish_user:
        await send_nsfw_notice(message, "No user penalty was applied.")
        return

    status = await get_member_status(message.chat.id, user.id)
    if status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
        await send_nsfw_notice(message, "The sender is a Telegram admin, so no automatic restriction was applied.")
        return

    _, action, mute_seconds, _ = nsfw_settings(group)
    action_text = "The content was deleted."
    action_applied = False
    try:
        if action == "ban":
            await bot.ban_chat_member(message.chat.id, user.id)
            action_text = f"{user.full_name} was banned."
            action_applied = True
        else:
            until = utcnow() + timedelta(seconds=mute_seconds)
            await bot.restrict_chat_member(
                message.chat.id,
                user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            minutes = max(1, mute_seconds // 60)
            action_text = f"{user.full_name} was muted for {minutes} minute{'s' if minutes != 1 else ''}."
            action_applied = True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.warning("Could not apply NSFW action in %s to %s: %s", message.chat.id, user.id, exc)
        action_text = "The content was deleted, but the bot could not restrict the sender. Check the bot's admin permissions."

    if action_applied:
        await bump_group(message.chat.id, nsfw_actions=1)
        await bump_daily(message.chat.id, nsfw_actions=1)
    await send_nsfw_notice(message, action_text)


async def send_sticker_tutorial(
    message: Message,
    group: dict[str, Any],
    *,
    force: bool = False,
    direct_blocked: bool = True,
) -> None:
    user = message.from_user
    code = group.get("group_code")
    if not user or user.is_bot or not code:
        return

    key = (message.chat.id, user.id)
    now = asyncio.get_running_loop().time()
    last = tutorial_last_sent.get(key, 0.0)
    if not force and now - last < TUTORIAL_TTL:
        return

    # A deliberately opened help result should always work, but replace the
    # previous tutorial instead of stacking duplicate bot messages.
    if force:
        old_message_id = tutorial_message_ids.get(key)
        if old_message_id:
            try:
                await bot.delete_message(message.chat.id, old_message_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

    tutorial_last_sent[key] = now

    mention = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Open Sticker Search",
                switch_inline_query_current_chat=f"{code} ",
            )
        ]]
    )
    intro = (
        f"{mention}, direct stickers are not allowed in this group.\n"
        if direct_blocked
        else f"{mention}, use this group's approved sticker library.\n"
    )
    try:
        sent = await bot.send_message(
            message.chat.id,
            "<b>How to send stickers</b>\n\n"
            + intro
            + f"Group code: <code>{html.escape(code)}</code>\n\n"
            + "Tap <b>Open Sticker Search</b> below. The group's code will be filled in automatically, "
            + "so you can choose an approved sticker immediately. You can also type an emoji or pack name to filter results.\n\n"
            + "This message will be removed automatically in 5 minutes.",
            reply_markup=keyboard,
        )
        tutorial_message_ids[key] = sent.message_id
        asyncio.create_task(delete_message_later(message.chat.id, sent.message_id))
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


def inline_info_result(result_id: str, title: str, description: str, message_text: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text=message_text),
    )


async def extract_pack_name(message: Message) -> str | None:
    arg = command_arg(message)
    if arg:
        return clean_pack_input(arg)
    if message.reply_to_message and message.reply_to_message.sticker:
        return message.reply_to_message.sticker.set_name
    return None


async def load_sticker_set(name: str) -> dict[str, Any]:
    sticker_set = await bot.get_sticker_set(name)
    stickers = []
    for sticker in sticker_set.stickers:
        stickers.append(
            {
                "file_id": sticker.file_id,
                "file_unique_id": sticker.file_unique_id,
                "emoji": sticker.emoji or "",
            }
        )
    return {
        "name": sticker_set.name,
        "title": sticker_set.title,
        "sticker_type": sticker_set.sticker_type,
        "stickers": stickers,
        "updated_at": utcnow(),
    }


def within_rate_limit(chat_id: int, user_id: int, count: int, window: int) -> bool:
    now = asyncio.get_running_loop().time()
    q = rate_windows[(chat_id, user_id)]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= count:
        return False
    q.append(now)
    return True


@router.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message) -> None:
    await touch_dm_user(message.from_user)
    await message.answer(
        "<b>Sticker Guard</b>\n\n"
        "Add me to a group as admin with <b>Delete messages</b> and <b>Ban users</b> permissions. "
        "Then the group owner can add approved sticker packs with /add. NSFW protection is enabled by default.\n\n"
        "Use /help for commands."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await touch_dm_user(message.from_user)
        await message.answer(
            "<b>Sticker Guard</b>\n\n"
            "Add me to a group as an admin with <b>Delete messages</b> and <b>Ban users</b> permissions. "
            "Each group gets its own sticker code, approved sticker packs, and local NSFW media protection.\n\n"
            "Use the group commands inside the group."
        )
        return

    if message.chat.type not in GROUP_TYPES:
        return

    group = await ensure_group(message.chat)
    code = group["group_code"]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Open Sticker Search",
                switch_inline_query_current_chat=f"{code} ",
            )
        ]]
    )
    await message.answer(
        "<b>How to send stickers</b>\n\n"
        f"Group code: <code>{html.escape(code)}</code>\n"
        "Tap <b>Open Sticker Search</b> below to open the approved sticker library with this group's code already filled in. "
        "You can also type an emoji or pack name after the code to filter stickers.\n\n"
        "<b>Group commands</b>\n"
        "/add — add pack (reply to a sticker or give pack link/name)\n"
        "/rm — remove pack\n"
        "/packs — allowed packs\n"
        "/auth — owner authorizes a Telegram admin (reply)\n"
        "/unauth — owner removes authorization (reply)\n"
        "/mods — authorized admins\n"
        "/code — view the group code; /code NEWCODE changes it\n"
        "/search — open this group's inline sticker search\n"
        "/stats — group stats\n"
        "/top — top packs/emojis\n"
        "/limit — view/set rate limit, e.g. <code>/limit 5 30</code>\n"
        "/safe — NSFW protection settings\n\n"
        "Only the group owner or an owner-authorized Telegram admin can add/remove packs or change the group code.",
        reply_markup=keyboard,
    )


@router.message(Command("add"))
async def cmd_add(message: Message) -> None:
    if not await require_manager(message):
        return
    name = await extract_pack_name(message)
    if not name:
        await message.answer("Reply to a sticker with /add, or use <code>/add PACK_LINK</code>.")
        return
    try:
        pack = await load_sticker_set(name)
    except TelegramBadRequest:
        await message.answer("❌ Sticker pack not found.")
        return

    if str(pack["sticker_type"]) == "custom_emoji":
        await message.answer("❌ Custom-emoji packs are not enabled yet. Regular/animated/video sticker packs are supported.")
        return

    await db.packs.update_one({"name": pack["name"]}, {"$set": pack}, upsert=True)
    result = await db.group_packs.update_one(
        {"chat_id": message.chat.id, "pack_name": pack["name"]},
        {
            "$setOnInsert": {
                "chat_id": message.chat.id,
                "pack_name": pack["name"],
                "added_by": message.from_user.id,
                "added_at": utcnow(),
            }
        },
        upsert=True,
    )
    if result.upserted_id is None:
        await message.answer(f"ℹ️ <b>{html.escape(pack['title'])}</b> is already allowed here.")
        return

    await bump_group(message.chat.id, packs_added=1)
    await bump_daily(message.chat.id, packs_added=1)
    await log_activity("pack_add", chat=message.chat, user=message.from_user, details=pack["name"])
    await message.answer(f"✅ Added <b>{html.escape(pack['title'])}</b> · {len(pack['stickers'])} stickers indexed.")


@router.message(Command("rm"))
async def cmd_rm(message: Message) -> None:
    if not await require_manager(message):
        return
    name = await extract_pack_name(message)
    if not name:
        await message.answer("Reply to a sticker with /rm, or use <code>/rm PACK_LINK</code>.")
        return
    result = await db.group_packs.delete_one({"chat_id": message.chat.id, "pack_name": name})
    if not result.deleted_count:
        await message.answer("❌ That pack is not enabled in this group.")
        return
    await bump_group(message.chat.id, packs_removed=1)
    await bump_daily(message.chat.id, packs_removed=1)
    await log_activity("pack_remove", chat=message.chat, user=message.from_user, details=name)
    await message.answer(f"✅ Removed <code>{html.escape(name)}</code>.")


@router.message(Command("packs"))
async def cmd_packs(message: Message) -> None:
    if not await require_group(message):
        return
    names = []
    async for row in db.group_packs.find({"chat_id": message.chat.id}).sort("added_at", ASCENDING):
        names.append(row["pack_name"])
    if not names:
        await message.answer("No sticker packs are enabled yet.")
        return

    pack_titles: dict[str, str] = {}
    async for pack in db.packs.find({"name": {"$in": names}}, {"name": 1, "title": 1}):
        pack_titles[pack["name"]] = pack.get("title") or pack["name"]
    lines = [f"{i}. {html.escape(pack_titles.get(name, name))}" for i, name in enumerate(names, 1)]
    await message.answer("<b>Allowed sticker packs</b>\n\n" + "\n".join(lines))


@router.message(Command("auth"))
async def cmd_auth(message: Message) -> None:
    if not await require_owner(message):
        return
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if not target or target.is_bot:
        await message.answer("Reply to the Telegram admin you want to authorize, then send /auth.")
        return
    status = await get_member_status(message.chat.id, target.id)
    if status != ChatMemberStatus.ADMINISTRATOR:
        await message.answer("❌ That user must already be a Telegram admin of this group.")
        return
    await db.managers.update_one(
        {"chat_id": message.chat.id, "user_id": target.id},
        {
            "$set": {
                "username": target.username,
                "name": target.full_name,
                "authorized_by": message.from_user.id,
                "authorized_at": utcnow(),
            }
        },
        upsert=True,
    )
    await log_activity("manager_add", chat=message.chat, user=message.from_user, details=f"user_id={target.id}")
    await message.answer(f"✅ {html.escape(target.full_name)} can now manage sticker packs.")


@router.message(Command("unauth"))
async def cmd_unauth(message: Message) -> None:
    if not await require_owner(message):
        return
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if not target:
        await message.answer("Reply to the authorized admin, then send /unauth.")
        return
    result = await db.managers.delete_one({"chat_id": message.chat.id, "user_id": target.id})
    if not result.deleted_count:
        await message.answer("That user is not authorized.")
        return
    await log_activity("manager_remove", chat=message.chat, user=message.from_user, details=f"user_id={target.id}")
    await message.answer(f"✅ Authorization removed for {html.escape(target.full_name)}.")


@router.message(Command("mods"))
async def cmd_mods(message: Message) -> None:
    if not await require_group(message):
        return
    rows = []
    async for row in db.managers.find({"chat_id": message.chat.id}).sort("authorized_at", ASCENDING):
        label = f"@{row['username']}" if row.get("username") else row.get("name") or str(row["user_id"])
        rows.append(f"• {html.escape(label)}")
    await message.answer("<b>Authorized admins</b>\n\n" + ("\n".join(rows) if rows else "None"))


@router.message(Command("code"))
async def cmd_code(message: Message) -> None:
    if not await require_group(message):
        return
    group = await ensure_group(message.chat)
    arg = command_arg(message).strip()
    if not arg:
        await message.answer(
            f"Group code: <code>{html.escape(group['group_code'])}</code>\n\n"
            f"Use <code>@{html.escape(BOT_USERNAME)} {html.escape(group['group_code'])}</code> to open this group's stickers."
        )
        return

    if not message.from_user or not await can_manage(message.chat.id, message.from_user.id):
        await message.answer("❌ Only the group owner or an authorized admin can change the group code.")
        return

    code = arg.upper()
    if not CODE_RE.fullmatch(code):
        await message.answer("❌ Use 3-12 characters: letters, numbers, or underscore only.")
        return

    if code == group.get("group_code"):
        await message.answer(f"That is already this group's code: <code>{html.escape(code)}</code>.")
        return

    if await db.groups.find_one({"group_code": code, "chat_id": {"$ne": message.chat.id}}, {"_id": 1}):
        await message.answer("❌ That code is already in use. Please choose another one.")
        return

    try:
        await db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"group_code": code}})
    except DuplicateKeyError:
        await message.answer("❌ That code is already in use. Please choose another one.")
        return

    await log_activity("code_change", chat=message.chat, user=message.from_user, details=code)
    await message.answer(
        f"✅ Group code changed to <code>{html.escape(code)}</code>.\n"
        f"Users can now type <code>@{html.escape(BOT_USERNAME)} {html.escape(code)}</code>."
    )


@router.message(Command("search"))
async def cmd_search(message: Message) -> None:
    if not await require_group(message):
        return
    group = await ensure_group(message.chat)
    code = group["group_code"]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open Sticker Search", switch_inline_query_current_chat=f"{code} ")]
        ]
    )
    await message.answer(
        f"Group code: <code>{html.escape(code)}</code>\n\n"
        "Tap below to open approved stickers. You can optionally type an emoji or pack name to filter them.",
        reply_markup=keyboard,
    )


@router.message(Command("limit"))
async def cmd_limit(message: Message) -> None:
    if not await require_manager(message):
        return
    group = await ensure_group(message.chat)
    arg = command_arg(message)
    if not arg:
        await message.answer(
            f"Current limit: <b>{group.get('limit_count', 5)}</b> stickers / <b>{group.get('limit_window', 30)}</b> sec per user.\n"
            "Set with <code>/limit 5 30</code>."
        )
        return
    try:
        count_s, window_s = arg.split(maxsplit=1)
        count, window = int(count_s), int(window_s)
        if not (1 <= count <= 50 and 5 <= window <= 3600):
            raise ValueError
    except ValueError:
        await message.answer("Use <code>/limit COUNT SECONDS</code> (count 1-50, seconds 5-3600).")
        return
    await db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"limit_count": count, "limit_window": window}})
    await message.answer(f"✅ Limit set to <b>{count}</b> stickers / <b>{window}</b> sec per user.")


@router.message(Command("safe"))
async def cmd_safe(message: Message) -> None:
    if not await require_group(message):
        return
    group = await ensure_group(message.chat)
    arg = command_arg(message).strip().lower()
    enabled, action, mute_seconds, threshold = nsfw_settings(group)

    if not arg:
        action_label = "permanent ban" if action == "ban" else f"{max(1, mute_seconds // 60)} minute mute"
        await message.answer(
            "<b>🛡️ NSFW protection</b>\n\n"
            f"Status: <b>{'ON' if enabled else 'OFF'}</b>\n"
            f"Action: <b>{html.escape(action_label)}</b>\n"
            f"Detection threshold: <b>{threshold:.2f}</b>\n\n"
            "Scans photos, image documents and stickers. Animated/video stickers and GIFs are checked using their Telegram thumbnail.\n\n"
            "Managers: <code>/safe on</code>, <code>/safe off</code>, <code>/safe mute 60</code>, "
            "<code>/safe ban</code>, <code>/safe threshold 0.85</code>."
        )
        return

    if not message.from_user or not await can_manage(message.chat.id, message.from_user.id):
        await message.answer("❌ Only the group owner or an authorized admin can change NSFW protection.")
        return

    parts = arg.split()
    if parts[0] in {"on", "off"} and len(parts) == 1:
        value = parts[0] == "on"
        await db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"nsfw_enabled": value}})
        await message.answer(f"✅ NSFW protection is now <b>{'ON' if value else 'OFF'}</b>.")
        return

    if parts[0] == "ban" and len(parts) == 1:
        await db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"nsfw_action": "ban"}})
        await message.answer("✅ NSFW action set to <b>permanent ban</b>.")
        return

    if parts[0] == "mute":
        minutes = 60
        if len(parts) == 2:
            try:
                minutes = int(parts[1])
            except ValueError:
                minutes = 0
        if not (1 <= minutes <= 10080):
            await message.answer("Use <code>/safe mute MINUTES</code> (1-10080).")
            return
        await db.groups.update_one(
            {"chat_id": message.chat.id},
            {"$set": {"nsfw_action": "mute", "nsfw_mute_seconds": minutes * 60}},
        )
        await message.answer(f"✅ NSFW action set to a <b>{minutes} minute mute</b>.")
        return

    if parts[0] == "threshold" and len(parts) == 2:
        try:
            value = float(parts[1])
        except ValueError:
            value = 0.0
        if not (0.50 <= value <= 0.99):
            await message.answer("Use <code>/safe threshold 0.85</code> (0.50-0.99). Higher values reduce false positives.")
            return
        await db.groups.update_one({"chat_id": message.chat.id}, {"$set": {"nsfw_threshold": value}})
        await message.answer(f"✅ NSFW threshold set to <b>{value:.2f}</b>.")
        return

    await message.answer(
        "Usage: <code>/safe on</code>, <code>/safe off</code>, <code>/safe mute 60</code>, "
        "<code>/safe ban</code>, or <code>/safe threshold 0.85</code>."
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not await require_group(message):
        return
    group = await ensure_group(message.chat)
    stats = group.get("stats", {})
    today = await db.daily.find_one({"day": today_key(), "chat_id": message.chat.id}) or {}
    packs = await db.group_packs.count_documents({"chat_id": message.chat.id})
    managers = await db.managers.count_documents({"chat_id": message.chat.id})
    await message.answer(
        "<b>📊 Group stats</b>\n\n"
        f"Packs: <b>{packs}</b>\n"
        f"Authorized admins: <b>{managers}</b>\n"
        f"Stickers sent: <b>{stats.get('stickers_sent', 0)}</b> (today {today.get('stickers_sent', 0)})\n"
        f"Inline searches: <b>{stats.get('inline_queries', 0)}</b> (today {today.get('inline_queries', 0)})\n"
        f"Unauthorized stickers deleted: <b>{stats.get('deleted_stickers', 0)}</b>\n"
        f"Other-bot inline messages deleted: <b>{stats.get('deleted_inline', 0)}</b>\n"
        f"Rate-limit deletions: <b>{stats.get('rate_deleted', 0)}</b>\n"
        f"NSFW media removed: <b>{stats.get('nsfw_deleted', 0)}</b> (today {today.get('nsfw_deleted', 0)})\n"
        f"NSFW restrictions/bans: <b>{stats.get('nsfw_actions', 0)}</b>"
    )


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    if not await require_group(message):
        return
    top_packs = []
    async for row in db.usage.find({"chat_id": message.chat.id, "kind": "pack"}).sort("count", DESCENDING).limit(5):
        top_packs.append(f"• <code>{html.escape(row['key'])}</code> — {row['count']}")
    top_emojis = []
    async for row in db.usage.find({"chat_id": message.chat.id, "kind": "emoji"}).sort("count", DESCENDING).limit(8):
        top_emojis.append(f"{html.escape(row['key'])} {row['count']}")
    await message.answer(
        "<b>🔥 Top usage</b>\n\n"
        "<b>Packs</b>\n" + ("\n".join(top_packs) if top_packs else "No data yet.") +
        "\n\n<b>Emojis</b>\n" + (" · ".join(top_emojis) if top_emojis else "No data yet.")
    )


@router.inline_query()
async def inline_search(query: InlineQuery) -> None:
    raw = (query.query or "").strip()

    # Telegram does not provide the destination chat ID for an inline query.
    # The group code safely identifies which group's sticker library to use.
    if not raw:
        result = inline_info_result(
            "how_to_send",
            "How to send stickers",
            "Tap to get this group's code and sticker-search button.",
            f"{INLINE_HELP_MARKER}\nOpening this group's sticker guide…",
        )
        await query.answer([result], cache_time=1, is_personal=True)
        return

    parts = raw.split(maxsplit=1)
    code = parts[0].upper()
    search = parts[1].strip() if len(parts) > 1 else ""
    group = await db.groups.find_one({"group_code": code, "active": True})
    if not group:
        results = [
            inline_info_result(
                "invalid_code",
                "Invalid group code",
                f"{code} is not a valid Sticker Guard group code. Tap for the correct code in this group.",
                f"{INLINE_INVALID_MARKER}\nInvalid group code. Loading this group's sticker guide…",
            ),
            inline_info_result(
                "how_to_send_invalid",
                "How to send stickers",
                "Tap to get this group's code and sticker-search button.",
                f"{INLINE_HELP_MARKER}\nOpening this group's sticker guide…",
            ),
        ]
        await query.answer(results, cache_time=1, is_personal=True)
        return

    # A group code may be shared, but it never grants access to non-members.
    status = await get_member_status(group["chat_id"], query.from_user.id)
    if status in {None, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        result = inline_info_result(
            "not_member",
            "This code is not available to you",
            "You must be a member of that group to use its sticker library.",
            "You must be a member of the group linked to this code before you can use its sticker library.",
        )
        await query.answer([result], cache_time=1, is_personal=True)
        return

    allowed = []
    async for row in db.group_packs.find({"chat_id": group["chat_id"]}, {"pack_name": 1}):
        allowed.append(row["pack_name"])
    if not allowed:
        result = inline_info_result(
            "no_packs",
            "No sticker packs enabled",
            "The owner or an authorized admin must add a pack first.",
            "No sticker packs are enabled for this group yet.",
        )
        await query.answer([result], cache_time=1, is_personal=True)
        return

    needle = search.casefold()
    candidates: list[tuple[str, dict[str, Any]]] = []
    async for pack in db.packs.find({"name": {"$in": allowed}}):
        pack_match = bool(needle) and (
            needle in pack.get("name", "").casefold()
            or needle in pack.get("title", "").casefold()
        )
        for sticker in pack.get("stickers", []):
            emoji = sticker.get("emoji", "")
            if needle and not pack_match and needle not in emoji.casefold():
                continue
            candidates.append((pack["name"], sticker))

    if not candidates:
        result = inline_info_result(
            "no_results",
            "No stickers found",
            "Try another emoji or pack name.",
            "No approved stickers matched that search. Try another emoji or pack name.",
        )
        await query.answer([result], cache_time=1, is_personal=True)
        return

    try:
        start = max(0, int(query.offset or "0"))
    except ValueError:
        start = 0
    page = candidates[start:start + 50]

    results: list[InlineQueryResultCachedSticker] = []
    for pack_name, sticker in page:
        result_id = hashlib.sha1(f"{pack_name}:{sticker['file_unique_id']}".encode()).hexdigest()[:32]
        results.append(InlineQueryResultCachedSticker(id=result_id, sticker_file_id=sticker["file_id"]))

    next_offset = str(start + 50) if start + 50 < len(candidates) else ""
    await bump_group(group["chat_id"], inline_queries=1)
    await bump_daily(group["chat_id"], inline_queries=1)
    await query.answer(results, cache_time=1, is_personal=True, next_offset=next_offset)


@router.my_chat_member()
async def on_bot_membership(update: Any) -> None:
    chat = update.chat
    if chat.type not in GROUP_TYPES:
        return
    status = update.new_chat_member.status
    active = status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    await ensure_group(chat)
    await db.groups.update_one({"chat_id": chat.id}, {"$set": {"active": active, "last_seen": utcnow()}})
    await log_activity("group_active" if active else "group_inactive", chat=chat, user=update.from_user)


async def record_allowed_sticker(message: Message) -> None:
    sticker = message.sticker
    if not sticker:
        return
    await bump_group(message.chat.id, stickers_sent=1)
    await bump_daily(message.chat.id, stickers_sent=1)
    if sticker.set_name:
        await bump_usage(message.chat.id, "pack", sticker.set_name)
    if sticker.emoji:
        await bump_usage(message.chat.id, "emoji", sticker.emoji)


@router.message(F.photo | F.document | F.animation)
async def nsfw_media_moderation(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await touch_dm_user(message.from_user)
        return
    if message.chat.type not in GROUP_TYPES:
        return

    group = await ensure_group(message.chat)
    enabled, _, _, threshold = nsfw_settings(group)
    if enabled:
        result = await scan_message_nsfw(message, threshold)
        if result and result.get("unsafe"):
            await apply_nsfw_violation(message, group, result)
            return

    # Keep the existing policy: inline media from other bots is not allowed.
    if message.via_bot and message.via_bot.id != BOT_ID:
        if await safe_delete(message):
            await bump_group(message.chat.id, deleted_inline=1)
            await bump_daily(message.chat.id, deleted_inline=1)
        return


@router.message(F.sticker | F.via_bot)
async def moderation_and_dm_catchall(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await touch_dm_user(message.from_user)
        return
    if message.chat.type not in GROUP_TYPES:
        return

    group = await ensure_group(message.chat)

    enabled, _, _, threshold = nsfw_settings(group)
    if message.sticker and enabled:
        result = await scan_message_nsfw(message, threshold)
        if result and result.get("unsafe"):
            # If this sticker came from our own approved inline library, delete
            # it and remove it from the snapshot, but do not punish the user for
            # selecting something the bot itself offered.
            punish = not (message.via_bot and message.via_bot.id == BOT_ID)
            await apply_nsfw_violation(message, group, result, punish_user=punish)
            return

    # Inline help results cannot know the destination group while the user is
    # browsing. Once the result is posted here, we do know the chat ID, so
    # replace the temporary inline article with the real group-specific guide.
    if (
        message.via_bot
        and message.via_bot.id == BOT_ID
        and not message.sticker
        and message.text
        and (INLINE_HELP_MARKER in message.text or INLINE_INVALID_MARKER in message.text)
    ):
        await safe_delete(message)
        await send_sticker_tutorial(message, group, force=True, direct_blocked=False)
        return

    # Group owner and /auth-authorized admins may send stickers directly.
    # Normal Telegram admins are NOT exempt unless the owner authorized them.
    if message.sticker and message.from_user and await can_manage(message.chat.id, message.from_user.id):
        await record_allowed_sticker(message)
        return

    # Any inline result from another bot is removed, regardless of media type.
    if message.via_bot and message.via_bot.id != BOT_ID:
        if await safe_delete(message):
            await bump_group(message.chat.id, deleted_inline=1)
            await bump_daily(message.chat.id, deleted_inline=1)
            if message.sticker:
                await send_sticker_tutorial(message, group)
        return

    if not message.sticker:
        return

    # Manual stickers are always blocked. Stickers must come through this bot's inline mode.
    if not message.via_bot or message.via_bot.id != BOT_ID:
        if await safe_delete(message):
            await bump_group(message.chat.id, deleted_stickers=1)
            await bump_daily(message.chat.id, deleted_stickers=1)
            await send_sticker_tutorial(message, group)
        return

    set_name = message.sticker.set_name
    if not set_name or not await db.group_packs.find_one({"chat_id": message.chat.id, "pack_name": set_name}):
        if await safe_delete(message):
            await bump_group(message.chat.id, deleted_stickers=1)
            await bump_daily(message.chat.id, deleted_stickers=1)
            await send_sticker_tutorial(message, group)
        return

    if message.from_user:
        allowed = within_rate_limit(
            message.chat.id,
            message.from_user.id,
            int(group.get("limit_count", 5)),
            int(group.get("limit_window", 30)),
        )
        if not allowed:
            if await safe_delete(message):
                await bump_group(message.chat.id, rate_deleted=1)
                await bump_daily(message.chat.id, rate_deleted=1)
            return

    await record_allowed_sticker(message)


async def owner_dm_only(message: Message) -> bool:
    if message.chat.type != ChatType.PRIVATE or not message.from_user:
        return False
    await touch_dm_user(message.from_user)
    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("Not available.")
        return False
    return True


@router.message(Command("bstats"), F.chat.type == ChatType.PRIVATE)
async def cmd_bstats(message: Message) -> None:
    if not await owner_dm_only(message):
        return
    groups_total = await db.groups.count_documents({})
    groups_active = await db.groups.count_documents({"active": True})
    dm_users = await db.users.count_documents({})
    pack_links = await db.group_packs.count_documents({})
    unique_packs = await db.packs.count_documents({})
    today_rows = []
    async for row in db.daily.find({"day": today_key()}):
        today_rows.append(row)
    today = defaultdict(int)
    for row in today_rows:
        for key in ("stickers_sent", "inline_queries", "deleted_stickers", "deleted_inline", "rate_deleted", "packs_added", "packs_removed", "nsfw_deleted", "nsfw_actions"):
            today[key] += int(row.get(key, 0))
    new_users_today = await db.users.count_documents({"first_seen": {"$gte": utcnow() - timedelta(days=1)}})
    await message.answer(
        "<b>📊 Bot stats</b>\n\n"
        f"Groups: <b>{groups_total}</b> (active {groups_active})\n"
        f"DM users: <b>{dm_users}</b> (+{new_users_today} / 24h)\n"
        f"Pack links across groups: <b>{pack_links}</b>\n"
        f"Unique cached packs: <b>{unique_packs}</b>\n\n"
        f"<b>Today</b>\n"
        f"Inline searches: {today['inline_queries']}\n"
        f"Stickers sent: {today['stickers_sent']}\n"
        f"Unauthorized stickers deleted: {today['deleted_stickers']}\n"
        f"Other-bot inline deleted: {today['deleted_inline']}\n"
        f"Rate-limit deleted: {today['rate_deleted']}"
    )


@router.message(Command("activity"), F.chat.type == ChatType.PRIVATE)
async def cmd_activity(message: Message) -> None:
    if not await owner_dm_only(message):
        return
    rows = []
    async for row in db.activity.find({}).sort("created_at", DESCENDING).limit(15):
        when = row.get("created_at")
        stamp = when.strftime("%d %b %H:%M") if isinstance(when, datetime) else "?"
        title = row.get("chat_title") or str(row.get("chat_id") or "-")
        details = row.get("details") or ""
        rows.append(f"<code>{stamp}</code> · {html.escape(row.get('event', '?'))} · {html.escape(title)} {html.escape(details)}")
    await message.answer("<b>Recent activity</b>\n\n" + ("\n".join(rows) if rows else "No activity yet."))


@router.message(Command("groups"), F.chat.type == ChatType.PRIVATE)
async def cmd_groups_owner(message: Message) -> None:
    if not await owner_dm_only(message):
        return
    total = await db.groups.count_documents({})
    active = await db.groups.count_documents({"active": True})
    inactive = total - active
    cutoff = utcnow() - timedelta(days=30)
    stale = await db.groups.count_documents({"active": True, "last_seen": {"$lt": cutoff}})
    top = []
    async for row in db.groups.find({"active": True}).sort("stats.stickers_sent", DESCENDING).limit(5):
        top.append(f"• {html.escape(row.get('title') or str(row['chat_id']))} — {row.get('stats', {}).get('stickers_sent', 0)}")
    await message.answer(
        f"<b>Groups</b>\n\nTotal: <b>{total}</b>\nActive: <b>{active}</b>\nInactive: <b>{inactive}</b>\nStale 30d: <b>{stale}</b>\n\n"
        "<b>Top by sticker sends</b>\n" + ("\n".join(top) if top else "No data yet.")
    )


@router.message(Command("users"), F.chat.type == ChatType.PRIVATE)
async def cmd_users_owner(message: Message) -> None:
    if not await owner_dm_only(message):
        return
    total = await db.users.count_documents({})
    active24 = await db.users.count_documents({"last_seen": {"$gte": utcnow() - timedelta(days=1)}})
    active7 = await db.users.count_documents({"last_seen": {"$gte": utcnow() - timedelta(days=7)}})
    new24 = await db.users.count_documents({"first_seen": {"$gte": utcnow() - timedelta(days=1)}})
    await message.answer(
        f"<b>DM users</b>\n\nTotal: <b>{total}</b>\nActive 24h: <b>{active24}</b>\nActive 7d: <b>{active7}</b>\nNew 24h: <b>{new24}</b>"
    )


@router.message(Command("errors"), F.chat.type == ChatType.PRIVATE)
async def cmd_errors_owner(message: Message) -> None:
    if not await owner_dm_only(message):
        return
    rows = []
    async for row in db.errors.find({}).sort("created_at", DESCENDING).limit(10):
        when = row.get("created_at")
        stamp = when.strftime("%d %b %H:%M") if isinstance(when, datetime) else "?"
        rows.append(f"<code>{stamp}</code> · {html.escape(row.get('error', '')[:250])}")
    await message.answer("<b>Recent errors</b>\n\n" + ("\n".join(rows) if rows else "No recorded errors."))


@router.error()
async def error_handler(event: ErrorEvent) -> bool:
    exc = event.exception
    log.error("Unhandled update error: %s", exc, exc_info=(type(exc), exc, exc.__traceback__))
    now = utcnow()
    try:
        await db.errors.insert_one(
            {
                "error": repr(exc),
                "created_at": now,
                "expire_at": now + timedelta(days=30),
            }
        )
    except Exception:
        pass
    return True


async def configure_bot() -> None:
    global BOT_ID, BOT_USERNAME
    me = await bot.get_me()
    BOT_ID = me.id
    BOT_USERNAME = me.username or ""

    await bot.set_my_commands(
        [
            BotCommand(command="add", description="Add an allowed sticker pack"),
            BotCommand(command="rm", description="Remove an allowed sticker pack"),
            BotCommand(command="packs", description="List allowed packs"),
            BotCommand(command="auth", description="Authorize a Telegram admin"),
            BotCommand(command="unauth", description="Remove admin authorization"),
            BotCommand(command="mods", description="List authorized admins"),
            BotCommand(command="code", description="View or change the group code"),
            BotCommand(command="search", description="Search this group's stickers"),
            BotCommand(command="stats", description="Group sticker stats"),
            BotCommand(command="top", description="Top packs and emojis"),
            BotCommand(command="limit", description="View or set sticker rate limit"),
            BotCommand(command="safe", description="NSFW protection settings"),
            BotCommand(command="help", description="Show help"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="help", description="Show help"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )


async def webhook_handler(request: web.Request) -> web.Response:
    if WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(received, WEBHOOK_SECRET):
            return web.Response(status=403, text="forbidden")
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as exc:
        log.exception("Webhook processing failed: %s", exc)
        return web.Response(status=500, text="error")
    return web.Response(text="ok")


def uptime_seconds() -> int:
    return max(0, int((utcnow() - STARTED_AT).total_seconds()))


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "status": "online",
            "bot": f"@{BOT_USERNAME}" if BOT_USERNAME else "starting",
            "mode": "webhook" if WEBHOOK_ENABLED else "polling",
            "uptime_seconds": uptime_seconds(),
            "port": PORT,
        }
    )


async def status_page_handler(_: web.Request) -> web.Response:
    bot_name = f"@{html.escape(BOT_USERNAME)}" if BOT_USERNAME else "starting"
    mode = "Webhook" if WEBHOOK_ENABLED else "Polling"
    body = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
  <title>Sticker Guard Status</title>
  <style>
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; font-family:system-ui,-apple-system,sans-serif; background:#f6f8fb; color:#172033; }}
    .card {{ width:min(420px,calc(100% - 32px)); padding:28px; border-radius:20px; background:white; box-shadow:0 12px 40px rgba(20,40,80,.10); text-align:center; }}
    .dot {{ display:inline-block; width:10px; height:10px; margin-right:7px; border-radius:50%; background:#22c55e; }}
    h1 {{ margin:0 0 10px; font-size:26px; }}
    p {{ margin:8px 0; color:#5b6475; }}
    code {{ padding:3px 7px; border-radius:7px; background:#eef2f7; }}
  </style>
</head>
<body>
  <main class=\"card\">
    <h1>Sticker Guard</h1>
    <p><span class=\"dot\"></span><strong>Online</strong></p>
    <p>Bot: <code>{bot_name}</code></p>
    <p>Mode: <code>{mode}</code></p>
    <p>Uptime: <code>{uptime_seconds()}s</code></p>
  </main>
</body>
</html>"""
    return web.Response(text=body, content_type="text/html")


async def start_http_server(*, include_webhook: bool) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", status_page_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/status", health_handler)
    if include_webhook:
        app.router.add_post("/telegram/webhook", webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Status server listening on 0.0.0.0:%s", PORT)
    if RENDER_EXTERNAL_HOSTNAME:
        log.info("Public status URL: https://%s/", RENDER_EXTERNAL_HOSTNAME)
    return runner


async def run_webhook() -> None:
    webhook = f"{WEBHOOK_URL}/telegram/webhook"
    await bot.set_webhook(
        webhook,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    runner = await start_http_server(include_webhook=True)
    log.info("Webhook mode active on port %s -> %s", PORT, webhook)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def run_polling() -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    runner = await start_http_server(include_webhook=False)
    log.info("Polling mode active with status server on port %s", PORT)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()


async def main() -> None:
    await init_db()
    await configure_bot()
    log.info("Started @%s (%s)", BOT_USERNAME, BOT_ID)
    try:
        if WEBHOOK_ENABLED:
            await run_webhook()
        else:
            if WEBHOOK_URL and not WEBHOOK_SECRET:
                log.warning("WEBHOOK_SECRET is empty; falling back to polling mode.")
            await run_polling()
    finally:
        await bot.session.close()
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
