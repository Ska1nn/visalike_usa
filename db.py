import sqlite3
from datetime import datetime

DB_PATH = "accounts.db"

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

def get_accounts():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, embassy_code, period_start, period_end FROM accounts")
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
        }
    return None

def set_active_account(account_id):
    # Сохраняем ID активного аккаунта в отдельный файл или таблицу
    with open("active_account_id.txt", "w") as f:
        f.write(str(account_id))