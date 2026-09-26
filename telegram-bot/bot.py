# -*- coding: utf-8 -*-
import asyncio
import html
import io
import json
import hashlib
import re
import logging
import mimetypes
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests
from PIL import Image, ImageOps
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from history_store import (
    create_history,
    decode_settings,
    decode_tags,
    find_duplicate,
    get_history,
    init_history_db,
    list_history,
    update_history,
    update_tags_note,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_VERSION = "v0.4.0"
BOT_TOKEN_ENV = os.getenv("BOT_TOKEN", "").strip()
IMGBED_URL = os.getenv("IMGBED_URL", "http://imgbed:8080").rstrip("/")
IMGBED_PUBLIC_URL = os.getenv("IMGBED_PUBLIC_URL", "").strip().rstrip("/")
IMGBED_API_TOKEN_ENV = os.getenv("IMGBED_API_TOKEN", "").strip()
DB_PATH = os.getenv("BOT_DB_PATH", "/data/bot.db")
IMGBED_DB_PATH = os.getenv("IMGBED_DB_PATH", "/imgbed-data/database.sqlite")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))
UPDATE_AGENT_URL = os.getenv("UPDATE_AGENT_URL", "http://updater:8081").rstrip("/")
APP_VERSION = os.getenv("APP_VERSION", "").strip()
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
try:
    LOCAL_TZ = ZoneInfo(BOT_TIMEZONE)
except Exception:
    LOCAL_TZ = ZoneInfo("UTC")


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
            [KeyboardButton("👤 当前配置"), KeyboardButton("🕘 最近上传")],
            [KeyboardButton("🌐 打开图床"), KeyboardButton("⬆️ 版本升级")],
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
    auto_date_dir: bool = False


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_settings)").fetchall()}
        if "auto_date_dir" not in columns:
            conn.execute(
                "ALTER TABLE user_settings ADD COLUMN auto_date_dir INTEGER NOT NULL DEFAULT 0"
            )

    init_history_db(DB_PATH)


def get_meta(key: str, default: str = "") -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_meta(key: str, value) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO bot_meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, str(value)),
        )


def delete_meta(key: str) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM bot_meta WHERE key=?", (key,))


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
        auto_date_dir=bool(row["auto_date_dir"]),
    )


