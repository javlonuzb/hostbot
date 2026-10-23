"""Hosting bot ma'lumotlar bazasi (SQLite).

v2: botlar, maxfiy kalitlar, foydalanuvchi limitlari, promo kodlar,
zaxira nusxalar, yangiliklar va umumiy sozlamalar (kv) shu yerda saqlanadi.
"""
import os
import sqlite3
import time

DATA_DIR = os.getenv("DATA_DIR", "data")

_FIELDS = {
    "name", "username", "token", "entry", "autorun", "want_running",
    "git_repo", "git_branch", "last_error", "crashes", "pinned",
}
_USER_FIELDS = {"promo_until", "promo_code", "max_bots", "ai_daily", "note"}

WELCOME_NEWS = (
    "🚀 <b>Host Bot v2</b>\n"
    "• 💾 Har kuni avtomatik zaxira — baza va fayllar kanalga yuboriladi, bir tugma bilan tiklash\n"
    "• 🔐 Har bot uchun alohida maxfiy kalitlar (panelda, kodda emas)\n"
    "• 🔥 Bot yiqilsa: avtomatik qayta ishga tushadi + oxirgi xato va «🛠 AI bilan tuzatish» tugmasi\n"
    "• 📦 GitHub'dan o'rnatish va bitta tugma bilan yangilash\n"
    "• 📊 Har botning RAM/CPU sarfi, kunlik limitlar\n"
    "• 🧠 AI: ko'p fayllik loyiha, «kodni tushuntir», model tanlash\n"
    "• 👑 Admin paneli, 📢 zaxira kanali va 🎁 promo rejim\n"
    "Buyruqlar yo'q — hammasi inline tugmalarda."
)


def db_path():
    return os.path.join(DATA_DIR, "hosting.db")


def bot_dir(host_id):
    # Absolyut yo'l: bot jarayoni boshqa papkadan (cwd) ishga tushadi
    return os.path.abspath(os.path.join(DATA_DIR, "bots", str(host_id)))


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(db_path(), timeout=20)
    c.row_factory = sqlite3.Row
    return c


