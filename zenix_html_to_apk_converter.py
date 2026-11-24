import io
import logging
import os
import re
import threading
import time
from html import escape

import requests
import telebot
from PIL import Image
from telebot import types

# Set BOT_TOKEN in your hosting platform's environment variables.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_URL = "https://app-creator-x.ueu.workers.dev/api/v1/build"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANNER_PATH = os.path.join(BASE_DIR, "zenix_banner.jpg")

MAX_HTML_SIZE = 10 * 1024 * 1024
MAX_ZIP_SIZE = 10 * 1024 * 1024
MAX_ICON_SIZE = 6 * 1024 * 1024
MAX_ICON_PIXELS = 4_000_000
MAX_TOTAL_BODY = 16_842_752
MAX_TOTAL_FILES = MAX_TOTAL_BODY - 64 * 1024
MAX_APP_NAME_UTF16 = 80
MAX_APP_NAME_UTF8 = 120

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("zenix_html_to_apk")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise SystemExit("Set a valid BOT_TOKEN environment variable before running.")
if not os.path.isfile(BANNER_PATH):
    raise SystemExit("zenix_banner.jpg is missing. Keep it in the same folder as this Python file.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
sessions = {}
STARTED_AT = time.time()


def kb_main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🚀 Create APK", callback_data="menu:new"))
    kb.add(
        types.InlineKeyboardButton("📡 Live Bot Status", callback_data="menu:status"),
        types.InlineKeyboardButton("📘 How It Works", callback_data="menu:help"),
    )
    kb.add(types.InlineKeyboardButton("💎 About Zenix", callback_data="menu:about"))
    return kb


def kb_cancel():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✖️ Cancel", callback_data="flow:cancel"))
    return kb


def kb_back():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home"))
    return kb


def kb_confirm():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Build APK", callback_data="flow:build"),
        types.InlineKeyboardButton("✏️ Rename", callback_data="flow:rename"),
    )
    kb.add(types.InlineKeyboardButton("✖️ Cancel", callback_data="flow:cancel"))
    return kb


def progress(step, label):
    labels = ["App Name", "Website", "App Icon", "Review", "Build"]
    cells = ["✅" if i < step else ("🟣" if i == step else "⚪") for i in range(5)]
    return f"<b>STEP {step + 1}/5 — {escape(label or labels[step])}</b>\n{'  '.join(cells)}\n\n"


def reset(chat_id):
    sessions.pop(chat_id, None)


def safe_name(value):
    return escape(value or "")


def validate_app_name(value):
    value = (value or "").strip()
    if not value:
        return "Please send a non-empty app name."
    utf16_units = len(value.encode("utf-16-le")) // 2
    utf8_bytes = len(value.encode("utf-8"))
    if utf16_units > MAX_APP_NAME_UTF16:
        return f"Name is too long ({utf16_units}/80 UTF-16 units)."
    if utf8_bytes > MAX_APP_NAME_UTF8:
        return f"Name is too large ({utf8_bytes}/120 UTF-8 bytes)."
    if any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value):
        return "The app name cannot contain control characters."
    return None


def validate_icon(content):
    if not content:
        return "The icon file is empty."
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ("PNG", "JPEG"):
                return "Only PNG and JPEG icons are supported."
            if image.width * image.height > MAX_ICON_PIXELS:
                return "The icon is too large: maximum is 4 megapixels."
            if image.format == "PNG" and image.info.get("interlace"):
                return "Interlaced PNG icons are not supported. Please export a regular PNG."
    except Exception:
        return "That file is not a valid PNG or JPEG image."
    return None


