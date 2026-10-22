"""Hosting bot ma'lumotlar bazasi (SQLite). Qo'shilgan botlar shu yerda saqlanadi."""
import os
import sqlite3
import time

DATA_DIR = os.getenv("DATA_DIR", "data")

_FIELDS = {"name", "username", "token", "entry", "autorun", "want_running"}

WELCOME_NEWS = (
    "🚀 Yangilanish!\n"
    "• 🐘 PHP botlar qo'llab-quvvatlanadi (.php fayl yoki composer.json bilan zip)\n"
    "• 🧠 AI bilan kod yozish: bot tavsifini yozasiz, DeepSeek tayyor .py/.php fayl yuboradi\n"
    "• 🛠 AI bilan xatoni tuzatish (panel -> loglar asosida)\n"
    "• 📦 Botni zip qilib yuklab olish, ishlash vaqti (uptime) ko'rsatiladi\n"
    "• 📰 Yangiliklar bo'limi"
)


def db_path():
    return os.path.join(DATA_DIR, "hosting.db")


def bot_dir(host_id):
    # Absolyut yo'l: bot jarayoni boshqa papkadan (cwd) ishga tushadi
    return os.path.abspath(os.path.join(DATA_DIR, "bots", str(host_id)))


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS hosts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                username TEXT,
                token TEXT NOT NULL,
                entry TEXT,
                autorun INTEGER NOT NULL DEFAULT 1,
                want_running INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS news(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created REAL NOT NULL
            )
            """
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS news_seen(user_id INTEGER PRIMARY KEY, last_id INTEGER NOT NULL)"
        )
        if c.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0:
            c.execute(
                "INSERT INTO news(author_id, text, created) VALUES(0,?,?)",
                (WELCOME_NEWS, time.time()),
            )


def add_host(owner_id, token, name, username):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO hosts(owner_id, name, username, token, created) VALUES(?,?,?,?,?)",
            (owner_id, name, username, token, time.time()),
        )
        return cur.lastrowid


def get_host(host_id):
    with _conn() as c:
        return c.execute("SELECT * FROM hosts WHERE id=?", (host_id,)).fetchone()


def user_hosts(owner_id):
    with _conn() as c:
        return c.execute("SELECT * FROM hosts WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()


def all_hosts():
    with _conn() as c:
        return c.execute("SELECT * FROM hosts ORDER BY id").fetchall()


def update_host(host_id, **fields):
    bad = set(fields) - _FIELDS
    if bad:
        raise ValueError(f"Noma'lum maydon: {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE hosts SET {sets} WHERE id=?", (*fields.values(), host_id))


def delete_host(host_id):
    with _conn() as c:
        c.execute("DELETE FROM hosts WHERE id=?", (host_id,))


# ----------------------------- yangiliklar -----------------------------
def add_news(author_id, text):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO news(author_id, text, created) VALUES(?,?,?)",
            (author_id, text, time.time()),
        )
        return cur.lastrowid


def list_news(limit=5):
    with _conn() as c:
        return c.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def delete_news(news_id):
    with _conn() as c:
        return c.execute("DELETE FROM news WHERE id=?", (news_id,)).rowcount


def unread_news(user_id):
    """Foydalanuvchi hali ko'rmagan yangiliklar soni."""
    with _conn() as c:
        row = c.execute("SELECT last_id FROM news_seen WHERE user_id=?", (user_id,)).fetchone()
        last = row[0] if row else 0
        return c.execute("SELECT COUNT(*) FROM news WHERE id>?", (last,)).fetchone()[0]


def mark_news_seen(user_id):
    with _conn() as c:
        top = c.execute("SELECT COALESCE(MAX(id),0) FROM news").fetchone()[0]
        c.execute(
            "INSERT INTO news_seen(user_id,last_id) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_id=excluded.last_id",
            (user_id, top),
        )