def update_settings(user_id: int, **values) -> UserSettings:
    if not values:
        return get_settings(user_id)
    allowed = {
        "channel_type", "channel_name", "upload_folder", "auto_retry", "name_type",
        "convert_webp", "compress_enabled", "compress_threshold", "compress_target",
        "auto_date_dir",
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


def settings_from_snapshot(user_id: int, data: dict) -> UserSettings:
    base = asdict(get_settings(user_id))
    for key in base:
        if key in data:
            base[key] = data[key]
    base["user_id"] = user_id
    return UserSettings(**base)


def normalize_folder(folder: str) -> str:
    folder = (folder or "/").replace("\\", "/").strip()
    if not folder.startswith("/"):
        folder = "/" + folder
    folder = re.sub(r"/+", "/", folder)
    if len(folder) > 1:
        folder = folder.rstrip("/")
    return folder or "/"


def effective_upload_folder(settings: UserSettings) -> str:
    base = normalize_folder(settings.upload_folder)
    if not settings.auto_date_dir:
        return base
    date_part = datetime.now(LOCAL_TZ).strftime("%Y/%m/%d")
    if base == "/":
        return "/" + date_part
    return f"{base}/{date_part}"


def parse_caption_metadata(caption: str) -> Tuple[List[str], str]:
    caption = (caption or "").strip()
    if not caption:
        return [], ""
    tags = []
    seen = set()
    for match in re.finditer(r"(?<!\S)#([\w\u4e00-\u9fff-]{1,32})", caption, flags=re.UNICODE):
        tag = match.group(1).strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
    note = re.sub(r"(?<!\S)#[\w\u4e00-\u9fff-]{1,32}", " ", caption, flags=re.UNICODE)
    note = re.sub(r"\s+", " ", note).strip()
    return tags[:20], note[:500]


def format_bytes(value: int) -> str:
    size = float(max(0, int(value or 0)))
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def format_eta(seconds: float) -> str:
    if seconds < 0 or seconds == float("inf"):
        return "--"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def get_image_info(data: bytes, mime_type: str, fallback_width: int = 0, fallback_height: int = 0) -> dict:
    info = {
        "width": int(fallback_width or 0),
        "height": int(fallback_height or 0),
        "format": "",
    }
    if not (mime_type or "").startswith("image/"):
        return info
    try:
        with Image.open(io.BytesIO(data)) as image:
            info["width"], info["height"] = image.size
            info["format"] = (image.format or "").upper()
    except Exception:
        pass
    return info


def media_source_from_message(message) -> dict:
    media = None
    filename = ""
    mime = "application/octet-stream"
    width = 0
    height = 0
    duration = 0

    if message.photo:
        media = message.photo[-1]
        filename = f"{media.file_unique_id}.jpg"
        mime = "image/jpeg"
        width = int(media.width or 0)
        height = int(media.height or 0)
    elif message.video:
        media = message.video
        filename = media.file_name or f"{media.file_unique_id}.mp4"
        mime = media.mime_type or "video/mp4"
        width = int(media.width or 0)
        height = int(media.height or 0)
        duration = int(message.video.duration or 0)
    elif message.document:
        media = message.document
        filename = media.file_name or str(media.file_unique_id)
        mime = media.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    else:
        raise ValueError("不支持的消息类型")

    tags, note = parse_caption_metadata(message.caption or "")
    return {
        "file_id": media.file_id,
        "file_unique_id": media.file_unique_id,
        "filename": filename,
        "mime_type": mime,
        "expected_size": int(media.file_size or 0),
        "width": width,
        "height": height,
        "duration": duration,
        "caption": message.caption or "",
        "tags": tags,
        "note": note,
        "message_id": int(message.message_id),
        "media_group_id": str(message.media_group_id or ""),
    }


def history_time(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp), LOCAL_TZ).strftime("%m-%d %H:%M")
    except Exception:
        return "--"


def history_status_icon(row) -> str:
    if row["status"] == "success":
        return "⚡" if int(row["is_duplicate"] or 0) else "✅"
    if row["status"] == "failed":
        return "❌"
    return "⏳"


def history_info_text(row, *, compact: bool = False) -> str:
    tags = decode_tags(row)
    filename = row["final_name"] or row["original_name"] or "未命名文件"
    mime = row["mime_type"] or "application/octet-stream"
    lines = []
    if not compact:
        lines.append(f"{history_status_icon(row)} <b>{html.escape(filename)}</b>")

    if int(row["width"] or 0) and int(row["height"] or 0):
        lines.append(f"🖼 {row['width']}×{row['height']} · <code>{html.escape(mime)}</code>")
    else:
        lines.append(f"📄 <code>{html.escape(mime)}</code>")

    final_size = int(row["final_size"] or 0)
    original_size = int(row["original_size"] or 0)
    if final_size:
        size_line = f"📦 {format_bytes(final_size)}"
        if original_size and original_size != final_size:
            size_line += f" · 原始 {format_bytes(original_size)}"
        lines.append(size_line)

    channel = f"{row['channel_type'] or ''} / {row['channel_name'] or ''}".strip(" /")
    if channel:
        lines.append(f"☁️ {html.escape(channel)}")
    if row["upload_folder"]:
        lines.append(f"📁 <code>{html.escape(row['upload_folder'])}</code>")
    if int(row["is_duplicate"] or 0):
        lines.append("⚡ SHA-256 查重命中 · 秒传")
    if tags:
        lines.append("🏷 " + " ".join("#" + html.escape(tag) for tag in tags))
    if row["note"]:
        lines.append("📝 " + html.escape(str(row["note"])))
    if row["status"] == "failed" and row["error"]:
        lines.append("❌ " + html.escape(str(row["error"])[:500]))
    return "\n".join(lines)


