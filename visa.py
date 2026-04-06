# visa.py
import asyncio
import configparser
import datetime
import glob
import json
import os
import pathlib
import random
import sqlite3
import sys
import threading
import time
import traceback

import requests
import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

# ---------- Selenium ----------
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait as Wait
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)

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
    cursor.execute(f"UPDATE accounts SET {col} = ? WHERE id = ? AND owner_telegram_id = ?",
                   (value, aid, owner_telegram_id))
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
        self.FACILITY_ID = str(Embassies[self.YOUR_EMBASSY][1]).strip()
        self.REGEX_CONTINUE = Embassies[self.YOUR_EMBASSY][2]
        self.rebuild_urls()

    def rebuild_urls(self):
        embassy = str(self.EMBASSY).strip()
        facility = str(self.FACILITY_ID).strip()
        base = f"https://ais.usvisa-info.com/{embassy}/niv"
        self.SIGN_IN_LINK = f"{base}/users/sign_in"
        self.APPOINTMENT_URL = f"{base}/schedule/{self.SCHEDULE_ID}/appointment"
        self.DATE_URL = f"{base}/schedule/{self.SCHEDULE_ID}/appointment/days/{facility}.json?appointments[expedite]=false"
        self.TIME_URL = f"{base}/schedule/{self.SCHEDULE_ID}/appointment/times/%s.json?date=%s&appointments[expedite]=false"
        self.SIGN_OUT_LINK = f"{base}/users/sign_out"


runtime = RuntimeConfig()
LOG_FILE_NAME = "logs/log_" + str(datetime.date.today()) + ".txt"

config = configparser.ConfigParser()
config.read("config.ini")
BOT_TOKEN = config["TELEGRAM"].get("BOT_TOKEN")
SENDGRID_API_KEY = config["NOTIFICATION"].get("SENDGRID_API_KEY", "")
PUSHOVER_TOKEN = config["NOTIFICATION"].get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = config["NOTIFICATION"].get("PUSHOVER_USER", "")

minute = 60
hour = 60 * minute
STEP_TIME = 0.5
RETRY_TIME_L_BOUND = config["TIME"].getfloat("RETRY_TIME_L_BOUND")
RETRY_TIME_U_BOUND = config["TIME"].getfloat("RETRY_TIME_U_BOUND")
WORK_LIMIT_TIME = config["TIME"].getfloat("WORK_LIMIT_TIME")
WORK_COOLDOWN_TIME = config["TIME"].getfloat("WORK_COOLDOWN_TIME")
BAN_COOLDOWN_TIME = config["TIME"].getfloat("BAN_COOLDOWN_TIME")
REQUEST_COUNT = config["TIME"].getfloat("REQUEST_COUNT")

JS_SCRIPT = """
    return fetch('%s', {
        method: 'GET',
        headers: {
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest'
        },
        credentials: 'include'
    }).then(r => r.text());
"""


def build_cookie_string(driver):
    cookies = driver.get_cookies()
    return "; ".join([f"{c['name']}={c['value']}" for c in cookies])


# ---------- Utils ----------
def send_telegram(msg):
    if BOT_TOKEN and runtime.OWNER_TELEGRAM_ID:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        try:
            requests.post(
                url,
                json={
                    "chat_id": runtime.OWNER_TELEGRAM_ID,
                    "text": msg,
                    "parse_mode": "HTML"
                },
                timeout=10
            )
        except Exception as e:
            with open(LOG_FILE_NAME, "a") as f:
                f.write(f"{datetime.datetime.now()}: Ошибка отправки в TG: {e}\n")


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
        url = "https://api.pushover.net/1/messages.json"
        data = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "message": msg}
        requests.post(url, data)


def info_logger(log, send_to_telegram=False):
    timestamp = str(datetime.datetime.now())
    full_log = f"{timestamp}:\n{log}\n"
    print(full_log)

    # make dir
    log_dir = os.path.dirname(LOG_FILE_NAME)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    #with open(LOG_FILE_NAME, "a") as f:
    #   f.write(full_log)
    if send_to_telegram:
        send_telegram(log)