def send_home(chat_id, intro=True):
    caption = (
        "<b>ZENIX HTML TO APK CONVERTER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 <b>Website</b> ➜ 📱 <b>Android APK</b>\n\n"
        "Convert your HTML website or ZIP project into an Android test APK.\n\n"
        "🟢 <b>Bot Status:</b> ONLINE\n"
        "⚡ <i>Fast • Simple • Easy to use</i>"
        if intro else
        "<b>ZENIX HTML TO APK CONVERTER</b>\n\nChoose an option below:"
    )
    with open(BANNER_PATH, "rb") as photo:
        bot.send_photo(chat_id, photo, caption=caption, reply_markup=kb_main_menu())


def edit_message(message, text, markup=None):
    try:
        if message.content_type == "photo":
            bot.edit_message_caption(text, message.chat.id, message.message_id, reply_markup=markup)
        else:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=markup)
    except Exception as exc:
        logger.debug("Message edit failed: %s", exc)


def flow_screen(chat_id, text, markup=None, source=None):
    session = sessions.get(chat_id)
    if session is None:
        return None
    msg = source or bot.send_message(chat_id, text, reply_markup=markup)
    if source:
        edit_message(source, text, markup)
    session["ui_message"] = msg
    return msg


def flow_next(chat_id, text, handler, *args, source=None, markup=None):
    msg = flow_screen(chat_id, text, markup or kb_cancel(), source=source)
    if msg:
        bot.register_next_step_handler(msg, handler, *args)
    return msg


def api_error_message(resp):
    try:
        payload = resp.json()
        detail = payload.get("error", resp.text)
    except ValueError:
        detail = resp.text or f"HTTP {resp.status_code}"
    help_text = {
        400: "Please check the app name and uploaded files.",
        413: "The complete request is too large. Try smaller files.",
        415: "The upload format was rejected. Please try again.",
        429: "The build service is rate-limited. Wait a minute and retry.",
        500: "The build service failed unexpectedly. Please retry later.",
    }.get(resp.status_code, "Please try again later.")
    return f"{escape(str(detail))}\n\n<i>{help_text}</i>"


def bot_status_text():
    # This check confirms the bot process can reach Telegram right now.
    try:
        me = bot.get_me()
        uptime = int(time.time() - STARTED_AT)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        return (
            "<b>📡 ZENIX LIVE BOT STATUS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🟢 <b>Bot:</b> ONLINE\n"
            "🟢 <b>Telegram:</b> CONNECTED\n"
            f"🤖 <b>Bot Name:</b> {escape(me.first_name or 'Zenix')}\n"
            f"⏱️ <b>Uptime:</b> {hours}h {minutes}m {seconds}s\n\n"
            "<i>Status checked just now.</i>"
        )
    except Exception:
        return (
            "<b>📡 ZENIX LIVE BOT STATUS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔴 <b>Telegram:</b> CONNECTION ERROR\n\n"
            "<i>Please try refreshing in a moment.</i>"
        )


def animate_build(message, stop_event):
    stages = [
        ("Uploading website", 22),
        ("Preparing Android WebView", 43),
        ("Compiling Android package", 68),
        ("Signing test build", 87),
        ("Finalizing APK", 96),
    ]
    index = 0
    while not stop_event.is_set():
        label, percent = stages[index % len(stages)]
        filled = percent // 10
        bar = "🟪" * filled + "▫️" * (10 - filled)
        edit_message(
            message,
            "<b>ZENIX HTML TO APK CONVERTER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚙️ <b>BUILD IN PROGRESS</b>\n\n"
            f"🔧 {label}\n{bar} <b>{percent}%</b>\n\n"
            "☕ Please wait while your APK is being prepared…",
        )
        index += 1
        stop_event.wait(1.5)


HELP_TEXT = (
    "<b>📘 HOW IT WORKS</b>\n\n"
    "<b>1.</b> Tap <b>Create APK</b>.\n"
    "<b>2.</b> Enter your app name.\n"
    "<b>3.</b> Upload an <code>.html</code>, <code>.htm</code>, or ZIP containing <code>index.html</code>.\n"
    "<b>4.</b> Upload a PNG/JPEG app icon.\n"
    "<b>5.</b> Review your details and build.\n\n"
    "📦 HTML/ZIP maximum: 10 MiB\n"
    "🖼️ Icon maximum: 6 MiB and 4 megapixels\n\n"
    "⚠️ Builds are for testing. Each APK uses a temporary signing key and cannot update an earlier build."
)