def history_detail_keyboard(row, page: int = 0) -> InlineKeyboardMarkup:
    rows = []
    if row["status"] == "success" and row["url"]:
        rows.append([InlineKeyboardButton("🔗 打开链接", url=row["url"])])
    if row["status"] == "failed":
        rows.append([InlineKeyboardButton("🔄 重新上传", callback_data=f"retry:{row['id']}")])
    rows.append([InlineKeyboardButton("🏷 标签/备注", callback_data=f"meta:{row['id']}")])
    rows.append([InlineKeyboardButton("⬅️ 最近上传", callback_data=f"recent:{page}")])
    return InlineKeyboardMarkup(rows)


async def show_recent_uploads(update: Update, page: int = 0) -> None:
    uid = update.effective_user.id
    page = max(0, int(page))
    per_page = 5
    rows, total = list_history(DB_PATH, uid, limit=per_page, offset=page * per_page)
    max_page = max(0, (total - 1) // per_page) if total else 0
    if page > max_page:
        page = max_page
        rows, total = list_history(DB_PATH, uid, limit=per_page, offset=page * per_page)

    if not rows:
        text = "🕘 <b>最近上传</b>\n\n暂无上传记录。"
        markup = None
    else:
        text_lines = [f"🕘 <b>最近上传</b> · 第 {page + 1}/{max_page + 1} 页", ""]
        buttons = []
        for index, row in enumerate(rows, start=1):
            name = row["final_name"] or row["original_name"] or "未命名文件"
            if len(name) > 26:
                name = name[:23] + "…"
            text_lines.append(
                f"{history_status_icon(row)} {index}. {html.escape(name)}"
                f" · {format_bytes(int(row['final_size'] or row['original_size'] or 0))}"
                f" · {history_time(row['created_at'])}"
            )
            buttons.append([
                InlineKeyboardButton(
                    f"{history_status_icon(row)} {index}. {name}",
                    callback_data=f"hist:{row['id']}:{page}",
                )
            ])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"recent:{page - 1}"))
        if page < max_page:
            nav.append(InlineKeyboardButton("下一页 ➡️", callback_data=f"recent:{page + 1}"))
        if nav:
            buttons.append(nav)
        text = "\n".join(text_lines)
        markup = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def show_history_detail(update: Update, history_id: int, page: int = 0) -> None:
    row = get_history(DB_PATH, history_id, update.effective_user.id)
    if not row:
        if update.callback_query:
            await update.callback_query.answer("记录不存在", show_alert=True)
        return
    text = history_info_text(row)
    if row["status"] == "success" and row["url"]:
        text += f"\n\n🔗 URL:\n<code>{html.escape(row['url'])}</code>"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=history_detail_keyboard(row, page),
            disable_web_page_preview=True,
        )
    else:
        await update.effective_message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=history_detail_keyboard(row, page),
            disable_web_page_preview=True,
        )


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
    if data.get("updating"):
        status = "⏳ " + str(data.get("message") or "正在升级")
    elif data.get("updateAvailable"):
        status = "🆕 发现新版本，可以点击「立即升级」"
    else:
        status = "✅ 当前已经是最新版本，无需升级"
    return (
        "⬆️ <b>版本升级</b>\n\n"
        f"当前版本：<code>{html.escape(str(current))}</code>\n"
        f"最新版本：<code>{html.escape(str(latest))}</code>\n\n"
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


def update_result_text(data: dict) -> str:
    success = data.get("success")
    version = data.get("currentVersion") or APP_VERSION or "unknown"
    if success is True:
        return (
            "✅ <b>版本升级成功</b>\n\n"
            f"当前版本：<code>{html.escape(str(version))}</code>"
            + "\n\nImgBed 与 Telegram Bot 已应用新版本。"
        )
    message = str(data.get("message") or data.get("error") or "未知错误")
    stage = str(data.get("stage") or "failed")
    return (
        "❌ <b>版本升级失败</b>\n\n"
        f"阶段：<code>{html.escape(stage)}</code>\n"
        f"原因：<code>{html.escape(message)}</code>\n\n"
        "可在网页「版本升级」页面查看详细日志。"
    )


async def send_update_result(application: Application, data: dict) -> bool:
    chat_id = get_meta("update_pending_chat")
    message_id = get_meta("update_pending_message")
    if not chat_id:
        return False

    text = update_result_text(data)
    try:
        if message_id:
            await application.bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 再次检查", callback_data="upd:check")]
                ]),
            )
        else:
            await application.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode="HTML",
            )
    except Exception:
        logger.exception("Failed to send update result")
        return False

    finished_at = str(data.get("finishedAt") or int(time.time()))
    set_meta("last_update_notice", finished_at)
    delete_meta("update_pending_chat")
    delete_meta("update_pending_message")
    delete_meta("update_pending_started")
    return True