def clean_old_files(path_pattern, max_count):
    files = sorted(glob.glob(path_pattern), key=os.path.getmtime)
    for i in range(len(files) - max_count):
        os.remove(files[i])

def prepare_log_folders_and_clean():
    pathlib.Path("logs/screenshots").mkdir(parents=True, exist_ok=True)
    clean_old_files("logs/*.txt", 5)
    clean_old_files("logs/screenshots/*.*", 5)


def auto_action(driver, label, find_by, el_type, action, value="", sleep_time=0.0):
    print(f"\t{label}:", end="")
    try:
        match find_by.lower():
            case "id":
                item = driver.find_element(By.ID, el_type)
            case "name":
                item = driver.find_element(By.NAME, el_type)
            case "class":
                item = driver.find_element(By.CLASS_NAME, el_type)
            case "xpath":
                item = driver.find_element(By.XPATH, el_type)
            case _:
                print(" Fail (неверный find_by)")
                return
        match action.lower():
            case "send":
                item.send_keys(value)
            case "click":
                item.click()
            case _:
                print(" Fail (неверное действие)")
                return
        print(" Check!")
        if sleep_time:
            time.sleep(sleep_time)
    except Exception as e:
        print(f" Fail (ошибка: {str(e)[:50]})")


def start_process(driver):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"Попытка входа #{attempt + 1}")
            driver.get(runtime.SIGN_IN_LINK)

            # Увеличьте таймаут и добавьте проверку альтернативных элементов
            try:
                Wait(driver, 30).until(EC.presence_of_element_located((By.NAME, "commit")))
            except:
                # Проверьте, если страница уже загрузилась с другими элементами
                if "sign_in" in driver.current_url:
                    print("Элемент 'commit' не найден, проверяем другие элементы...")

                # Проверка по ID
                try:
                    Wait(driver, 10).until(EC.presence_of_element_located((By.ID, "new_user")))
                    print("Найдена форма по ID 'new_user'")
                except:
                    pass

            # Добавьте задержку для полной загрузки
            time.sleep(3)

            # Попробуйте найти элементы по разным селекторам
            try:
                auto_action(driver, "Click bounce", "xpath", '//a[@class="down-arrow bounce"]', "click",
                            sleep_time=STEP_TIME)
            except:
                print("Элемент bounce не найден, продолжаем...")

            auto_action(driver, "Email", "id", "user_email", "send", runtime.USERNAME, STEP_TIME)
            auto_action(driver, "Password", "id", "user_password", "send", runtime.PASSWORD, STEP_TIME)

            # Попробуйте разные способы найти checkbox
            try:
                auto_action(driver, "Privacy", "class", "icheckbox", "click", "", STEP_TIME)
            except:
                try:
                    auto_action(driver, "Privacy", "xpath", "//input[@type='checkbox']", "click", "", STEP_TIME)
                except:
                    print("Чекбокс не найден")

            auto_action(driver, "Enter Panel", "name", "commit", "click", "", STEP_TIME)

            # Увеличьте таймаут проверки успешного входа
            try:
                Wait(driver, 30).until(
                    EC.presence_of_element_located(
                        (By.XPATH, f"//a[contains(text(), '{runtime.REGEX_CONTINUE}')]")
                    )
                )
                info_logger("[LOGIN] Successful!", True)
                return  # Успешный выход из функции

            except TimeoutException:
                # Проверка, не появилась ли ошибка
                page_source = driver.page_source.lower()
                if "invalid" in page_source or "error" in page_source or "incorrect" in page_source:
                    msg = "❌ Ошибка входа: неверные данные"
                    info_logger(msg, True)
                    raise RuntimeError("LOGIN_FAILED")

                # Сохраним скриншот для отладки
                try:
                    driver.save_screenshot(f"logs/screenshots/debug_login_{attempt}.png")
                except:
                    pass

                print(f"Попытка #{attempt + 1} неудачна, повтор через 5 секунд...")
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                else:
                    # Последняя попытка также не удалась
                    msg = (
                        "❌ Ошибка входа в аккаунт после нескольких попыток.\n\n"
                        "Возможные причины:\n"
                        "• неверный логин или пароль\n"
                        "• аккаунт заблокирован\n"
                        "• требуется капча\n"
                        "• проблемы с сетью\n\n"
                        "Мониторинг остановлен."
                    )
                    info_logger(msg, True)
                    raise RuntimeError("LOGIN_FAILED")

        except Exception as e:
            print(f"Исключение в start_process: {e}")
            if attempt == max_retries - 1:
                msg = (
                    f"❌ Критическая ошибка входа: {str(e)[:100]}\n\n"
                    "Мониторинг остановлен."
                )
                info_logger(msg, True)
                raise RuntimeError("LOGIN_FAILED")
            time.sleep(5)


