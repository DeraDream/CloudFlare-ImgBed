# -*- coding: utf-8 -*-
import asyncio
import html
import io
import json
import logging
import mimetypes
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests
from PIL import Image, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_VERSION = "v0.2.1"
BOT_TOKEN_ENV = os.getenv("BOT_TOKEN", "").strip()
IMGBED_URL = os.getenv("IMGBED_URL", "http://imgbed:8080").rstrip("/")
IMGBED_PUBLIC_URL = os.getenv("IMGBED_PUBLIC_URL", "").strip().rstrip("/")
IMGBED_API_TOKEN_ENV = os.getenv("IMGBED_API_TOKEN", "").strip()
DB_PATH = os.getenv("BOT_DB_PATH", "/data/bot.db")
IMGBED_DB_PATH = os.getenv("IMGBED_DB_PATH", "/imgbed-data/database.sqlite")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))
UPDATE_AGENT_URL = os.getenv("UPDATE_AGENT_URL", "http://updater:8081").rstrip("/")
APP_VERSION = os.getenv("APP_VERSION", "").strip()


def parse_allowed_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in (raw or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError:
            pass
    return result


ALLOWED_USER_IDS_ENV = parse_allowed_ids(os.getenv("ALLOWED_USER_IDS", ""))


def load_web_bot_config() -> dict:
    """从 ImgBed Docker 数据库读取 Telegram Bot 上传配置。"""
    if not IMGBED_DB_PATH or not os.path.exists(IMGBED_DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(f"file:{IMGBED_DB_PATH}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            ("manage@sysConfig@upload",),
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return {}
        settings = json.loads(row[0])
        channels = settings.get("telegram", {}).get("channels", [])
        for channel in channels:
            if channel.get("mode") == "uploadBot":
                return channel
    except Exception as exc:
        logger.warning("Failed to read ImgBed bot config: %s", exc)
    return {}


def current_allowed_ids() -> set[int]:
    if ALLOWED_USER_IDS_ENV:
        return ALLOWED_USER_IDS_ENV
    cfg = load_web_bot_config()
    return parse_allowed_ids(str(cfg.get("chatId", "")))


def current_api_token() -> str:
    if IMGBED_API_TOKEN_ENV:
        return IMGBED_API_TOKEN_ENV
    return str(load_web_bot_config().get("proxyUrl", "")).strip()


def current_bot_token() -> str:
    if BOT_TOKEN_ENV:
        return BOT_TOKEN_ENV
    return str(load_web_bot_config().get("botToken", "")).strip()


def current_bot_enabled() -> bool:
    if BOT_TOKEN_ENV:
        return True
    return bool(load_web_bot_config().get("enabled", False))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("imgbed-tg-bot")

CHANNEL_LABELS = {
    "cfr2": "本地存储 (R2_env)",
    "s3": "S3 / Cloudflare R2",
    "discord": "Discord",
    "huggingface": "Hugging Face",
    "webdav": "WebDAV",
}
NAME_LABELS = {
    "default": "默认",
    "index": "仅前缀",
    "origin": "仅原名",
    "short": "短链接",
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Telegram 原生 Reply Keyboard。非 persistent 模式允许客户端显示键盘展开/收起控件。"""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚙️ 上传设置"), KeyboardButton("📦 存储渠道")],
            [KeyboardButton("👤 当前配置"), KeyboardButton("🌐 打开图床")],
            [KeyboardButton("⬆️ 版本升级"), KeyboardButton("⌨️ 收起菜单")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=False,
        input_field_placeholder="发送图片/文件，或使用快捷按钮",
    )


@dataclass
class UserSettings:
    user_id: int
    channel_type: str = ""
    channel_name: str = ""
    upload_folder: str = "/"
    auto_retry: bool = True
    name_type: str = "default"
    convert_webp: bool = False
    compress_enabled: bool = True
    compress_threshold: float = 5.0
    compress_target: float = 4.0


def db_connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                channel_type TEXT NOT NULL DEFAULT '',
                channel_name TEXT NOT NULL DEFAULT '',
                upload_folder TEXT NOT NULL DEFAULT '/',
                auto_retry INTEGER NOT NULL DEFAULT 1,
                name_type TEXT NOT NULL DEFAULT 'default',
                convert_webp INTEGER NOT NULL DEFAULT 0,
                compress_enabled INTEGER NOT NULL DEFAULT 1,
                compress_threshold REAL NOT NULL DEFAULT 5.0,
                compress_target REAL NOT NULL DEFAULT 4.0
            )
            """
        )


def get_settings(user_id: int) -> UserSettings:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO user_settings(user_id) VALUES(?)", (user_id,))
            row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    return UserSettings(
        user_id=user_id,
        channel_type=row["channel_type"],
        channel_name=row["channel_name"],
        upload_folder=row["upload_folder"],
        auto_retry=bool(row["auto_retry"]),
        name_type=row["name_type"],
        convert_webp=bool(row["convert_webp"]),
        compress_enabled=bool(row["compress_enabled"]),
        compress_threshold=float(row["compress_threshold"]),
        compress_target=float(row["compress_target"]),
    )


def update_settings(user_id: int, **values) -> UserSettings:
    if not values:
        return get_settings(user_id)
    allowed = {
        "channel_type", "channel_name", "upload_folder", "auto_retry", "name_type",
        "convert_webp", "compress_enabled", "compress_threshold", "compress_target",
    }
    values = {k: v for k, v in values.items() if k in allowed}
    if not values:
        return get_settings(user_id)
    columns = ", ".join(f"{key}=?" for key in values)
    params = [int(v) if isinstance(v, bool) else v for v in values.values()]
    params.append(user_id)
    with db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (user_id,))
        conn.execute(f"UPDATE user_settings SET {columns} WHERE user_id=?", params)
    return get_settings(user_id)


def reset_settings(user_id: int) -> UserSettings:
    with db_connect() as conn:
        conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
    return get_settings(user_id)


def is_allowed(user_id: Optional[int]) -> bool:
    if user_id is None or not current_bot_enabled():
        return False
    allowed = current_allowed_ids()
    # 安全优先：未配置白名单时不允许上传，只回显用户 ID 方便管理员配置。
    return bool(allowed) and user_id in allowed


async def ensure_allowed(update: Update) -> bool:
    user = update.effective_user
    if user and is_allowed(user.id):
        return True
    uid = user.id if user else "unknown"
    text = (
        "⛔ 你不在 Bot 白名单中。\n\n"
        f"你的 Telegram User ID：<code>{uid}</code>\n"
        "请在后台「上传设置 → Telegram Bot 上传配置」中加入该 User ID。"
    )
    if update.callback_query:
        await update.callback_query.answer("无权限", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML")
    return False


def api_headers() -> Dict[str, str]:
    token = current_api_token()
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def api_get_channels() -> Dict[str, List[dict]]:
    response = requests.get(
        f"{IMGBED_URL}/api/channels",
        headers=api_headers(),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    # Telegram 在此 fork 中只作为上传端，不允许作为存储渠道。
    data.pop("telegram", None)
    return data


async def get_channels() -> Dict[str, List[dict]]:
    return await asyncio.to_thread(api_get_channels)


def updater_status_sync() -> dict:
    response = requests.get(f"{UPDATE_AGENT_URL}/status", timeout=150)
    response.raise_for_status()
    return response.json()


async def updater_status() -> dict:
    return await asyncio.to_thread(updater_status_sync)


def updater_start_sync() -> dict:
    response = requests.post(f"{UPDATE_AGENT_URL}/update", timeout=15)
    if response.status_code not in (200, 202, 409):
        response.raise_for_status()
    return response.json()


async def updater_start() -> dict:
    return await asyncio.to_thread(updater_start_sync)


def update_status_text(data: dict) -> str:
    if not data.get("ok"):
        return "⬆️ <b>版本升级</b>\n\n❌ 无法获取版本信息：<code>" + html.escape(str(data.get("error", "unknown"))) + "</code>"
    current = data.get("currentVersion") or APP_VERSION or "unknown"
    latest = data.get("latestVersion") or "unknown"
    current_commit = data.get("currentShort") or ""
    latest_commit = data.get("latestShort") or ""
    if data.get("updating"):
        status = "⏳ " + str(data.get("message") or "正在升级")
    elif data.get("updateAvailable"):
        status = "🆕 发现新版本"
    else:
        status = "✅ 当前已是最新版本"
    return (
        "⬆️ <b>版本升级</b>\n\n"
        f"当前版本：<code>{html.escape(str(current))}</code>"
        + (f" ({html.escape(str(current_commit))})" if current_commit else "")
        + "\n"
        f"最新版本：<code>{html.escape(str(latest))}</code>"
        + (f" ({html.escape(str(latest_commit))})" if latest_commit else "")
        + "\n\n"
        + status
    )


def update_keyboard(data: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 检查更新", callback_data="upd:check")]]
    if data.get("updateAvailable") and not data.get("updating"):
        rows.insert(0, [InlineKeyboardButton("⬆️ 立即升级", callback_data="upd:run")])
    return InlineKeyboardMarkup(rows)


async def show_update_status(update: Update) -> None:
    try:
        data = await updater_status()
        text = update_status_text(data)
        markup = update_keyboard(data)
    except Exception as exc:
        text = "⬆️ <b>版本升级</b>\n\n❌ 更新服务不可用：<code>" + html.escape(str(exc)) + "</code>"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 重试", callback_data="upd:check")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)


def flatten_channels(channels: Dict[str, List[dict]]) -> List[Tuple[str, str]]:
    result: List[Tuple[str, str]] = []
    for channel_type in ("cfr2", "s3", "webdav", "huggingface", "discord"):
        for item in channels.get(channel_type, []) or []:
            name = str(item.get("name", "")).strip()
            if name:
                result.append((channel_type, name))
    return result


def normalize_public_url(url: str) -> str:
    if not url:
        return url
    if not IMGBED_PUBLIC_URL:
        return url
    if url.startswith("/"):
        return f"{IMGBED_PUBLIC_URL}{url}"
    try:
        parsed = urlparse(url)
        if parsed.path.startswith("/file/"):
            suffix = parsed.path
            if parsed.query:
                suffix += f"?{parsed.query}"
            return f"{IMGBED_PUBLIC_URL}{suffix}"
    except Exception:
        pass
    return url


def image_to_bytes(image: Image.Image, fmt: str, quality: Optional[int] = None) -> bytes:
    out = io.BytesIO()
    kwargs = {"optimize": True}
    if quality is not None:
        kwargs["quality"] = quality
    if fmt == "JPEG":
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        kwargs["progressive"] = True
    elif fmt == "WEBP":
        kwargs["method"] = 6
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


def compress_to_target(image: Image.Image, fmt: str, target_bytes: int) -> bytes:
    if fmt not in ("JPEG", "WEBP"):
        return image_to_bytes(image, fmt)
    low, high = 35, 95
    best = image_to_bytes(image, fmt, low)
    high_quality = image_to_bytes(image, fmt, high)
    if len(high_quality) <= target_bytes:
        return high_quality
    if len(best) > target_bytes:
        return best
    for _ in range(7):
        quality = (low + high) // 2
        current = image_to_bytes(image, fmt, quality)
        if len(current) <= target_bytes:
            best = current
            low = quality + 1
        else:
            high = quality - 1
    return best


def preprocess_file(data: bytes, filename: str, mime_type: str, settings: UserSettings) -> Tuple[bytes, str, str]:
    if not (mime_type or "").startswith("image/"):
        return data, filename, mime_type
    # 动图保留原样，避免静态化。
    if (mime_type or "").lower() == "image/gif" or filename.lower().endswith(".gif"):
        return data, filename, mime_type
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image.load()
    except Exception:
        return data, filename, mime_type

    threshold_bytes = int(settings.compress_threshold * 1024 * 1024)
    target_bytes = max(128 * 1024, int(settings.compress_target * 1024 * 1024))
    should_compress = settings.compress_enabled and len(data) > threshold_bytes

    if settings.convert_webp:
        processed = compress_to_target(image, "WEBP", target_bytes) if should_compress else image_to_bytes(image, "WEBP", 92)
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        return processed, f"{stem}.webp", "image/webp"

    fmt = (image.format or "").upper()
    if not should_compress:
        return data, filename, mime_type
    if fmt in ("JPEG", "JPG"):
        return compress_to_target(image, "JPEG", target_bytes), filename, "image/jpeg"
    if fmt == "WEBP":
        return compress_to_target(image, "WEBP", target_bytes), filename, "image/webp"
    if fmt == "PNG":
        # PNG 只做无损 optimize；若需要显著减小体积，可在设置中开启 WebP。
        processed = image_to_bytes(image, "PNG")
        return processed if len(processed) < len(data) else data, filename, "image/png"
    return data, filename, mime_type


def upload_file_sync(data: bytes, filename: str, mime_type: str, settings: UserSettings) -> str:
    params = {
        "uploadChannel": settings.channel_type,
        "channelName": settings.channel_name,
        "uploadFolder": settings.upload_folder or "/",
        "autoRetry": "true" if settings.auto_retry else "false",
        "uploadNameType": settings.name_type,
        "returnFormat": "full",
        "serverCompress": "false",
    }
    response = requests.post(
        f"{IMGBED_URL}/upload?{urlencode(params)}",
        headers=api_headers(),
        files={"file": (filename, data, mime_type or "application/octet-stream")},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    result = response.json()
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise RuntimeError(f"Unexpected response: {result!r}")
    url = result[0].get("publicUrl") or result[0].get("src")
    if not url:
        raise RuntimeError(f"Upload response has no URL: {result!r}")
    return normalize_public_url(str(url))


async def upload_file(data: bytes, filename: str, mime_type: str, settings: UserSettings) -> str:
    return await asyncio.to_thread(upload_file_sync, data, filename, mime_type, settings)


def settings_text(s: UserSettings) -> str:
    channel = "未设置"
    if s.channel_type and s.channel_name:
        channel = f"{CHANNEL_LABELS.get(s.channel_type, s.channel_type)} / {s.channel_name}"
    return (
        "⚙️ <b>上传设置</b>\n\n"
        f"📦 存储渠道：<code>{html.escape(channel)}</code>\n"
        f"📁 上传目录：<code>{html.escape(s.upload_folder or '/')}</code>\n"
        f"🔄 自动切换：{'✅ 开启' if s.auto_retry else '❌ 关闭'}\n"
        f"📝 命名方式：{NAME_LABELS.get(s.name_type, s.name_type)}\n"
        f"🖼 转换 WebP：{'✅ 开启' if s.convert_webp else '❌ 关闭'}\n"
        f"🗜 图片压缩：{'✅ 开启' if s.compress_enabled else '❌ 关闭'}\n"
        f"📏 压缩阈值：{s.compress_threshold:g} MB\n"
        f"🎯 期望大小：{s.compress_target:g} MB"
    )


def settings_keyboard(s: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 存储渠道", callback_data="cfg:channel"),
            InlineKeyboardButton("📁 上传目录", callback_data="cfg:folder"),
        ],
        [
            InlineKeyboardButton(f"🔄 自动切换 {'✅' if s.auto_retry else '❌'}", callback_data="cfg:auto"),
            InlineKeyboardButton("📝 命名方式", callback_data="cfg:name"),
        ],
        [
            InlineKeyboardButton(f"🖼 WebP {'✅' if s.convert_webp else '❌'}", callback_data="cfg:webp"),
            InlineKeyboardButton(f"🗜 压缩 {'✅' if s.compress_enabled else '❌'}", callback_data="cfg:compress"),
        ],
        [
            InlineKeyboardButton("📏 压缩阈值", callback_data="cfg:threshold"),
            InlineKeyboardButton("🎯 期望大小", callback_data="cfg:target"),
        ],
        [InlineKeyboardButton("↩️ 恢复默认", callback_data="cfg:reset")],
    ])


async def render_settings(update: Update, settings: Optional[UserSettings] = None) -> None:
    user_id = update.effective_user.id
    s = settings or get_settings(user_id)
    text = settings_text(s)
    markup = settings_keyboard(s)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    await update.effective_message.reply_text(
        "📌 <b>ImgBed Telegram 上传助手</b>\n\n"
        "直接发送图片或文件即可上传到图床。\n"
        "Telegram 仅作为上传入口，文件最终保存到你选择的本地 / R2 / S3 / WebDAV 等渠道。\n\n"
        "下方已启用快捷菜单，可通过输入框旁的键盘按钮展开/收起。\n\n"
        f"ImgBedTGBot · {BOT_VERSION}",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True,
    )


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    s = get_settings(update.effective_user.id)
    await update.effective_message.reply_text(
        f"👤 <b>{html.escape(update.effective_user.full_name)}</b>\n"
        f"🆔 <code>{update.effective_user.id}</code>\n\n{settings_text(s)}",
        parse_mode="HTML",
        reply_markup=settings_keyboard(s),
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    await render_settings(update)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    await update.effective_message.reply_text(
        "⌨️ 快捷菜单已打开。",
        reply_markup=main_menu_keyboard(),
    )


async def open_web(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    url = IMGBED_PUBLIC_URL or IMGBED_URL
    await update.effective_message.reply_text(
        "🌐 打开图床：",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("打开图床", url=url)]]),
    )


async def hide_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    await update.effective_message.reply_text(
        "⌨️ 快捷菜单已收起。需要时发送 /menu 可重新打开。",
        reply_markup=ReplyKeyboardRemove(),
    )


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    text = (update.effective_message.text or "").strip()
    if text == "⚙️ 上传设置":
        await render_settings(update)
    elif text == "📦 存储渠道":
        await show_channel_picker(update)
    elif text == "👤 当前配置":
        await me(update, context)
    elif text == "🌐 打开图床":
        await open_web(update, context)
    elif text == "⬆️ 版本升级":
        await show_update_status(update)
    elif text == "⌨️ 收起菜单":
        await hide_menu(update, context)


async def storage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    await show_channel_picker(update)


async def show_channel_picker(update: Update) -> None:
    try:
        channels = await get_channels()
        flat = flatten_channels(channels)
    except Exception as exc:
        logger.exception("Failed to load channels")
        msg = f"❌ 获取存储渠道失败：<code>{html.escape(str(exc))}</code>"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(msg, parse_mode="HTML")
        return
    if not flat:
        text = "📭 当前没有可用存储渠道，请先在 Web 管理端配置。"
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text)
        return
    buttons = []
    for index, (channel_type, name) in enumerate(flat):
        label = f"{CHANNEL_LABELS.get(channel_type, channel_type)} · {name}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"chan:{channel_type}:{index}")])
    buttons.append([InlineKeyboardButton("⬅️ 返回设置", callback_data="cfg:home")])
    markup = InlineKeyboardMarkup(buttons)
    if update.callback_query:
        await update.callback_query.edit_message_text("📦 <b>选择默认存储渠道</b>", parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text("📦 <b>选择默认存储渠道</b>", parse_mode="HTML", reply_markup=markup)


async def show_name_picker(query) -> None:
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("默认", callback_data="name:default"), InlineKeyboardButton("仅前缀", callback_data="name:index")],
        [InlineKeyboardButton("仅原名", callback_data="name:origin"), InlineKeyboardButton("短链接", callback_data="name:short")],
        [InlineKeyboardButton("⬅️ 返回设置", callback_data="cfg:home")],
    ])
    await query.edit_message_text("📝 <b>选择文件命名方式</b>", parse_mode="HTML", reply_markup=markup)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    uid = query.from_user.id
    s = get_settings(uid)

    if data in ("cfg:home",):
        await render_settings(update, s)
    elif data == "cfg:channel":
        await show_channel_picker(update)
    elif data == "cfg:folder":
        context.user_data["awaiting_setting"] = "folder"
        await query.edit_message_text("📁 请输入上传目录，例如：<code>/telegram</code>\n输入 <code>/</code> 表示根目录。", parse_mode="HTML")
    elif data == "cfg:auto":
        await render_settings(update, update_settings(uid, auto_retry=not s.auto_retry))
    elif data == "cfg:name":
        await show_name_picker(query)
    elif data == "cfg:webp":
        await render_settings(update, update_settings(uid, convert_webp=not s.convert_webp))
    elif data == "cfg:compress":
        await render_settings(update, update_settings(uid, compress_enabled=not s.compress_enabled))
    elif data == "cfg:threshold":
        context.user_data["awaiting_setting"] = "threshold"
        await query.edit_message_text("📏 请输入压缩阈值（MB），例如：<code>5</code>。", parse_mode="HTML")
    elif data == "cfg:target":
        context.user_data["awaiting_setting"] = "target"
        await query.edit_message_text("🎯 请输入压缩后的期望大小（MB），例如：<code>4</code>。", parse_mode="HTML")
    elif data == "cfg:reset":
        await render_settings(update, reset_settings(uid))
    elif data.startswith("name:"):
        name_type = data.split(":", 1)[1]
        if name_type in NAME_LABELS:
            await render_settings(update, update_settings(uid, name_type=name_type))
    elif data.startswith("chan:"):
        try:
            _, channel_type, index_str = data.split(":", 2)
            index = int(index_str)
            flat = flatten_channels(await get_channels())
            chosen_type, chosen_name = flat[index]
            if chosen_type != channel_type:
                raise ValueError("channel list changed")
            await render_settings(update, update_settings(uid, channel_type=chosen_type, channel_name=chosen_name))
        except Exception as exc:
            logger.exception("Failed to choose channel")
            await query.edit_message_text(f"❌ 渠道选择失败：<code>{html.escape(str(exc))}</code>", parse_mode="HTML")
    elif data == "upd:check":
        await show_update_status(update)
    elif data == "upd:run":
        try:
            result = await updater_start()
            if result.get("accepted") or result.get("ok"):
                await query.edit_message_text(
                    "⬆️ <b>升级任务已开始</b>\n\n"
                    "正在从 GitHub 拉取最新代码并重新构建容器。\n"
                    "图床和 Bot 可能短暂重启，约几十秒到几分钟后恢复。",
                    parse_mode="HTML",
                )
            else:
                raise RuntimeError(result.get("error") or "升级请求失败")
        except Exception as exc:
            await query.edit_message_text(
                "❌ 升级启动失败：<code>" + html.escape(str(exc)) + "</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 重试", callback_data="upd:check")]]),
            )


