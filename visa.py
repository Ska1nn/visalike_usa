# visa.py
import time
import json
import logging
import random
import requests
import configparser
import threading
import asyncio
import os
import sys
import datetime
import sqlite3

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)

# ---------- Selenium ----------
from selenium import webdriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait as Wait
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ---------- Embassy ----------
from embassy import Embassies

# ---------- DB ----------
DB_PATH = "accounts.db"
ACTIVE_ACCOUNT_FILE = "active_account_id.txt"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            schedule_id TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            embassy_code TEXT NOT NULL,
            owner_telegram_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_account(data, owner_telegram_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO accounts (
            username, password, schedule_id, period_start, period_end, embassy_code, owner_telegram_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data["USERNAME"],
        data["PASSWORD"],
        data["SCHEDULE_ID"],
        data["PERIOD_START"],
        data["PERIOD_END"],
        data["YOUR_EMBASSY"],
        owner_telegram_id
    ))
    conn.commit()
    aid = cursor.lastrowid
    conn.close()
    return aid

def get_accounts_for_user(owner_telegram_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, embassy_code, period_start, period_end, created_at FROM accounts WHERE owner_telegram_id = ?",
        (owner_telegram_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_account_by_id(aid):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM accounts WHERE id = ?", (aid,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "USERNAME": row[1],
            "PASSWORD": row[2],
            "SCHEDULE_ID": row[3],
            "PERIOD_START": row[4],
            "PERIOD_END": row[5],
            "YOUR_EMBASSY": row[6],
            "OWNER_TELEGRAM_ID": row[7],
            "created_at": row[8],
        }
    return None

def update_account_field(aid, field, value, owner_telegram_id):
    allowed_fields = {
        "username": "username",
        "password": "password",
        "schedule_id": "schedule_id",
        "period_start": "period_start",
        "period_end": "period_end",
        "embassy_code": "embassy_code"
    }
    col = allowed_fields.get(field)
    if not col:
        return False
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(f"UPDATE accounts SET {col} = ? WHERE id = ? AND owner_telegram_id = ?", (value, aid, owner_telegram_id))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated

def delete_account_by_id(aid, owner_telegram_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM accounts WHERE id = ? AND owner_telegram_id = ?",
        (aid, owner_telegram_id)
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def set_active_account(aid):
    with open(ACTIVE_ACCOUNT_FILE, "w") as f:
        f.write(str(aid))

def load_active_account():
    try:
        with open(ACTIVE_ACCOUNT_FILE, "r") as f:
            aid = int(f.read().strip())
        return get_account_by_id(aid)
    except Exception:
        return None

# ---------- Runtime Config ----------
class RuntimeConfig:
    def __init__(self):
        self.OWNER_TELEGRAM_ID = None
        self.load()

    def load(self):
        acc = load_active_account()
        if acc:
            self.USERNAME = acc["USERNAME"]
            self.PASSWORD = acc["PASSWORD"]
            self.SCHEDULE_ID = acc["SCHEDULE_ID"]
            self.PERIOD_START = acc["PERIOD_START"]
            self.PERIOD_END = acc["PERIOD_END"]
            self.YOUR_EMBASSY = acc["YOUR_EMBASSY"]
            self.OWNER_TELEGRAM_ID = acc["OWNER_TELEGRAM_ID"]
        else:
            config = configparser.ConfigParser()
            config.read("config.ini")
            self.USERNAME = config["PERSONAL_INFO"]["USERNAME"]
            self.PASSWORD = config["PERSONAL_INFO"]["PASSWORD"]
            self.SCHEDULE_ID = config["PERSONAL_INFO"]["SCHEDULE_ID"]
            self.PERIOD_START = config["PERSONAL_INFO"]["PERIOD_START"]
            self.PERIOD_END = config["PERSONAL_INFO"]["PERIOD_END"]
            self.YOUR_EMBASSY = config["PERSONAL_INFO"]["YOUR_EMBASSY"]
            self.OWNER_TELEGRAM_ID = int(config["TELEGRAM"].get("CHAT_ID", 0))

        self.EMBASSY = Embassies[self.YOUR_EMBASSY][0]
        self.FACILITY_ID = Embassies[self.YOUR_EMBASSY][1]
        self.REGEX_CONTINUE = Embassies[self.YOUR_EMBASSY][2]
        self.rebuild_urls()

    def rebuild_urls(self):
        # ✅ ИСПРАВЛЕНО: УБРАНЫ ВСЕ ПРОБЕЛЫ!
        self.SIGN_IN_LINK = f"https://ais.usvisa-info.com/{self.EMBASSY}/niv/users/sign_in"
        self.APPOINTMENT_URL = f"https://ais.usvisa-info.com/{self.EMBASSY}/niv/schedule/{self.SCHEDULE_ID}/appointment"
        self.DATE_URL = f"https://ais.usvisa-info.com/{self.EMBASSY}/niv/schedule/{self.SCHEDULE_ID}/appointment/days/{self.FACILITY_ID}.json?appointments[expedite]=false"
        self.TIME_URL = f"https://ais.usvisa-info.com/{self.EMBASSY}/niv/schedule/{self.SCHEDULE_ID}/appointment/times/{self.FACILITY_ID}.json?date=%s&appointments[expedite]=false"
        self.SIGN_OUT_LINK = f"https://ais.usvisa-info.com/{self.EMBASSY}/niv/users/sign_out"

# ---------- Global Vars ----------
runtime = RuntimeConfig()
LOG_FILE_NAME = "log_" + str(datetime.date.today()) + ".txt"

config = configparser.ConfigParser()
config.read("config.ini")
BOT_TOKEN = config["TELEGRAM"].get("BOT_TOKEN")
SENDGRID_API_KEY = config["NOTIFICATION"].get("SENDGRID_API_KEY", "")
PUSHOVER_TOKEN = config["NOTIFICATION"].get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = config["NOTIFICATION"].get("PUSHOVER_USER", "")

# Chrome setup
options = Options()
options.binary_location = "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
options.add_argument("user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.7499.146 Safari/537.36")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_experimental_option("useAutomationExtension", False)
service = Service("/Users/skainn/chromedriver-143/chromedriver-mac-x64/chromedriver")
driver = webdriver.Chrome(service=service, options=options)

# Time settings
minute = 60
hour = 60 * minute
STEP_TIME = 0.5
RETRY_TIME_L_BOUND = config["TIME"].getfloat("RETRY_TIME_L_BOUND")
RETRY_TIME_U_BOUND = config["TIME"].getfloat("RETRY_TIME_U_BOUND")
WORK_LIMIT_TIME = config["TIME"].getfloat("WORK_LIMIT_TIME")
WORK_COOLDOWN_TIME = config["TIME"].getfloat("WORK_COOLDOWN_TIME")
BAN_COOLDOWN_TIME = config["TIME"].getfloat("BAN_COOLDOWN_TIME")
REQUEST_COUNT = config["TIME"].getfloat("REQUEST_COUNT")

JS_SCRIPT = (
    "var req = new XMLHttpRequest();"
    "req.open('GET', '%s', false);"
    "req.setRequestHeader('Accept', 'application/json, text/javascript, */*; q=0.01');"
    "req.setRequestHeader('X-Requested-With', 'XMLHttpRequest');"
    "req.setRequestHeader('Cookie', '_yatri_session=%s');"
    "req.send(null);"
    "return req.responseText;"
)

# ---------- Utils ----------
def send_telegram(msg):
    if BOT_TOKEN and runtime.OWNER_TELEGRAM_ID:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"  # ✅ без пробелов
        try:
            requests.post(url, json={"chat_id": runtime.OWNER_TELEGRAM_ID, "text": msg}, timeout=10)
        except Exception as e:
            print(f"Ошибка отправки в TG: {e}")

def send_notification(title, msg):
    print(f"Sending notification: {title}")
    if SENDGRID_API_KEY:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        message = Mail(
            from_email=runtime.USERNAME,
            to_emails=runtime.USERNAME,
            subject=title,
            html_content=msg
        )
        try:
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            sg.send(message)
        except Exception as e:
            print(e)
    if PUSHOVER_TOKEN:
        url = "https://api.pushover.net/1/messages.json"  # ✅ без пробелов
        data = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "message": msg}
        requests.post(url, data)

def info_logger(file_path, log, send_to_telegram=False):
    timestamp = str(datetime.datetime.now())
    full_log = f"{timestamp}:\n{log}\n"
    print(full_log)
    with open(file_path, "a") as f:
        f.write(full_log)
    if send_to_telegram:
        send_telegram(log)

def auto_action(label, find_by, el_type, action, value="", sleep_time=0):
    print(f"\t{label}:", end="")
    item = None
    match find_by.lower():
        case "id": item = driver.find_element(By.ID, el_type)
        case "name": item = driver.find_element(By.NAME, el_type)
        case "class": item = driver.find_element(By.CLASS_NAME, el_type)
        case "xpath": item = driver.find_element(By.XPATH, el_type)
        case _: return
    match action.lower():
        case "send": item.send_keys(value)
        case "click": item.click()
        case _: return
    print(" Check!")
    if sleep_time:
        time.sleep(sleep_time)

# ---------- Visa Logic ----------
def start_process():
    driver.get(runtime.SIGN_IN_LINK)
    time.sleep(STEP_TIME)
    Wait(driver, 60).until(EC.presence_of_element_located((By.NAME, "commit")))
    auto_action("Click bounce", "xpath", '//a[@class="down-arrow bounce"]', "click", sleep_time=STEP_TIME)
    auto_action("Email", "id", "user_email", "send", runtime.USERNAME, STEP_TIME)
    auto_action("Password", "id", "user_password", "send", runtime.PASSWORD, STEP_TIME)
    auto_action("Privacy", "class", "icheckbox", "click", "", STEP_TIME)
    auto_action("Enter Panel", "name", "commit", "click", "", STEP_TIME)
    Wait(driver, 60).until(
        EC.presence_of_element_located((By.XPATH, f"//a[contains(text(), '{runtime.REGEX_CONTINUE}')]"))
    )
    info_logger(LOG_FILE_NAME, "[LOGIN] Successful!", True)

def get_date():
    try:
        session = driver.get_cookie("_yatri_session")["value"]
        script = JS_SCRIPT % (runtime.DATE_URL, session)
        content = driver.execute_script(script)
        return json.loads(content)
    except:
        return None

def get_time(date):
    time_url = runtime.TIME_URL % date
    session = driver.get_cookie("_yatri_session")["value"]
    script = JS_SCRIPT % (time_url, session)
    content = driver.execute_script(script)
    data = json.loads(content)
    times = data.get("available_times")
    return times[-1] if times else None

def get_available_date(dates):
    PED = datetime.datetime.strptime(runtime.PERIOD_END, "%Y-%m-%d")
    PSD = datetime.datetime.strptime(runtime.PERIOD_START, "%Y-%m-%d")
    for d in dates:
        date = d.get("date")
        if not date:
            continue
        try:
            new_date = datetime.datetime.strptime(date, "%Y-%m-%d")
            if PSD < new_date < PED:
                return date
        except ValueError:
            continue
    return None

# ---------- Telegram Bot ----------
# Состояния только для добавления
(
    WAITING_USERNAME,
    WAITING_PASSWORD,
    WAITING_SCHEDULE_ID,
    WAITING_PERIOD_START,
    WAITING_PERIOD_END,
    WAITING_EMBASSY
) = range(6)

# Состояния для редактирования
(
    SELECTING_FIELD,
    EDITING_FIELD
) = range(2)

user_sessions = {}

# Соответствие полей и меток
FIELD_LABELS = {
    "username": "📧 Email",
    "password": "🔒 Пароль",
    "schedule_id": "🔢 Schedule ID",
    "period_start": "📅 Дата начала",
    "period_end": "📆 Дата окончания",
    "embassy_code": "🌍 Посольство"
}

FIELD_KEYS = list(FIELD_LABELS.keys())

# --- Основные команды ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_menu(update, context)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["➕ Добавить аккаунт"],
        ["✏️ Редактировать аккаунт"],
        ["📋 Мои аккаунты"],
        ["✅ Выбрать аккаунт"],
        ["🗑️ Удалить аккаунт"],
        ["ℹ️ Подробная информация"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# --- Добавление (без изменений) ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {}
    await update.message.reply_text("📧 Введите email:")
    return WAITING_USERNAME

async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id]["USERNAME"] = update.message.text.strip()
    await update.message.reply_text("🔒 Введите пароль:")
    return WAITING_PASSWORD

async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id]["PASSWORD"] = update.message.text.strip()
    await update.message.reply_text("🔢 Введите SCHEDULE_ID:")
    return WAITING_SCHEDULE_ID