def select_consulate(driver):
    facility = runtime.FACILITY_ID
    info_logger(f"DEBUG: Выбор консульства с facility_id = {facility}")

    try:
        # Ждём, пока элемент станет доступным
        Wait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "appointments_consulate_appointment_facility_id"))
        )

        # Выполняем выбор консульства
        driver.execute_script("""
            const select = document.getElementById('appointments_consulate_appointment_facility_id');
            if (!select) return false;
            
            select.value = arguments[0];
            
            // Триггерим все необходимые события
            select.dispatchEvent(new Event('change', { bubbles: true }));
            select.dispatchEvent(new Event('input', { bubbles: true }));
            
            // Также можно вызвать onchange, если он есть
            if (typeof select.onchange === 'function') {
                select.onchange();
            }
            
            return true;
        """, facility)

        # Ждём, пока страница обновится (либо URL изменится, либо появится индикатор загрузки)
        info_logger("⏳ Ожидание обновления страницы после выбора консульства...")
        time.sleep(random.uniform(3, 5))

        # Проверяем, что выбор сохранился
        for attempt in range(5):
            try:
                current_value = driver.execute_script("""
                    const select = document.getElementById('appointments_consulate_appointment_facility_id');
                    return select ? select.value : '';
                """)

                if current_value == facility:
                    info_logger(f"✅ Консульство выбрано и сохранено (facility_id = {facility})")

                    # Даём дополнительное время на загрузку данных календаря
                    info_logger("⏳ Ожидание загрузки данных календаря...")
                    time.sleep(random.uniform(2, 4))

                    return True
                else:
                    info_logger(f"⚠️ Значение не сохранилось. Попытка {attempt + 1}/5")
                    time.sleep(1)
            except Exception as e:
                info_logger(f"⚠️ Ошибка проверки: {e}")
                time.sleep(1)

        info_logger("❌ Не удалось выбрать консульство после нескольких попыток")
        return False

    except TimeoutException:
        info_logger("❌ Таймаут при ожидании элемента select")
        return False
    except Exception as e:
        info_logger(f"❌ Ошибка в select_consulate: {e}")
        return False