async def setting_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    awaiting = context.user_data.pop("awaiting_setting", None)
    if not awaiting:
        return
    uid = update.effective_user.id
    value = (update.effective_message.text or "").strip()
    try:
        if awaiting == "folder":
            folder = value or "/"
            if not folder.startswith("/"):
                folder = "/" + folder
            s = update_settings(uid, upload_folder=folder)
        elif awaiting == "threshold":
            number = float(value)
            if not 0.5 <= number <= 100:
                raise ValueError("范围应为 0.5 ~ 100 MB")
            current = get_settings(uid)
            updates = {"compress_threshold": number}
            if current.compress_target > number:
                updates["compress_target"] = number
            s = update_settings(uid, **updates)
        elif awaiting == "target":
            number = float(value)
            current = get_settings(uid)
            if not 0.1 <= number <= current.compress_threshold:
                raise ValueError(f"范围应为 0.1 ~ {current.compress_threshold:g} MB")
            s = update_settings(uid, compress_target=number)
        else:
            return
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ 设置无效：{exc}\n请重新打开 /settings 设置。")
        return
    await render_settings(update, s)


async def download_telegram_file(update: Update) -> Tuple[bytes, str, str]:
    message = update.effective_message
    if message.photo:
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        return data, f"{photo.file_unique_id}.jpg", "image/jpeg"
    if message.document:
        doc = message.document
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        filename = doc.file_name or f"{doc.file_unique_id}"
        mime = doc.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return data, filename, mime
    raise ValueError("不支持的消息类型")


