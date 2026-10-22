"""Hosting bot ma'lumotlar bazasi (SQLite). Qo'shilgan botlar shu yerda saqlanadi."""
import os
import sqlite3
import time

DATA_DIR = os.getenv("DATA_DIR", "data")

_FIELDS = {"name", "username", "token", "entry", "autorun", "want_running"}


def db_path():
    return os.path.join(DATA_DIR, "hosting.db")


def bot_dir(host_id):
    return os.path.join(DATA_DIR, "bots", str(host_id))


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