def get_date(driver):
    try:
        info_logger(f"DEBUG: DATE_URL = {runtime.DATE_URL}")
        info_logger(f"DEBUG: APPOINTMENT_URL = {runtime.APPOINTMENT_URL}")

        # НЕ ПЕРЕЗАГРУЖАЕМ СТРАНИЦУ! Мы уже на нужной странице после select_consulate()
        # driver.get(runtime.APPOINTMENT_URL)  # УБЕРИТЕ ЭТУ СТРОКУ!

        # Вместо этого просто ждём немного
        time.sleep(random.uniform(1.5, 3))

        # Упростите вызов без дополнительных аргументов
        url1 = runtime.DATE_URL.strip()

        # Добавим отладочную информацию
        info_logger(f"📡 Отправка запроса по URL: {url1}")

        content1 = driver.execute_script("""
            return fetch(arguments[0], {
                method: 'GET',
                headers: {
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                credentials: 'include'
            }).then(r => r.text());
        """, url1)

        url2 = url1.replace("expedite=false", "expedite=true")
        content2 = driver.execute_script("""
            return fetch(arguments[0], {
                method: 'GET',
                headers: {
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                credentials: 'include'
            }).then(r => r.text());
        """, url2)

        # Логируем размеры ответов для отладки
        if content1:
            info_logger(f"📄 Ответ обычного запроса: {len(content1)} символов")
        if content2:
            info_logger(f"📄 Ответ expedite запроса: {len(content2)} символов")

        for label, content in [("обычный", content1), ("expedite", content2)]:
            if not content or content.strip() == "":
                info_logger(f"⚠️ {label} запрос вернул пустой ответ")
                continue
            if content.lstrip().startswith("<"):
                info_logger(f"⚠️ {label} запрос вернул HTML вместо JSON (возможно, сессия истекла)")
                continue
            low = content.lower()
            if "sign in" in low or "session" in low or "csrf" in low:
                info_logger(f"🔑 {label} запрос: сессия истекла")
                return "SESSION_EXPIRED"
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    if data:
                        info_logger(f"✅ Даты получены через {label} запрос! Найдено {len(data)} дат")
                        # Логируем первые несколько дат для отладки
                        for i, date_info in enumerate(data[:5]):
                            date_str = date_info.get('date', 'N/A')
                            business_day = date_info.get('business_day', 'N/A')
                            info_logger(f"  📅 {i + 1}: {date_str} (бизнес дней: {business_day})")
                        return data
                    else:
                        info_logger(f"⚠️ {label} запрос вернул пустой список дат")
                else:
                    info_logger(f"⚠️ {label} запрос вернул не список: {type(data)}")
                    info_logger(f"📄 Содержимое: {content[:200]}")
            except json.JSONDecodeError as e:
                info_logger(f"❌ Ошибка парсинга JSON в {label} запросе: {e}")
                info_logger(f"📄 Содержимое ответа (первые 500 символов): {content[:500]}")
                continue
            except Exception as e:
                info_logger(f"❌ Ошибка обработки {label} запроса: {e}")
                continue

        info_logger("📭 Все запросы вернули пустые или некорректные данные")
        return []

    except Exception as e:
        info_logger(f"❌ get_date error: {e}")
        info_logger(f"❌ Traceback: {traceback.format_exc()}")
        return None


def get_time(driver, date):
    try:
        consulate = select_consulate(driver)
        if not consulate:
            info_logger("❌ Не удалось выбрать консульство в get_time")
            return None

        time.sleep(random.uniform(2, 4))

        time_url = runtime.TIME_URL % (runtime.FACILITY_ID, date)
        info_logger(f"📡 Запрос времени по URL: {time_url}")

        content = driver.execute_script("""
            return fetch(arguments[0], {
                method: 'GET',
                headers: {
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                credentials: 'include'
            }).then(r => r.text());
        """, time_url)

        if not content or content.lstrip().startswith("<"):
            info_logger("⚠️ get_time: пустой ответ или HTML")
            return None

        data = json.loads(content)
        times = data.get("available_times")
        if times:
            info_logger(f"✅ Доступные времена: {times}")
            return times

        info_logger("⚠️ get_time: нет доступных времен")
        return None

    except Exception as e:
        info_logger(f"❌ get_time error: {e}")
        return None


def get_available_date(dates):
    if not dates:
        return None

    try:
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
    except Exception as e:
        info_logger(f"❌ get_available_date error: {e}")

    return None


# ---------- Telegram Bot Handlers ----------
(
    WAITING_USERNAME,
    WAITING_PASSWORD,
    WAITING_SCHEDULE_ID,
    WAITING_PERIOD_START,
    WAITING_PERIOD_END,
    WAITING_EMBASSY
) = range(6)

(
    SELECTING_FIELD,
    EDITING_FIELD
) = range(2)

user_sessions = {}

FIELD_LABELS = {
    "username": "📧 Email",
    "password": "🔒 Пароль",
    "schedule_id": "🔢 Schedule ID",
    "period_start": "📅 Дата начала",
    "period_end": "📆 Дата окончания",
    "embassy_code": "🌍 Посольство"
}

