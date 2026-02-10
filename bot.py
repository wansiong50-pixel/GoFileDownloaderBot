import os
import asyncio
import threading
import logging
import json
import requests
import yt_dlp
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- FAKE SERVER ---
app_flask = Flask(__name__)
@app_flask.route('/')
def health_check(): return "Bot is running!"
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host='0.0.0.0', port=port)

# --- CONFIG ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

# --- HELPER: STREAM TO GOFILE ---
async def stream_to_gofile(url, format_string, filename):
    try:
        api = requests.get("https://api.gofile.io/getServer").json()
        if api['status'] != 'ok': return None, "GoFile API Error"
        server = api['data']['server']
        upload_url = f"https://{server}.gofile.io/uploadFile"

        # UPDATE: Added --extractor-args to pretend to be an Android phone
        # This bypasses the "Data Center" blocks on video formats
        cmd = (
            f'yt-dlp --cookies cookies.txt '
            f'--extractor-args "youtube:player_client=android" '
            f'-f "{format_string}" -o - "{url}" | '
            f'curl -F "file=@-;filename={filename}" {upload_url}'
        )

        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        response = stdout.decode().strip()
        if not response.startswith("{"):
            logger.error(f"GoFile Blocked: {response[:200]}")
            return None, "GoFile blocked request (Data Center IP)."

        data = json.loads(response)
        if data['status'] == 'ok':
            return data['data']['downloadPage'], None
        else:
            return None, "Upload failed."
    except Exception as e:
        return None, str(e)

# --- HELPER: LOCAL DOWNLOAD ---
def download_local(url, format_string, chat_id, is_audio=False):
    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/{chat_id}_%(title)s.%(ext)s',
        'quiet': True,
        'noplaylist': True,
        'format': format_string,
        'writethumbnail': True,
        'cookiefile': 'cookies.txt',
        # UPDATE: Force Android Client to see all formats
        'extractor_args': {'youtube': {'player_client': ['android']}},
    }

    if is_audio:
        ydl_opts['postprocessors'] = [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
            {'key': 'FFmpegMetadata', 'add_metadata': True},
            {'key': 'EmbedThumbnail'},
        ]
    else:
        ydl_opts['postprocessors'] = [
            {'key': 'FFmpegMetadata', 'add_metadata': True},
            {'key': 'EmbedThumbnail'}
        ]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if is_audio:
            base = filename.rsplit(".", 1)[0]
            if os.path.exists(base + ".mp3"): filename = base + ".mp3"
        return filename, info.get('title', 'Media'), info.get('uploader', 'Unknown')

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me a link! (Mobile Mode 📱)")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http"): return
    context.user_data['current_url'] = url
    
    keyboard = [
        [InlineKeyboardButton("🎵 Audio (MP3)", callback_data="type|mp3")],
        [InlineKeyboardButton("🎬 Video (Choose Quality)", callback_data="type|video")]
    ]
    await update.message.reply_text("Select Format:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|")
    action = data[0]
    
    url = context.user_data.get('current_url')
    if not url:
        await query.edit_message_text("❌ Link expired. Please send it again.")
        return

    # --- MENU NAVIGATION ---
    if action == "type" and data[1] == "video":
        keyboard = [
            [InlineKeyboardButton("🌟 1080p", callback_data="qual|1080")],
            [InlineKeyboardButton("📺 720p", callback_data="qual|720")],
            [InlineKeyboardButton("📱 480p", callback_data="qual|480")],
            [InlineKeyboardButton("📉 360p", callback_data="qual|360")],
            [InlineKeyboardButton("🔙 Back", callback_data="back|menu")]
        ]
        await query.edit_message_text("Select Quality:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "back":
        keyboard = [[InlineKeyboardButton("🎵 Audio", callback_data="type|mp3")], [InlineKeyboardButton("🎬 Video", callback_data="type|video")]]
        await query.edit_message_text("Select Format:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # --- EXECUTION LOGIC ---
    target_quality = data[1]
    await query.edit_message_text(f"⏳ Processing {target_quality.upper()}...")

    if action == "type" and target_quality == "mp3":
        fmt = "bestaudio/best"
        ext = "mp3"
        is_audio = True
    else: 
        # Relaxed format string: Try strict first, but fallback to 'best' if specific height is missing
        fmt = f"bestvideo[height<={target_quality}]+bestaudio/best[height<={target_quality}]/best"
        ext = "mp4"
        is_audio = False

    limit_50mb = 50 * 1024 * 1024
    use_gofile = False
    
    try:
        # UPDATE: Check size using Android Client args
        ydl_opts_check = {
            'quiet': True, 
            'format': fmt, 
            'cookiefile': 'cookies.txt',
            'extractor_args': {'youtube': {'player_client': ['android']}}
        }
        with yt_dlp.YoutubeDL(ydl_opts_check) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            
            if filesize > limit_50mb:
                use_gofile = True
            else:
                use_gofile = False 
                
    except Exception as e:
        logger.error(f"Size check error: {e}")
        use_gofile = False 

    # 2. RUN DOWNLOAD
    if use_gofile:
        await query.edit_message_text(f"📦 Large file detected (>50MB). Streaming to GoFile...")
        filename = f"video_{target_quality}.{ext}"
        link, error = await stream_to_gofile(url, fmt, filename)
        if link:
            await query.edit_message_text(f"✅ **Link Ready!**\n[Download Here]({link})", parse_mode='Markdown')
        else:
            await query.edit_message_text(f"❌ Streaming failed: {error}")
    else:
        await query.edit_message_text(f"⬇️ Downloading locally...")
        try:
            path, title, author = await asyncio.to_thread(download_local, url, fmt, query.message.chat_id, is_audio)
            
            await query.edit_message_text("⬆️ Uploading to Telegram...")
            with open(path, 'rb') as f:
                if is_audio:
                    await context.bot.send_audio(query.message.chat_id, audio=f, title=title, performer=author, thumbnail=open(path, 'rb'))
                else:
                    await context.bot.send_video(query.message.chat_id, video=f, caption=title)
            
            await query.delete_message()
            os.remove(path)
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {str(e)}")
            if 'path' in locals() and os.path.exists(path): os.remove(path)

if __name__ == '__main__':
    threading.Thread(target=run_web_server).start()
    if BOT_TOKEN:
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
        app.add_handler(CallbackQueryHandler(button_click))
        app.run_polling()
        
