import logging
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    ContextTypes, 
    MessageHandler, 
    CallbackQueryHandler, 
    CommandHandler, 
    filters, 
    AIORateLimiter 
)
from flask import Flask

# ==============================================================================
# ⚙️ CONFIGURATION (STRICTLY FROM ENVIRONMENT)
# ==============================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID_RAW = os.environ.get("ADMIN_ID")
# 👇 NOW CLEAN: If you forget to set the var, it just says "Link not set"
DONATION_LINK = os.environ.get("DONATION_LINK", "Link not set") 

if not BOT_TOKEN or not CHANNEL_ID or not ADMIN_ID_RAW:
    print("⚠️ CRITICAL: Environment Variables Missing. Bot will likely fail.")
else:
    try:
        ADMIN_ID = int(ADMIN_ID_RAW)
    except ValueError:
        raise ValueError("❌ ADMIN_ID must be a number!")

DAILY_LIMIT = 100

# ==============================================================================
# 🕒 TIMEZONE HELPER (MALAYSIA GMT+8)
# ==============================================================================
def get_malaysia_date():
    utc_now = datetime.now(timezone.utc)
    myt_now = utc_now + timedelta(hours=8)
    return myt_now.strftime("%Y-%m-%d")

# ==============================================================================
# 🌐 KEEP ALIVE SERVER
# ==============================================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running on Malaysia Time!"

def run_http():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_http)
    t.start()

# ==============================================================================
# 🗄️ DATABASE MANAGEMENT
# ==============================================================================
def init_db():
    conn = sqlite3.connect("quota.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_quota (
            user_id INTEGER PRIMARY KEY,
            date TEXT,
            count INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = sqlite3.connect("quota.db")
    cur = conn.cursor()
    row = cur.execute("SELECT user_id FROM banned_users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None

def ban_user_db(target_id):
    conn = sqlite3.connect("quota.db")
    try:
        conn.execute("INSERT INTO banned_users VALUES (?)", (target_id,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def unban_user_db(target_id):
    conn = sqlite3.connect("quota.db")
    conn.execute("DELETE FROM banned_users WHERE user_id=?", (target_id,))
    conn.commit()
    conn.close()

def check_and_update_quota(user_id):
    conn = sqlite3.connect("quota.db")
    cur = conn.cursor()
    today = get_malaysia_date()
    
    row = cur.execute("SELECT date, count FROM user_quota WHERE user_id=?", (user_id,)).fetchone()
    
    current_count = 0
    if row is None:
        cur.execute("INSERT INTO user_quota VALUES (?, ?, 1)", (user_id, today))
        current_count = 1
    else:
        last_date, count = row
        if last_date != today:
            current_count = 1
            cur.execute("UPDATE user_quota SET date=?, count=1 WHERE user_id=?", (today, user_id))
        else:
            current_count = count + 1
            cur.execute("UPDATE user_quota SET count=? WHERE user_id=?", (current_count, user_id))
            
    conn.commit()
    conn.close()
    return current_count

def get_current_quota(user_id):
    conn = sqlite3.connect("quota.db")
    cur = conn.cursor()
    row = cur.execute("SELECT count, date FROM user_quota WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    
    today = get_malaysia_date()
    if row and row[1] == today:
        return row[0]
    return 0

# ==============================================================================
# 👮 ADMIN COMMANDS
# ==============================================================================
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        target_id = int(context.args[0])
        if ban_user_db(target_id):
            await update.message.reply_text(f"✅ User `{target_id}` BANNED.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ User `{target_id}` is already banned.")
    except:
        await update.message.reply_text("❌ Usage: `/ban 123456789`")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        target_id = int(context.args[0])
        unban_user_db(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` UNBANNED.", parse_mode="Markdown")
    except:
        await update.message.reply_text("❌ Usage: `/unban 123456789`")

# ==============================================================================
# 📩 MAIN CONFESSION HANDLER
# ==============================================================================
async def confession_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message
    
    if is_banned(user_id): return 

    sent_msg = None 

    # --- PHOTO ---
    if msg.photo:
        if user_id != ADMIN_ID:
            await msg.reply_text("❌ Maaf, buat masa ni hanya Admin boleh hantar gambar! 🙏")
            return
        caption = msg.caption if msg.caption else ""
        try:
            sent_msg = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=msg.photo[-1].file_id, caption=caption)
        except Exception as e:
            await msg.reply_text(f"❌ Error sending photo: {e}")
            return

    # --- TEXT ---
    elif msg.text:
        usage = get_current_quota(user_id)
        if usage >= DAILY_LIMIT:
            await msg.reply_text("❌ Limit harian (100) dah habis. Cuba lagi esok ya!")
            return

        try:
            sent_msg = await context.bot.send_message(chat_id=CHANNEL_ID, text=msg.text)
        except Exception as e:
            await msg.reply_text(f"❌ Error sending to channel: {e}")
            return
            
        check_and_update_quota(user_id)

    # --- REPLY ---
    if sent_msg:
        final_usage = get_current_quota(user_id)
        link = f"https://t.me/{CHANNEL_ID.replace('@', '')}/{sent_msg.message_id}"
        
        # Admin Log (with link for easy tracking)
        timestamp = get_malaysia_date() + " " + datetime.now(timezone.utc).strftime("%H:%M:%S") + " MYT"
        confession_preview = (msg.text[:100] + "...") if msg.text and len(msg.text) > 100 else (msg.text or "[Photo]")
        log_text = (
            f"🚨 **Confession Log**\n"
            f"⏰ {timestamp}\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"💬 **Msg:** {confession_preview}\n"
            f"🔗 **Link:** {link}\n"
            f"🚫 To ban: `/ban {user_id}`"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=log_text, parse_mode="Markdown")
        except: pass
        
        keyboard = [[InlineKeyboardButton("🔎 See Message", url=link)]]
        if user_id == ADMIN_ID:
            keyboard.append([InlineKeyboardButton("📌 Pin Post", callback_data=f"pin_{sent_msg.message_id}")])

        reply_text = (
            "🎉yeayy mesej min dah hantar dalam channel ye🎉🤭 jgn nakal\n"
            "ii tau min ban nanti ❗\n\n"
            "link donation untuk admin\n"
            f"{DONATION_LINK}\n\n"
            "This bot is built with @ForwardBuilderBot\n"
            "-----------------------------------------\n"
            "**Free Daily Quota**\n"
            f"Text: {final_usage}/{DAILY_LIMIT}\n"
            "Media: 0/0"
        )
        await msg.reply_text(reply_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def pin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID: return

    try:
        await context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=int(query.data.split("_")[1]))
        await context.bot.send_message(chat_id=query.from_user.id, text="✅ Pinned!")
    except Exception as e:
        await context.bot.send_message(chat_id=query.from_user.id, text=f"❌ Failed: {e}")

if __name__ == '__main__':
    init_db()
    keep_alive()
    
    if os.environ.get("BOT_TOKEN"): print("✅ Env Vars Found.")
    else: print("⚠️ Warning: Env Vars Missing.")

    app_bot = ApplicationBuilder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()
    
    app_bot.add_handler(CommandHandler("ban", ban_command))
    app_bot.add_handler(CommandHandler("unban", unban_command))
    app_bot.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, confession_handler))
    app_bot.add_handler(CallbackQueryHandler(pin_callback, pattern="^pin_"))
    
    print("Bot is running...")
    app_bot.run_polling()