async def receive_schedule_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id]["SCHEDULE_ID"] = update.message.text.strip()
    await update.message.reply_text("📅 Введите Дату начала (например, 2026-01-01):")
    return WAITING_PERIOD_START

async def receive_period_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id]["PERIOD_START"] = update.message.text.strip()
    await update.message.reply_text("📆 Введите Дату окончания (например, 2026-12-31):")
    return WAITING_PERIOD_END

async def receive_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id]["PERIOD_END"] = update.message.text.strip()
    await update.message.reply_text("🌍 Введите код посольства (например, en-ru-ast):")
    return WAITING_EMBASSY

async def receive_embassy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    embassy = update.message.text.strip()
    if embassy not in Embassies:
        await update.message.reply_text(f"❌ Неизвестный код посольства: {embassy}. Доступные: {', '.join(Embassies.keys())}")
        return WAITING_EMBASSY

    user_sessions[user_id]["YOUR_EMBASSY"] = embassy
    account_id = save_account(user_sessions[user_id], user_id)
    del user_sessions[user_id]
    await update.message.reply_text(f"✅ Аккаунт сохранён! ID: {account_id}")
    return ConversationHandler.END

# --- Редактирование (НОВАЯ ЛОГИКА) ---
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("UsageId: /edit <ID>")
        return ConversationHandler.END

    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return ConversationHandler.END

    acc = get_account_by_id(account_id)
    if not acc or acc["OWNER_TELEGRAM_ID"] != user_id:
        await update.message.reply_text("❌ Аккаунт не найден или не принадлежит вам.")
        return ConversationHandler.END

    user_sessions[user_id] = {"editing_id": account_id}

    # Формируем сообщение с текущими значениями
    msg = f"✏️ <b>Редактирование аккаунта ID {account_id}</b>\n\nВыберите поле для изменения:\n"
    for key in FIELD_KEYS:
        current = acc[key.upper()] if key != "embassy_code" else acc["YOUR_EMBASSY"]
        msg += f"\n{FIELD_LABELS[key]}: <code>{current}</code>"

    keyboard = [[FIELD_LABELS[key]] for key in FIELD_KEYS] + [["🔙 Отмена"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=reply_markup)
    return SELECTING_FIELD

async def select_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text == "🔙 Отмена":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await update.message.reply_text("❌ Редактирование отменено.")
        return ConversationHandler.END

    selected_key = None
    for key, label in FIELD_LABELS.items():
        if text == label:
            selected_key = key
            break

    if not selected_key:
        await update.message.reply_text("❌ Неизвестное поле. Выберите из списка.")
        return SELECTING_FIELD

    user_sessions[user_id]["editing_field"] = selected_key
    acc = get_account_by_id(user_sessions[user_id]["editing_id"])
    current_val = acc[selected_key.upper()] if selected_key != "embassy_code" else acc["YOUR_EMBASSY"]
    await update.message.reply_text(
        f"✏️ Введите новое значение для <b>{FIELD_LABELS[selected_key]}</b>:\n"
        f"Текущее: <code>{current_val}</code>\n"
        "Или напишите <code>пропустить</code> или <code>skip</code>, чтобы оставить без изменений.",
        parse_mode="HTML"
    )
    return EDITING_FIELD

async def edit_field_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = user_sessions.get(user_id, {})
    account_id = session.get("editing_id")
    field_key = session.get("editing_field")

    if not account_id or not field_key:
        await update.message.reply_text("❌ Сессия повреждена. Начните заново.")
        return ConversationHandler.END

    text = update.message.text.strip()

    if text.lower() in ("пропустить", "skip"):
        await update.message.reply_text("✅ Поле оставлено без изменений.")
    else:
        success = update_account_field(account_id, field_key, text, user_id)
        if success:
            await update.message.reply_text(f"✅ Поле <b>{FIELD_LABELS[field_key]}</b> обновлено!", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Не удалось обновить поле.")

    if user_id in user_sessions:
        del user_sessions[user_id]
    return ConversationHandler.END

# --- Список и информация ---
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    accounts = get_accounts_for_user(user_id)
    if not accounts:
        await update.message.reply_text("❌ У вас нет сохранённых аккаунтов.")
        return
    msg = "📋 Ваши аккаунты:\n\n"
    for acc in accounts:
        msg += f"ID: {acc[0]} | {acc[1]} | {acc[2]} | {acc[3]} — {acc[4]}\n"
    await update.message.reply_text(msg)

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("UsageId: /info <ID>")
        return
    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return

    acc = get_account_by_id(account_id)
    if not acc:
        await update.message.reply_text("❌ Аккаунт не найден.")
        return
    if acc["OWNER_TELEGRAM_ID"] != user_id:
        await update.message.reply_text("❌ Вы не владелец этого аккаунта.")
        return

    msg = (
        "<b>ℹ️ Подробная информация об аккаунте</b>\n\n"
        f"<b>ID:</b> {acc['id']}\n"
        f"<b>Email:</b> {acc['USERNAME']}\n"
        f"<b>Посольство:</b> {acc['YOUR_EMBASSY']}\n"
        f"<b>Schedule ID:</b> {acc['SCHEDULE_ID']}\n"
        f"<b>Период поиска:</b>\n"
        f"  📅 От: {acc['PERIOD_START']}\n"
        f"  📅 До: {acc['PERIOD_END']}\n"
        f"<b>Добавлен:</b> {acc['created_at']}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("UsageId: /use <ID>")
        return
    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return

    acc = get_account_by_id(account_id)
    if not acc:
        await update.message.reply_text("❌ Аккаунт не найден.")
        return
    if acc["OWNER_TELEGRAM_ID"] != user_id:
        await update.message.reply_text("❌ Вы не владелец этого аккаунта.")
        return

    set_active_account(account_id)
    await update.message.reply_text(f"✅ Аккаунт ID {account_id} выбран. Перезапуск...")
    os.execv(sys.executable, ['python'] + sys.argv)

# --- Удаление ---
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("UsageId: /delete <ID>")
        return
    try:
        account_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return

    acc = get_account_by_id(account_id)
    if not acc:
        await update.message.reply_text("❌ Аккаунт не найден.")
        return
    if acc["OWNER_TELEGRAM_ID"] != user_id:
        await update.message.reply_text("❌ Вы не владелец этого аккаунта.")
        return

    success = delete_account_by_id(account_id, user_id)
    if success:
        try:
            with open(ACTIVE_ACCOUNT_FILE, "r") as f:
                active_id = int(f.read().strip())
            if active_id == account_id:
                os.remove(ACTIVE_ACCOUNT_FILE)
        except (FileNotFoundError, ValueError, OSError):
            pass
        await update.message.reply_text(f"✅ Аккаунт ID {account_id} удалён.")
    else:
        await update.message.reply_text("❌ Не удалось удалить аккаунт.")

# --- Обработка кнопок меню ---
async def handle_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await cmd_add(update, context)

async def handle_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите ID аккаунта для редактирования:\n<code>/edit ID</code>",
        parse_mode="HTML"
    )

async def handle_list_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await cmd_list(update, context)

async def handle_use_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Чтобы выбрать аккаунт, отправьте команду:\n<code>/use ID</code>",
        parse_mode="HTML"
    )

async def handle_info_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите ID аккаунта:\n<code>/info ID</code>",
        parse_mode="HTML"
    )