FIELD_KEYS = list(FIELD_LABELS.keys())


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
    await update.message.reply_text("🌍 Введите код посольства (например, Astana, Almata):")
    return WAITING_EMBASSY


async def receive_embassy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    embassy = update.message.text.strip()
    if embassy not in Embassies:
        await update.message.reply_text(
            f"❌ Неизвестный код посольства: {embassy}. Доступные: {', '.join(Embassies.keys())}")
        return WAITING_EMBASSY

    user_sessions[user_id]["YOUR_EMBASSY"] = embassy
    account_id = save_account(user_sessions[user_id], user_id)
    del user_sessions[user_id]
    await update.message.reply_text(f"✅ Аккаунт сохранён! ID: {account_id}")
    return ConversationHandler.END


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
        await cmd_menu(update, context)
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
        await cmd_menu(update, context)
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


async def run_telegram_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    add_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("add", cmd_add),
            MessageHandler(filters.Regex("^➕ Добавить аккаунт$"), cmd_add),
        ],
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


def by_pass_limit_confirmation(driver):
    try:
        # Находим визуальный элемент чекбокса (div с классами icheckbox)
        # Селектор ищет div, который находится непосредственно перед input с нужным ID
        visual_checkbox = driver.find_element(By.XPATH,
                                              "//input[@id='confirmed_limit_message']/parent::div[contains(@class, 'icheckbox')]")

        print("✅ Найден визуальный элемент чекбокса")

        # Проверяем, не отмечен ли он уже (опционально, можно убрать)
        # Обычно у отмеченного icheckbox добавляется класс 'checked'
        if 'checked' not in visual_checkbox.get_attribute('class'):
            visual_checkbox.click()
            print("✅ Чекбокс отмечен (клик по визуальному div)")
        else:
            print("ℹ️ Чекбокс уже был отмечен")
        time.sleep(random.uniform(3, 5))
        # Далее ищем и нажимаем кнопку submit
        submit_elements = driver.find_elements(By.CSS_SELECTOR,
                                               "input[type='submit'], button[type='submit']")
        if submit_elements:
            submit_elements[0].click()
            time.sleep(random.uniform(3, 5))
            print(f"✅ Нажата submit кнопка (тег: {submit_elements[0].tag_name})")
        else:
            print("❌ Кнопка submit не найдена")

    except Exception as e:
        print(f"❌ Ошибка при работе с чекбоксом: {e}")