async def monitor_update(application: Application) -> None:
    """持续跟踪升级状态；Bot 被更新重启后，post_init 会自动续接。"""
    chat_id = get_meta("update_pending_chat")
    if not chat_id:
        return

    started_at = int(get_meta("update_pending_started", "0") or 0)
    last_stage = ""
    deadline = time.monotonic() + 3600

    while time.monotonic() < deadline:
        try:
            data = await updater_status()
        except Exception:
            # 更新期间 updater 自身也可能短暂重启，继续等待。
            await asyncio.sleep(4)
            continue

        if data.get("updating"):
            stage = str(data.get("stage") or "")
            message = str(data.get("message") or "正在升级")
            if stage != last_stage:
                last_stage = stage
                message_id = get_meta("update_pending_message")
                if message_id:
                    try:
                        await application.bot.edit_message_text(
                            chat_id=int(chat_id),
                            message_id=int(message_id),
                            text=(
                                "⬆️ <b>正在升级</b>\n\n"
                                f"{html.escape(message)}\n"
                                f"阶段：<code>{html.escape(stage or 'working')}</code>"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            await asyncio.sleep(4)
            continue

        finished_at = int(data.get("finishedAt") or 0)
        if data.get("success") is not None and (not started_at or finished_at >= started_at):
            await send_update_result(application, data)
            return

        await asyncio.sleep(4)

    # 超时也必须给反馈
    await send_update_result(application, {
        "success": False,
        "stage": "timeout",
        "message": "等待升级结果超过 60 分钟",
        "finishedAt": int(time.time()),
    })


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


def upload_file_sync(
    data: bytes,
    filename: str,
    mime_type: str,
    settings: UserSettings,
    progress_callback=None,
) -> str:
    params = {
        "uploadChannel": settings.channel_type,
        "channelName": settings.channel_name,
        "uploadFolder": settings.upload_folder or "/",
        "autoRetry": "true" if settings.auto_retry else "false",
        "uploadNameType": settings.name_type,
        "returnFormat": "full",
        "serverCompress": "false",
    }

    encoder = MultipartEncoder(
        fields={"file": (filename, data, mime_type or "application/octet-stream")}
    )

    def on_upload(monitor):
        if progress_callback and monitor.len:
            progress_callback(min(1.0, monitor.bytes_read / monitor.len))

    monitor = MultipartEncoderMonitor(encoder, on_upload)
    headers = api_headers()
    headers["Content-Type"] = monitor.content_type

    response = requests.post(
        f"{IMGBED_URL}/upload?{urlencode(params)}",
        headers=headers,
        data=monitor,
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


async def upload_file(
    data: bytes,
    filename: str,
    mime_type: str,
    settings: UserSettings,
    reporter=None,
) -> str:
    loop = asyncio.get_running_loop()

    def progress(fraction: float):
        if reporter:
            reporter.emit_from_thread(loop, 45 + fraction * 53, "正在上传到图床")

    return await asyncio.to_thread(
        upload_file_sync,
        data,
        filename,
        mime_type,
        settings,
        progress,
    )


def settings_text(s: UserSettings) -> str:
    channel = "未设置"
    if s.channel_type and s.channel_name:
        channel = f"{CHANNEL_LABELS.get(s.channel_type, s.channel_type)} / {s.channel_name}"
    return (
        "⚙️ <b>上传设置</b>\n\n"
        f"📦 存储渠道：<code>{html.escape(channel)}</code>\n"
        f"📁 上传目录：<code>{html.escape(s.upload_folder or '/')}</code>\n"
        f"📅 日期目录：{'✅ 开启' if s.auto_date_dir else '❌ 关闭'}"
        + (f" → <code>{html.escape(effective_upload_folder(s))}</code>" if s.auto_date_dir else "")
        + "\n"
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
            InlineKeyboardButton(f"📅 日期目录 {'✅' if s.auto_date_dir else '❌'}", callback_data="cfg:date"),
            InlineKeyboardButton(f"🔄 自动切换 {'✅' if s.auto_retry else '❌'}", callback_data="cfg:auto"),
        ],
        [
            InlineKeyboardButton("📝 命名方式", callback_data="cfg:name"),
            InlineKeyboardButton(f"🖼 WebP {'✅' if s.convert_webp else '❌'}", callback_data="cfg:webp"),
        ],
        [
            InlineKeyboardButton(f"🗜 压缩 {'✅' if s.compress_enabled else '❌'}", callback_data="cfg:compress"),
            InlineKeyboardButton("📏 压缩阈值", callback_data="cfg:threshold"),
        ],
        [InlineKeyboardButton("🎯 期望大小", callback_data="cfg:target")],
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
    elif text == "🕘 最近上传":
        await show_recent_uploads(update)
    elif text == "🌐 打开图床":
        await open_web(update, context)
    elif text == "⬆️ 版本升级":
        await show_update_status(update)


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
    elif data == "cfg:date":
        await render_settings(update, update_settings(uid, auto_date_dir=not s.auto_date_dir))
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
    elif data.startswith("recent:"):
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            page = 0
        await show_recent_uploads(update, page)
    elif data.startswith("hist:"):
        try:
            _, history_id, page = data.split(":", 2)
            await show_history_detail(update, int(history_id), int(page))
        except Exception:
            await query.answer("记录参数无效", show_alert=True)
    elif data.startswith("meta:"):
        try:
            history_id = int(data.split(":", 1)[1])
            row = get_history(DB_PATH, history_id, uid)
            if not row:
                raise ValueError("记录不存在")
            context.user_data["awaiting_history_meta"] = history_id
            tags = decode_tags(row)
            current = " ".join("#" + tag for tag in tags)
            if row["note"]:
                current = (current + " " + str(row["note"])).strip()
            await query.edit_message_text(
                "🏷 <b>编辑标签 / 备注</b>\n\n"
                "发送格式示例：\n"
                "<code>#服务器 #截图 这是今天的测试图</code>\n\n"
                "标签使用 # 开头，其余文字作为备注。\n"
                "发送 <code>-</code> 可清空。"
                + (f"\n\n当前：<code>{html.escape(current)}</code>" if current else ""),
                parse_mode="HTML",
            )
        except Exception as exc:
            await query.answer(str(exc), show_alert=True)
    elif data.startswith("retry:"):
        try:
            history_id = int(data.split(":", 1)[1])
            await retry_history_upload(update, context, history_id)
        except Exception as exc:
            await query.edit_message_text(
                "❌ 重试失败：<code>" + html.escape(str(exc)) + "</code>",
                parse_mode="HTML",
            )
    elif data == "upd:check":
        await show_update_status(update)
    elif data == "upd:run":
        try:
            result = await updater_start()
            if result.get("accepted") or result.get("ok"):
                set_meta("update_pending_chat", query.message.chat_id)
                set_meta("update_pending_message", query.message.message_id)
                set_meta("update_pending_started", int(time.time()))
                await query.edit_message_text(
                    "⬆️ <b>升级任务已开始</b>\n\n"
                    "正在从 GitHub 拉取最新代码并重新构建容器。\n"
                    "升级成功或失败后，我会自动在这里反馈结果。",
                    parse_mode="HTML",
                )
                context.application.create_task(monitor_update(context.application))
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

    uid = update.effective_user.id
    value = (update.effective_message.text or "").strip()

    history_id = context.user_data.pop("awaiting_history_meta", None)
    if history_id is not None:
        if value == "-":
            tags, note = [], ""
        else:
            tags, note = parse_caption_metadata(value)
        if not update_tags_note(DB_PATH, int(history_id), uid, tags, note):
            await update.effective_message.reply_text("❌ 记录不存在或无权限。")
            return
        await update.effective_message.reply_text("✅ 标签 / 备注已保存。")
        row = get_history(DB_PATH, int(history_id), uid)
        if row:
            await update.effective_message.reply_text(
                history_info_text(row)
                + (f"\n\n🔗 URL:\n<code>{html.escape(row['url'])}</code>" if row["url"] else ""),
                parse_mode="HTML",
                reply_markup=history_detail_keyboard(row),
                disable_web_page_preview=True,
            )
        return

    awaiting = context.user_data.pop("awaiting_setting", None)
    if not awaiting:
        return
    try:
        if awaiting == "folder":
            folder = value or "/"
            if not folder.startswith("/"):
                folder = "/" + folder
            s = update_settings(uid, upload_folder=folder)
            feedback = f"✅ 上传目录已设置为：<code>{html.escape(folder)}</code>"
        elif awaiting == "threshold":
            number = float(value)
            if not 0.5 <= number <= 100:
                raise ValueError("范围应为 0.5 ~ 100 MB")
            current = get_settings(uid)
            updates = {"compress_threshold": number}
            if current.compress_target > number:
                updates["compress_target"] = number
            s = update_settings(uid, **updates)
            feedback = f"✅ 压缩阈值已设置为：<code>{number:g} MB</code>"
        elif awaiting == "target":
            number = float(value)
            current = get_settings(uid)
            if not 0.1 <= number <= current.compress_threshold:
                raise ValueError(f"范围应为 0.1 ~ {current.compress_threshold:g} MB")
            s = update_settings(uid, compress_target=number)
            feedback = f"✅ 期望大小已设置为：<code>{number:g} MB</code>"
        else:
            return
    except Exception as exc:
        await update.effective_message.reply_text(f"❌ 设置无效：{exc}\n请重新打开 /settings 设置。")
        return

    await update.effective_message.reply_text(feedback, parse_mode="HTML")
    await render_settings(update, s)


def progress_bar(percent: float, width: int = 12) -> str:
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round(width * percent / 100))
    return "█" * filled + "░" * (width - filled)


class UploadProgressReporter:
    def __init__(self, message, filename: str):
        self.message = message
        self.filename = filename
        self.queue = asyncio.Queue(maxsize=1)
        self.task = None
        self.last_percent = -1
        self.last_phase = ""
        self.last_edit_at = 0.0

    async def start(self):
        self.task = asyncio.create_task(self._worker())
        await self.report(1, "准备处理")

    def _enqueue(self, percent: float, phase: str):
        item = (max(0.0, min(100.0, percent)), phase)
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def emit_from_thread(self, loop, percent: float, phase: str):
        loop.call_soon_threadsafe(self._enqueue, percent, phase)

    async def report(self, percent: float, phase: str):
        self._enqueue(percent, phase)
        await asyncio.sleep(0)

    async def _worker(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            percent, phase = item
            now = time.monotonic()
            should_edit = (
                percent >= 99
                or phase != self.last_phase
                or percent - self.last_percent >= 3
                or now - self.last_edit_at >= 1.0
            )
            if not should_edit:
                continue
            self.last_percent = percent
            self.last_phase = phase
            self.last_edit_at = now
            try:
                await self.message.edit_text(
                    "📤 <b>上传进度</b>\n\n"
                    f"<code>{progress_bar(percent)}</code> {percent:.0f}%\n"
                    f"阶段：{html.escape(phase)}\n"
                    f"文件：<code>{html.escape(self.filename)}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    async def stop(self):
        if not self.task:
            return
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(None)
        try:
            await self.task
        except Exception:
            pass


def download_stream_sync(url: str, expected_size: int, progress_callback=None) -> bytes:
    chunks = []
    downloaded = 0
    with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        total = expected_size or int(response.headers.get("Content-Length") or 0)
        for chunk in response.iter_content(chunk_size=256 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            downloaded += len(chunk)
            if progress_callback and total > 0:
                progress_callback(min(1.0, downloaded / total))
    if progress_callback:
        progress_callback(1.0)
    return b"".join(chunks)


async def download_telegram_file(update: Update, reporter=None) -> Tuple[bytes, str, str]:
    message = update.effective_message
    media = None
    filename = ""
    mime = "application/octet-stream"
    expected_size = 0

    if message.photo:
        media = message.photo[-1]
        filename = f"{media.file_unique_id}.jpg"
        mime = "image/jpeg"
        expected_size = int(media.file_size or 0)
    elif message.video:
        media = message.video
        filename = media.file_name or f"{media.file_unique_id}.mp4"
        mime = media.mime_type or "video/mp4"
        expected_size = int(media.file_size or 0)
    elif message.document:
        media = message.document
        filename = media.file_name or f"{media.file_unique_id}"
        mime = media.mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        expected_size = int(media.file_size or 0)
    else:
        raise ValueError("不支持的消息类型")

    if reporter:
        reporter.filename = filename
        await reporter.report(3, "正在获取 Telegram 文件")

    tg_file = await media.get_file()
    file_url = str(tg_file.file_path or "")
    if not file_url:
        raise RuntimeError("Telegram 未返回文件下载地址")
    if not file_url.startswith(("http://", "https://")):
        file_url = f"https://api.telegram.org/file/bot{current_bot_token()}/{file_url.lstrip('/')}"

    loop = asyncio.get_running_loop()

    def progress(fraction: float):
        if reporter:
            reporter.emit_from_thread(loop, 5 + fraction * 30, "正在从 Telegram 下载")

    data = await asyncio.to_thread(
        download_stream_sync,
        file_url,
        expected_size,
        progress,
    )
    return data, filename, mime


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
    status = await update.effective_message.reply_text("📤 正在准备上传…")
    reporter = UploadProgressReporter(status, "Telegram 文件")
    await reporter.start()

    try:
        data, filename, mime = await download_telegram_file(update, reporter)

        if mime.startswith("image/"):
            await reporter.report(38, "正在处理图片")
            processed, filename, mime = await asyncio.to_thread(
                preprocess_file, data, filename, mime, settings
            )
        else:
            processed = data
            await reporter.report(42, "正在准备视频/文件上传")

        reporter.filename = filename
        await reporter.report(45, "正在上传到图床")
        url = await upload_file(processed, filename, mime, settings, reporter)
        await reporter.report(99, "正在生成访问链接")
        await asyncio.sleep(0.2)
        await reporter.stop()

        text = (
            "✅ <b>上传成功！</b>\n\n"
            f"🔗 URL:\n<code>{html.escape(url)}</code>\n\n"
            f"📝 Markdown:\n<code>![]({html.escape(url)})</code>"
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
        await reporter.stop()
        await status.edit_text(
            "❌ <b>上传失败</b>\n\n"
            f"<code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )


async def post_init(application: Application) -> None:
    # 删除 Telegram 左侧“菜单/命令”入口。Reply Keyboard 的展开/收起由客户端输入框旁按钮控制。
    await application.bot.delete_my_commands()

    # 如果升级过程中 Bot 被重启，继续追踪上一次升级并自动反馈最终结果。
    if get_meta("update_pending_chat"):
        application.create_task(monitor_update(application))


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
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, handle_upload))
    app.add_handler(MessageHandler(
        filters.Regex(r"^(⚙️ 上传设置|📦 存储渠道|👤 当前配置|🕘 最近上传|🌐 打开图床|⬆️ 版本升级)$"),
        menu_button_handler,
    ))
    # 设置输入允许以 / 开头，例如上传目录 /telegram；未知斜杠文本也可作为设置值处理。
    app.add_handler(MessageHandler(filters.TEXT, setting_text_input))
    logger.info("ImgBed Telegram Bot %s started", BOT_VERSION)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