async def handle_delete_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите ID аккаунта для удаления:\n<code>/delete ID</code>",
        parse_mode="HTML"
    )

# --- Запуск бота ---
async def run_telegram_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчик добавления
    add_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            WAITING_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
            WAITING_SCHEDULE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule_id)],
            WAITING_PERIOD_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_period_start)],
            WAITING_PERIOD_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_period_end)],
            WAITING_EMBASSY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_embassy)],
        },
        fallbacks=[]
    )

    # Обработчик редактирования
    edit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("edit", cmd_edit)],
        states={
            SELECTING_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_field)],
            EDITING_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field_value)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(add_conv_handler)
    app.add_handler(edit_conv_handler)

    app.add_handler(MessageHandler(filters.Regex("^➕ Добавить аккаунт$"), handle_add_button))
    app.add_handler(MessageHandler(filters.Regex("^✏️ Редактировать аккаунт$"), handle_edit_button))
    app.add_handler(MessageHandler(filters.Regex("^📋 Мои аккаунты$"), handle_list_button))
    app.add_handler(MessageHandler(filters.Regex("^✅ Выбрать аккаунт$"), handle_use_info))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Подробная информация$"), handle_info_button))
    app.add_handler(MessageHandler(filters.Regex("^🗑️ Удалить аккаунт$"), handle_delete_button))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

