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

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- FAKE WEB SERVER (To keep Render awake) ---
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is alive and running!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host='0.0.0.0', port=port)

# --- CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DOWNLOAD_DIR = "downloads"

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# --- HELPER: STREAM TO GOFILE (For Large Files > 50MB) ---
async def stream_to_gofile(url, format_type):
    """
    Streams data from yt-dlp -> curl -> GoFile.
    Never saves the full file to disk, allowing 2GB+ uploads on free tier.
    """
    try:
        # 1. Get the best GoFile server
        api_resp = requests.get("https://api.gofile.io/getServer").json()
        if api_resp['status'] != 'ok':
            return None, "GoFile API unavailable"
        server = api_resp['data']['server']
        upload_url = f"https://{server}.gofile.io/uploadFile"

        # 2. Prepare filenames and commands
        # We use a shell pipe: yt-dlp outputs to stdout (-o -) -> curl reads from stdin (file=@-)
        if format_type == 'mp3':
            filename = "audio.mp3"
            cmd = f'yt-dlp -f bestaudio -o - "{url}" | curl -F "file=@-;filename={filename}" {upload_url}'
        else:
            filename = "video.mp4"
            cmd = f'yt-dlp -f best -o - "{url}" | curl -F "file=@-;filename={filename}" {upload_url}'

        # 3. Execute the pipeline
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        # 4. Parse response
        response_text = stdout.decode().strip()
        if not response_text:
            logger.error(f"GoFile Error: {stderr.decode()}")
            return None, "Upload failed (Check logs)"

        json_resp = json.loads(response_text)
        if json_resp['status'] == 'ok':
            return json_resp['data']['downloadPage'], None
        else:
            return None, "GoFile upload failed."

    except Exception as e:
        logger.error(f"Stream Error: {e}")
        return None, str(e)

# --- HELPER: LOCAL DOWNLOAD (For Small Files < 50MB) ---
def download_local(url, format_type, chat_id):
    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/{chat_id}_%(title)s.%(ext)s',
        'quiet': True,
        'noplaylist': True,
        'writethumbnail': True,
    }

    if format_type == "mp3":
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                {'key': 'FFmpegMetadata', 'add_metadata': True},
                {'key': 'EmbedThumbnail'},
            ],
        })
    else:
        ydl_opts.update({
            'format': 'best[filesize<50M]', # Strict limit for Telegram API
            'postprocessors': [{'key': 'FFmpegMetadata', 'add_metadata': True}],
        })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # yt-dlp might change extension after conversion (e.g. webm -> mp3)
        if format_type == "mp3":
            base = filename.rsplit(".", 1)[0]
            if os.path.exists(base + ".mp3"):
                filename = base + ".mp3"
                
        return filename, info.get('title', 'Media'), info.get('uploader', 'Unknown')

# --- BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me a link! I handle both small (<50MB) and HUGE files!")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if not url.startswith("http"):
        await update.message.reply_text("Please send a valid HTTP link.")
        return

    # Create buttons
    keyboard = [
        [
            InlineKeyboardButton("🎵 MP3 (Audio)", callback_data=f"mp3|{url}"),
            InlineKeyboardButton("🎬 MP4 (Video)", callback_data=f"mp4|{url}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Choose format:", reply_markup=reply_markup)

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Stop loading animation
    
    try:
        data = query.data.split("|", 1)
        format_type = data[0]
        url = data[1]
    except IndexError:
        await query.edit_message_text("❌ Error processing link.")
        return
    
    await query.edit_message_text(f"⏳ Analyzing file size for {format_type.upper()}...")
    
    # 1. Check File Size (Head Request via yt-dlp)
    limit_bytes = 50 * 1024 * 1024 # 50MB
    is_large_file = False
    
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            
            if filesize > limit_bytes or filesize == 0:
                is_large_file = True
    except Exception as e:
        # If check fails, default to Large File mode to be safe
        is_large_file = True

    # --- ROUTE A: LARGE FILE (Stream to GoFile) ---
    if is_large_file:
        await query.edit_message_text(
            f"📦 File is large (>50MB).\nTelegram can't handle this directly.\n"
            f"🚀 **Streaming to GoFile...** (This generates a download link)"
        )
        
        link, error = await stream_to_gofile(url, format_type)
        
        if link:
            await query.edit_message_text(
                f"✅ **Download Ready!**\n\n"
                f"🔗 [Click here to download your {format_type.upper()}]({link})\n\n"
                f"_(Link hosted by GoFile)_",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(f"❌ Streaming failed: {error}")

    # --- ROUTE B: SMALL FILE (Direct Telegram Upload) ---
    else:
        await query.edit_message_text(f"⬇️ Downloading {format_type.upper()} locally...")
        
        file_path = None
        try:
            file_path, title, author = await asyncio.to_thread(download_local, url, format_type, query.message.chat_id)
            
            await query.edit_message_text(f"⬆️ Uploading to Telegram...")
            
            with open(file_path, 'rb') as f:
                if format_type == "mp3":
                    await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, title=title, performer=author)
                else:
                    await context.bot.send_video(chat_id=query.message.chat_id, video=f, caption=title)
            
            await query.delete_message()
            
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {str(e)}")
        finally:
            # Cleanup: Always delete the local file
            if file_path and os.path.exists(file_path):
                os.remove(file_path)

if __name__ == '__main__':
    # Start web server in background
    threading.Thread(target=run_web_server).start()

    if not BOT_TOKEN:
        print("Error: BOT_TOKEN not set in environment variables.")
    else:
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
        app.add_handler(CallbackQueryHandler(button_click))
        
        print("Bot is polling...")
        app.run_polling()