def _columns(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def _migrate(c):
    """Eski (v1) bazani v2 sxemasiga ko'chiradi: ustunlar qo'shadi."""
    add = {
        "hosts": {
            "git_repo": "TEXT", "git_branch": "TEXT", "last_error": "TEXT",
            "crashes": "INTEGER NOT NULL DEFAULT 0", "pinned": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, cols in add.items():
        have = _columns(c, table)
        for name, decl in cols.items():
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


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
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                promo_until REAL NOT NULL DEFAULT 0,
                promo_code TEXT,
                max_bots INTEGER NOT NULL DEFAULT 0,
                ai_daily INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_usage(
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, day)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS secrets(
                host_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated REAL NOT NULL,
                PRIMARY KEY(host_id, key)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS promo(
                code TEXT PRIMARY KEY,
                days INTEGER NOT NULL DEFAULT 0,
                max_uses INTEGER NOT NULL DEFAULT 0,
                uses INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS backups(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT 'auto',
                info TEXT,
                created REAL NOT NULL
            )
            """
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, added_by INTEGER, created REAL)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS promo_redemptions(user_id INTEGER, code TEXT, created REAL, PRIMARY KEY(user_id,code))")
        _migrate(c)
        if c.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0:
            c.execute(
                "INSERT INTO news(author_id, text, created) VALUES(0,?,?)",
                (WELCOME_NEWS, time.time()),
            )
        # Standart promo kod: hamma limitni ochadi
        c.execute(
            "INSERT OR IGNORE INTO promo(code, days, max_uses, uses, active, created_by, created) "
            "VALUES('infinix-host-no-limit', 0, 0, 0, 1, 0, ?)",
            (time.time(),),
        )


# ----------------------------- botlar -----------------------------
def add_host(owner_id, token, name, username, git_repo=None, git_branch=None):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO hosts(owner_id, name, username, token, created, git_repo, git_branch) "
            "VALUES(?,?,?,?,?,?,?)",
            (owner_id, name, username, token, time.time(), git_repo, git_branch),
        )
        return cur.lastrowid


def get_host(host_id):
    with _conn() as c:
        return c.execute("SELECT * FROM hosts WHERE id=?", (host_id,)).fetchone()


def user_hosts(owner_id):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM hosts WHERE owner_id=? ORDER BY id", (owner_id,)
        ).fetchall()


def all_hosts():
    with _conn() as c:
        return c.execute("SELECT * FROM hosts ORDER BY id").fetchall()


def host_by_token(token):
    with _conn() as c:
        return c.execute("SELECT * FROM hosts WHERE token=?", (token,)).fetchone()


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
        c.execute("DELETE FROM secrets WHERE host_id=?", (host_id,))


def count_hosts(owner_id=None):
    with _conn() as c:
        if owner_id is None:
            return c.execute("SELECT COUNT(*) FROM hosts").fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM hosts WHERE owner_id=?", (owner_id,)).fetchone()[0]


# ----------------------------- maxfiy kalitlar -----------------------------
def set_secret(host_id, key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO secrets(host_id, key, value, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(host_id,key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (host_id, key, value, time.time()),
        )


def del_secret(host_id, key):
    with _conn() as c:
        return c.execute("DELETE FROM secrets WHERE host_id=? AND key=?", (host_id, key)).rowcount


def list_secrets(host_id):
    with _conn() as c:
        return c.execute("SELECT * FROM secrets WHERE host_id=? ORDER BY key", (host_id,)).fetchall()


def clear_secrets(host_id):
    with _conn() as c:
        c.execute("DELETE FROM secrets WHERE host_id=?", (host_id,))


# ----------------------------- foydalanuvchilar -----------------------------
def ensure_user(user_id):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id, created) VALUES(?,?)", (user_id, time.time())
        )


def get_user(user_id):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def set_user(user_id, **fields):
    bad = set(fields) - _USER_FIELDS
    if bad:
        raise ValueError(f"Noma'lum maydon: {bad}")
    ensure_user(user_id)
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*fields.values(), user_id))


def all_users():
    with _conn() as c:
        return c.execute("SELECT * FROM users ORDER BY created DESC").fetchall()


def known_user_ids():
    """Foydalanuvchi ID'lari: users jadvali + bot egalari."""
    ids = set()
    with _conn() as c:
        for r in c.execute("SELECT user_id FROM users"):
            ids.add(r[0])
        for r in c.execute("SELECT DISTINCT owner_id FROM hosts"):
            ids.add(r[0])
    return ids


# ----------------------------- AI sarfi -----------------------------
def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def ai_used(user_id, day=None):
    day = day or today()
    with _conn() as c:
        row = c.execute(
            "SELECT count FROM ai_usage WHERE user_id=? AND day=?", (user_id, day)
        ).fetchone()
        return row[0] if row else 0


def ai_inc(user_id, day=None):
    day = day or today()
    with _conn() as c:
        c.execute(
            "INSERT INTO ai_usage(user_id, day, count) VALUES(?,?,1) "
            "ON CONFLICT(user_id,day) DO UPDATE SET count=count+1",
            (user_id, day),
        )
        return c.execute(
            "SELECT count FROM ai_usage WHERE user_id=? AND day=?", (user_id, day)
        ).fetchone()[0]


# ----------------------------- promo -----------------------------
def get_promo(code):
    with _conn() as c:
        return c.execute("SELECT * FROM promo WHERE code=?", (code,)).fetchone()


def list_promos():
    with _conn() as c:
        return c.execute("SELECT * FROM promo ORDER BY created DESC").fetchall()


def add_promo(code, days=0, max_uses=0, created_by=0):
    with _conn() as c:
        c.execute(
            "INSERT INTO promo(code, days, max_uses, uses, active, created_by, created) "
            "VALUES(?,?,?,0,1,?,?) ON CONFLICT(code) DO UPDATE SET active=1",
            (code, days, max_uses, created_by, time.time()),
        )


def use_promo(code):
    with _conn() as c:
        c.execute("UPDATE promo SET uses=uses+1 WHERE code=?", (code,))


def set_promo_active(code, active):
    with _conn() as c:
        c.execute("UPDATE promo SET active=? WHERE code=?", (1 if active else 0, code))


def delete_promo(code):
    with _conn() as c:
        return c.execute("DELETE FROM promo WHERE code=?", (code,)).rowcount


# ----------------------------- adminlar -----------------------------
def add_admin(user_id, added_by=0):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO admins(user_id, added_by, created) VALUES(?,?,?)",
            (user_id, added_by, time.time()),
        )


def del_admin(user_id):
    with _conn() as c:
        return c.execute("DELETE FROM admins WHERE user_id=?", (user_id,)).rowcount


def list_admins():
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM admins ORDER BY created")]


# ----------------------------- zaxiralar -----------------------------
def add_backup(path, size, kind="auto", info=""):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO backups(path, size, kind, info, created) VALUES(?,?,?,?,?)",
            (path, size, kind, info, time.time()),
        )
        return cur.lastrowid


def list_backups(limit=15):
    with _conn() as c:
        return c.execute("SELECT * FROM backups ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_backup(backup_id):
    with _conn() as c:
        return c.execute("SELECT * FROM backups WHERE id=?", (backup_id,)).fetchone()


def delete_backup(backup_id):
    with _conn() as c:
        c.execute("DELETE FROM backups WHERE id=?", (backup_id,))


def prune_backups(keep=30):
    rows = list_backups(500)
    for r in rows[keep:]:
        delete_backup(r["id"])


# ----------------------------- kv sozlamalar -----------------------------
def kv_get(key, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def kv_set(key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)),
        )


def backup_channel():
    v = kv_get("backup_channel", "")
    try:
        return int(v) if v else None
    except ValueError:
        return None


def set_backup_channel(chat_id, title=""):
    kv_set("backup_channel", chat_id)
    if title:
        kv_set("backup_channel_title", title)


def backup_channel_title():
    return kv_get("backup_channel_title", "")


def backup_hour():
    try:
        return max(0, min(23, int(kv_get("backup_hour", os.getenv("BACKUP_HOUR", "3")))))
    except ValueError:
        return 3


def last_backup_day():
    return kv_get("backup_last_day", "")


def set_last_backup_day(day):
    kv_set("backup_last_day", day)


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


def redeem_promo(user_id, code):
    with _conn() as c:
        c.execute('BEGIN IMMEDIATE')
        p = c.execute('SELECT * FROM promo WHERE code=?', (code,)).fetchone()
        if not p or not p['active']:
            return False, 'Kod topilmadi yoki o‘chirilgan.'
        if c.execute('SELECT 1 FROM promo_redemptions WHERE user_id=? AND code=?', (user_id, code)).fetchone():
            return False, 'Bu kodni avval ishlatgansiz.'
        if p['max_uses'] and p['uses'] >= p['max_uses']:
            return False, 'Kodning ishlatish limiti tugagan.'
        now = time.time()
        row = c.execute('SELECT promo_until FROM users WHERE user_id=?', (user_id,)).fetchone()
        old = row[0] if row else 0
        until = -1 if p['days'] == 0 or old < 0 else max(old, now) + p['days'] * 86400
        c.execute('INSERT OR IGNORE INTO users(user_id, created) VALUES(?,?)', (user_id, now))
        c.execute('UPDATE users SET promo_until=?,promo_code=? WHERE user_id=?', (until, code, user_id))
        c.execute('INSERT INTO promo_redemptions VALUES(?,?,?)', (user_id, code, now))
        c.execute('UPDATE promo SET uses=uses+1 WHERE code=?', (code,))
        return True, ('Botlar soni va kunlik AI kvotasi cheksiz (doimiy).' if until < 0 else
                      f'Promo yoqildi: {p["days"]} kun qo‘shildi. Botlar soni va kunlik AI kvotasi cheksiz.')