def start_telegram_bot():
    asyncio.run(run_telegram_bot())

# ---------- Monitor Loop ----------
def monitor_loop():
    first_loop = True
    Req_count = 0
    t0 = time.time()
    previous_dates = set()

    while True:
        try:
            if first_loop:
                start_process()
                first_loop = False
                Req_count = 0
                t0 = time.time()

            Req_count += 1
            msg = f"\n{'-'*60}\nRequest #{Req_count} at {datetime.datetime.now()}\n"
            info_logger(LOG_FILE_NAME, msg)

            dates = get_date()
            while dates is None:
                info_logger(LOG_FILE_NAME, "Даты недоступны, повтор...")
                time.sleep(1)
                dates = get_date()

            if not dates:
                info_logger(LOG_FILE_NAME, "Список пуст, возможно бан", True)
                send_notification("BAN", "Возможен бан, перерыв...")
                driver.get(runtime.SIGN_OUT_LINK)
                time.sleep(BAN_COOLDOWN_TIME * hour)
                first_loop = True
                continue

            filtered_dates = []
            PED = datetime.datetime.strptime(runtime.PERIOD_END, "%Y-%m-%d")
            PSD = datetime.datetime.strptime(runtime.PERIOD_START, "%Y-%m-%d")
            for d in dates:
                date_str = d.get("date")
                if not date_str:
                    continue
                try:
                    date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
                    if PSD < date_obj < PED:
                        filtered_dates.append(date_str)
                except ValueError:
                    continue

            current_dates = set(filtered_dates)
            if current_dates:
                filtered_msg = "📅 Доступные даты в периоде:\n" + "\n".join(sorted(current_dates))
                info_logger(LOG_FILE_NAME, filtered_msg, True)
            else:
                info_logger(LOG_FILE_NAME, "❌ Нет дат в указанном периоде.", True)

            new_dates = current_dates - previous_dates
            if new_dates:
                new_msg = "🆕 Новые даты в периоде:\n" + "\n".join(sorted(new_dates))
                info_logger(LOG_FILE_NAME, new_msg, True)
            previous_dates = current_dates

            date_to_book = get_available_date(dates)
            if date_to_book:
                time_slot = get_time(date_to_book)
                if time_slot:
                    slot_msg = f"✅ Доступный слот: {date_to_book} {time_slot}"
                    info_logger(LOG_FILE_NAME, slot_msg, True)

            t1 = time.time()
            total_time = t1 - t0
            if total_time > WORK_LIMIT_TIME * hour or Req_count > REQUEST_COUNT:
                rest_msg = f"💤 Перерыв после {total_time/hour:.1f} ч / {Req_count} запросов"
                info_logger(LOG_FILE_NAME, rest_msg, True)
                send_notification("REST", rest_msg)
                driver.get(runtime.SIGN_OUT_LINK)
                time.sleep(WORK_COOLDOWN_TIME * hour)
                first_loop = True
                continue

            wait = random.uniform(RETRY_TIME_L_BOUND, RETRY_TIME_U_BOUND)
            info_logger(LOG_FILE_NAME, f"Следующий запрос через {wait:.1f} сек")
            time.sleep(wait)

        except Exception as e:
            logging.exception(e)
            err = f"❌ Ошибка: {e}"
            info_logger(LOG_FILE_NAME, err, True)
            time.sleep(60)

# ---------- Main ----------
if __name__ == "__main__":
    init_db()
    threading.Thread(target=start_telegram_bot, daemon=True).start()
    monitor_loop()