def monitor_loop():
    driver = None
    first_loop = True
    req_count = 0
    previous_dates = set()

    while True:
        try:
            if first_loop:
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass

                options = uc.ChromeOptions()
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--disable-gpu")
                options.add_argument("--window-size=1920,1080")
                options.add_argument("--disable-blink-features=AutomationControlled")

                print("Инициализация ChromeDriver")

                try:
                    driver = uc.Chrome(options=options)
                    print("✅ ChromeDriver успешно инициализирован")

                except Exception as e:
                    print(f"❌ Ошибка: {e}")
                    print("Попробуем другой способ...")

                    try:
                        # Вариант 2: С указанием пути для Apple Silicon
                        driver = uc.Chrome(
                            options=options,
                            driver_executable_path="/opt/homebrew/bin/chromedriver"
                        )
                        print("✅ ChromeDriver успешно инициализирован (Apple Silicon путь)")

                    except Exception as e2:
                        print(f"❌ Ошибка: {e2}")
                        print("Пожалуйста, разрешите ChromeDriver в Настройках безопасности Mac")
                        time.sleep(10)
                        continue

                # Увеличьте таймаут для start_process
                try:
                    print("Выполнение входа в систему...")
                    start_process(driver)
                    first_loop = False
                    req_count = 0
                    info_logger("[LOGIN] Successful!", True)

                    # Имитация просмотра календаря...
                    info_logger("👀 Имитация просмотра календаря...")
                    driver.get(runtime.APPOINTMENT_URL)
                    time.sleep(random.uniform(5, 10))

                except Exception as e:
                    print(f"❌ Ошибка в start_process: {e}")
                    first_loop = True
                    time.sleep(30)
                    continue

            req_count += 1
            msg = f"\n{'-' * 60}\nRequest #{req_count} at {datetime.datetime.now()}\n"
            info_logger(msg)

            # 1. Переходим на страницу записи
            info_logger("🔄 Переход на страницу записи...")
            driver.get(runtime.APPOINTMENT_URL)
            time.sleep(random.uniform(4, 7))

            by_pass_limit_confirmation(driver)

            # 2. Выбираем консульство
            info_logger("🏛️ Выбор консульства...")
            consulate = select_consulate(driver)
            if not consulate:
                info_logger("❌ Консульство не выбрано, перезапуск...", True)
                first_loop = True
                time.sleep(30)
                continue

            # 3. Проверяем даты (НЕ перезагружаем страницу!)
            info_logger("📅 Проверка доступных дат...")
            dates = get_date(driver)
            retry_count = 0
            while dates in (None, "BUSY", "SESSION_EXPIRED"):
                if dates == "SESSION_EXPIRED":
                    info_logger("🔄 Сессия истекла — перезаход...", True)
                    first_loop = True
                    break
                elif dates == "BUSY":
                    info_logger("⚠️ Сайт занят, пауза 5 сек...")
                    time.sleep(5)
                else:
                    info_logger("Даты недоступны, повтор через 1 сек...")
                    time.sleep(1)
                dates = get_date(driver)
                retry_count += 1
                if retry_count > 30:
                    info_logger("❌ Сайт долго недоступен — перезаход...", True)
                    first_loop = True
                    break

            if first_loop:
                continue

            if not dates:
                info_logger("Список дат пуст, ждем следующий цикл...", True)
                wait = random.uniform(RETRY_TIME_L_BOUND, RETRY_TIME_U_BOUND)
                info_logger(f"Следующий запрос через {wait:.1f} сек")
                time.sleep(wait)
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
                info_logger(filtered_msg, True)
            else:
                info_logger("❌ Нет дат в указанном периоде.", True)

            new_dates = current_dates - previous_dates
            if new_dates:
                new_msg = "🆕 Новые даты в периоде:\n" + "\n".join(sorted(new_dates))
                info_logger(new_msg, True)
            previous_dates = current_dates

            if dates:
                all_slots_message = "📅 Все доступные слоты:\n"

                for date in dates:
                    available_times = get_time(driver, date)
                    time.sleep(random.uniform(2, 4))
                    if available_times:
                        all_slots_message += f"\n📌 <b>{date.get('date')}</b>:\n"
                        for i, time_slot in enumerate(available_times, 1):
                            all_slots_message += f"  {i}. {time_slot}\n"

                info_logger(all_slots_message, True)

            if req_count >= 70:
                info_logger("🔄 Обновление сессии: перезаход в аккаунт...")
                first_loop = True
                continue

            wait = random.uniform(RETRY_TIME_L_BOUND, RETRY_TIME_U_BOUND)
            info_logger(f"Следующий запрос через {wait:.1f} сек")
            time.sleep(wait)

        except RuntimeError as e:
            if str(e) == "LOGIN_FAILED":
                info_logger("🛑 Ошибка авторизации. Повтор через 5 минут...", True)
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass
                driver = None
                first_loop = True
                time.sleep(300)
            else:
                raise

        except Exception:
            import traceback
            error_msg = f"❌ КРИТИЧЕСКАЯ ОШИБКА:\n{traceback.format_exc()}"
            info_logger(error_msg, True)
            if driver:
                try:
                    driver.quit()
                except:
                    pass
            driver = None
            first_loop = True
            time.sleep(60)


if __name__ == "__main__":
    print("=" * 60)
    print("Запуск Visa Bot Monitor")
    print(f"Время запуска: {datetime.datetime.now()}")
    print("=" * 60)

    init_db()

    prepare_log_folders_and_clean()

    telegram_thread = threading.Thread(target=start_telegram_bot, daemon=True)
    telegram_thread.start()

    time.sleep(2)

    print("Telegram бот запущен в фоновом режиме")
    print("Запуск основного цикла мониторинга...")
    print("=" * 60)

    monitor_loop()