ABOUT_TEXT = (
    "<b>💎 ABOUT ZENIX HTML TO APK CONVERTER</b>\n\n"
    "A Telegram bot interface for converting HTML websites and ZIP projects into Android test APKs.\n\n"
    "<b>Supported:</b> HTML, HTM, ZIP, PNG, JPEG\n"
    "<b>Output:</b> Android test APK\n\n"
    "⚡ <i>Powered by CreatorX Build API</i>"
)


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    reset(message.chat.id)
    send_home(message.chat.id)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=kb_back())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    reset(message.chat.id)
    bot.send_message(message.chat.id, "✖️ <b>Cancelled.</b>\n\nChoose an option below:", reply_markup=kb_main_menu())


@bot.message_handler(commands=["newapk"])
def cmd_newapk(message):
    start_flow(message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu:home")
def cb_home(call):
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id)
    edit_message(call.message, "<b>ZENIX HTML TO APK CONVERTER</b>\n\nChoose an option below:", kb_main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "menu:status")
def cb_status(call):
    bot.answer_callback_query(call.id, "Checking bot status…")
    edit_message(call.message, bot_status_text(), kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:help")
def cb_help(call):
    bot.answer_callback_query(call.id)
    edit_message(call.message, HELP_TEXT, kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:about")
def cb_about(call):
    bot.answer_callback_query(call.id)
    edit_message(call.message, ABOUT_TEXT, kb_back())


@bot.callback_query_handler(func=lambda c: c.data == "menu:new")
def cb_new(call):
    bot.answer_callback_query(call.id)
    start_flow(call.message.chat.id, edit=call.message)


@bot.callback_query_handler(func=lambda c: c.data == "flow:cancel")
def cb_cancel(call):
    reset(call.message.chat.id)
    bot.answer_callback_query(call.id, "Cancelled")
    edit_message(call.message, "✖️ <b>Cancelled.</b>\n\nChoose an option below:", kb_main_menu())


@bot.callback_query_handler(func=lambda c: c.data == "flow:rename")
def cb_rename(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(call.id, "Session expired. Start again.")
        return
    bot.answer_callback_query(call.id)
    sessions[chat_id]["ui_message"] = call.message
    flow_next(chat_id, progress(0, "New App Name") + "📛 <b>Send the new app name:</b>", step_app_name, True)


@bot.callback_query_handler(func=lambda c: c.data == "flow:build")
def cb_build(call):
    chat_id = call.message.chat.id
    if chat_id not in sessions or "icon" not in sessions[chat_id]:
        bot.answer_callback_query(call.id, "Session expired. Start again.")
        return
    bot.answer_callback_query(call.id, "Build started")
    sessions[chat_id]["ui_message"] = call.message
    flow_screen(chat_id, progress(4, "Building") + "⚙️ <b>Preparing your APK…</b>\n\n<i>Please wait. This may take a little time.</i>", source=call.message)
    threading.Thread(target=build_apk, args=(chat_id,), daemon=True).start()


def start_flow(chat_id, edit=None):
    sessions[chat_id] = {}
    text = progress(0, "App Name") + "📛 <b>What should your app be called?</b>\n\nSend a name from 1–80 UTF-16 units."
    flow_next(chat_id, text, step_app_name, source=edit)


def step_app_name(message, rename=False):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    name = (message.text or "").strip()
    error = validate_app_name(name)
    if error:
        flow_next(chat_id, progress(0, "App Name") + f"⚠️ <b>Invalid app name</b>\n\n{escape(error)}\n\nTry again:", step_app_name, rename)
        return
    sessions[chat_id]["app_name"] = name
    if rename:
        show_confirmation(chat_id)
        return
    flow_next(chat_id, progress(1, "Website") + f"✅ Name saved: <b>{safe_name(name)}</b>\n\n📄 <b>Upload your website</b>\nSend an <code>.html</code>, <code>.htm</code>, or <code>.zip</code> document.", step_html_file)


def step_html_file(message):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    if not message.document:
        flow_next(chat_id, progress(1, "Website") + "⚠️ <b>File required</b>\n\nUpload your HTML/ZIP as a document:", step_html_file)
        return
    doc = message.document
    filename = doc.file_name or "index.html"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("html", "htm", "zip"):
        flow_next(chat_id, progress(1, "Website") + "⚠️ Only <code>.html</code>, <code>.htm</code>, or <code>.zip</code> files are supported:", step_html_file)
        return
    limit = MAX_ZIP_SIZE if ext == "zip" else MAX_HTML_SIZE
    if doc.file_size and doc.file_size > limit:
        flow_next(chat_id, progress(1, "Website") + f"⚠️ File too large. Maximum: {limit:,} bytes. Send a smaller file:", step_html_file)
        return
    try:
        info = bot.get_file(doc.file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        logger.exception("Website download failed")
        flow_next(chat_id, progress(1, "Website") + f"⚠️ Download failed: {escape(str(exc))}\n\nTry again:", step_html_file)
        return
    if len(content) > limit:
        flow_next(chat_id, progress(1, "Website") + f"⚠️ File too large. Maximum: {limit:,} bytes. Send a smaller file:", step_html_file)
        return
    sessions[chat_id]["html"] = (filename, content)
    flow_next(chat_id, progress(2, "App Icon") + f"✅ Website received: <code>{escape(filename)}</code>\n\n🖼️ <b>Upload your app icon</b>\nPNG/JPEG, max 6 MiB and 4 megapixels.", step_icon_file)


def step_icon_file(message):
    chat_id = message.chat.id
    if chat_id not in sessions:
        return
    filename = "icon.jpg"
    if message.document:
        doc = message.document
        filename = doc.file_name or "icon.png"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ("png", "jpg", "jpeg"):
            flow_next(chat_id, progress(2, "App Icon") + "⚠️ Please upload a PNG or JPEG icon:", step_icon_file)
            return
        if doc.file_size and doc.file_size > MAX_ICON_SIZE:
            flow_next(chat_id, progress(2, "App Icon") + "⚠️ Icon exceeds 6 MiB. Send a smaller image:", step_icon_file)
            return
        file_id = doc.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
        filename = "icon.jpg"
    else:
        flow_next(chat_id, progress(2, "App Icon") + "⚠️ Icon required. Send a PNG or JPEG image:", step_icon_file)
        return
    try:
        info = bot.get_file(file_id)
        content = bot.download_file(info.file_path)
    except Exception as exc:
        logger.exception("Icon download failed")
        flow_next(chat_id, progress(2, "App Icon") + f"⚠️ Icon download failed: {escape(str(exc))}\n\nTry again:", step_icon_file)
        return
    if len(content) > MAX_ICON_SIZE:
        flow_next(chat_id, progress(2, "App Icon") + "⚠️ Icon exceeds 6 MiB. Send a smaller image:", step_icon_file)
        return
    error = validate_icon(content)
    if error:
        flow_next(chat_id, progress(2, "App Icon") + f"⚠️ Invalid icon: {escape(error)}\n\nSend another PNG/JPEG:", step_icon_file)
        return
    if len(sessions[chat_id]["html"][1]) + len(content) > MAX_TOTAL_FILES:
        reset(chat_id)
        bot.send_message(chat_id, "⚠️ Website and icon are too large together. Start again with smaller files.", reply_markup=kb_main_menu())
        return
    sessions[chat_id]["icon"] = (filename, content)
    show_confirmation(chat_id)


def show_confirmation(chat_id):
    session = sessions[chat_id]
    html_name, html_bytes = session["html"]
    icon_name, icon_bytes = session["icon"]
    text = (
        progress(3, "Review") + "<b>📋 READY TO BUILD</b>\n\n"
        f"📛 <b>App Name:</b> {safe_name(session['app_name'])}\n"
        f"📄 <b>Website:</b> <code>{escape(html_name)}</code> ({len(html_bytes):,} bytes)\n"
        f"🖼️ <b>Icon:</b> <code>{escape(icon_name)}</code> ({len(icon_bytes):,} bytes)\n\n"
        "Everything looks good. Start building your APK?"
    )
    flow_screen(chat_id, text, kb_confirm())


def build_apk(chat_id):
    session = sessions.get(chat_id)
    if not session:
        return
    ui_message = session.get("ui_message")
    stop_event = threading.Event()
    animation = threading.Thread(target=animate_build, args=(ui_message, stop_event), daemon=True) if ui_message else None
    if animation:
        animation.start()
    app_name = session["app_name"]
    html_name, html_bytes = session["html"]
    icon_name, icon_bytes = session["icon"]
    html_mime = "application/zip" if html_name.lower().endswith(".zip") else "text/html"
    icon_mime = "image/png" if icon_name.lower().endswith(".png") else "image/jpeg"
    files = {
        "html": (html_name, html_bytes, html_mime),
        "icon": (icon_name, icon_bytes, icon_mime),
    }
    try:
        response = requests.post(API_URL, data={"app_name": app_name}, files=files, timeout=120)
        stop_event.set()
        if animation:
            animation.join(timeout=2)
        content_type = response.headers.get("Content-Type", "")
        if response.status_code == 200 and "application/vnd.android.package-archive" in content_type:
            if ui_message:
                edit_message(ui_message, "<b>✅ APK BUILD COMPLETE</b>\n\n📦 Your APK is ready. Sending it now…")
            disposition = response.headers.get("Content-Disposition", "")
            match = re.search(r'filename=(?:"([^"]+)"|([^;]+))', disposition)
            apk_name = (match.group(1) or match.group(2)).strip() if match else f"{app_name}.apk"
            if not apk_name.lower().endswith(".apk"):
                apk_name += ".apk"
            bot.send_document(
                chat_id,
                (apk_name, response.content),
                caption=(
                    f"🎉 <b>Build complete!</b>\n\n✅ <code>{escape(apk_name)}</code> is ready.\n\n"
                    "⚠️ Test build only — this APK uses a temporary signing key and cannot update an earlier build."
                ),
                reply_markup=kb_main_menu(),
            )
        else:
            error_text = f"❌ <b>BUILD FAILED</b> ({response.status_code})\n\n{api_error_message(response)}"
            if ui_message:
                edit_message(ui_message, error_text, kb_main_menu())
            else:
                bot.send_message(chat_id, error_text, reply_markup=kb_main_menu())
    except requests.RequestException as exc:
        logger.exception("Build API request failed")
        stop_event.set()
        if animation:
            animation.join(timeout=2)
        error_text = f"❌ <b>BUILD SERVICE UNAVAILABLE</b>\n\n{escape(str(exc))}\n\nPlease try again later."
        if ui_message:
            edit_message(ui_message, error_text, kb_main_menu())
        else:
            bot.send_message(chat_id, error_text, reply_markup=kb_main_menu())
    finally:
        stop_event.set()
        reset(chat_id)


@bot.message_handler(func=lambda m: True, content_types=["text", "document", "photo"])
def fallback(message):
    bot.send_message(message.chat.id, "Choose an option below, or use /cancel to reset:", reply_markup=kb_main_menu())


if __name__ == "__main__":
    logger.info("ZENIX HTML TO APK CONVERTER starting…")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