async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update):
        return
    uid = update.effective_user.id
    settings = get_settings(uid)
    if not settings.channel_type or not settings.channel_name:
        await update.effective_message.reply_text(
            "⚠️ 还没有选择存储渠道，请先使用 /set_storage。",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 选择存储渠道", callback_data="cfg:channel")]]),
        )
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)
    status = await update.effective_message.reply_text("📤 正在上传…")
    try:
        data, filename, mime = await download_telegram_file(update)
        processed, filename, mime = await asyncio.to_thread(preprocess_file, data, filename, mime, settings)
        url = await upload_file(processed, filename, mime, settings)
        text = (
            "✅ <b>上传成功！</b>\n\n"
            f"🔗 URL:\n<code>{html.escape(url)}</code>\n\n"
            f"📝 Markdown:\n<code>![]({html.escape(url)})</code>\n\n"
            f"💬 BBCode:\n<code>[img]{html.escape(url)}[/img]</code>"
        )
        await status.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 打开链接", url=url)],
                [InlineKeyboardButton("⚙️ 上传设置", callback_data="cfg:home")],
            ]),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.exception("Upload failed")
        await status.edit_text(f"❌ 上传失败：\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")


async def post_init(application: Application) -> None:
    # 删除 Telegram 左侧“菜单/命令”入口。Reply Keyboard 的展开/收起由客户端输入框旁按钮控制。
    await application.bot.delete_my_commands()


def wait_for_runtime_config() -> str:
    """等待后台完成 Bot 配置。容器可先启动，后台保存后会自动继续。"""
    while True:
        token = current_bot_token()
        api_token = current_api_token()
        enabled = current_bot_enabled()
        if enabled and token and api_token:
            allowed = current_allowed_ids()
            if not allowed:
                logger.warning("Telegram Bot 已启用，但允许用户 ID 为空；Bot 将拒绝所有用户")
            return token
        logger.info("等待后台 Telegram Bot 配置：需要启用 Bot、填写 Bot Token 和 ImgBed API Token")
        time.sleep(10)


def main() -> None:
    init_db()
    bot_token = wait_for_runtime_config()
    app = Application.builder().token(bot_token).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("set_storage", storage_cmd))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_upload))
    app.add_handler(MessageHandler(
        filters.Regex(r"^(⚙️ 上传设置|📦 存储渠道|👤 当前配置|🌐 打开图床|⬆️ 版本升级|⌨️ 收起菜单)$"),
        menu_button_handler,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, setting_text_input))
    logger.info("ImgBed Telegram Bot %s started", BOT_VERSION)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
