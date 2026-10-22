"""Foydalanuvchi botlarini alohida jarayon (subprocess) sifatida ishga tushiradi."""
import asyncio
import hashlib
import os
import shutil
import signal
import sqlite3
import sys
import time
import zipfile

import db

MAX_ZIP_FILES = 300
MAX_UNZIPPED = 60 * 1024 * 1024
MAX_RESTARTS = 5
ENTRY_CANDIDATES = ("main.py", "bot.py", "app.py", "run.py", "index.py", "__main__.py")
DB_EXT = (".db", ".sqlite", ".sqlite3")
SKIP_DIRS = {"libs", "__pycache__", ".git", "venv", ".venv", "node_modules"}
SKIP_FILES = {"bot.log", ".req_hash"}

PROCS: dict = {}  # host_id -> {"proc", "log", "started", "stopping"}


# ----------------------------- fayllar -----------------------------
def safe_join(base, rel):
    base = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, rel))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError("Xavfli fayl yo'li")
    return target


def safe_extract(zip_path, dest):
    """Zipni xavfsiz ochadi (path traversal, symlink va katta arxivlardan himoya)."""
    dest = os.path.realpath(dest)
    os.makedirs(dest, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(zip_path) as z:
        infos = [
            i for i in z.infolist()
            if not i.is_dir() and not i.filename.replace("\\", "/").startswith("__MACOSX/")
        ]
        if len(infos) > MAX_ZIP_FILES:
            raise ValueError("Zip ichida fayllar juda ko'p")
        if sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise ValueError("Zip juda katta (ochilgach 60 MB dan oshadi)")
        names = []
        for i in infos:
            n = i.filename.replace("\\", "/")
            is_link = ((i.external_attr >> 16) & 0o170000) == 0o120000
            if n.startswith("/") or ".." in n.split("/") or is_link:
                raise ValueError(f"Xavfli fayl yo'li zip ichida: {i.filename}")
            names.append(n)
        prefix = ""
        tops = {n.split("/")[0] for n in names}
        if names and len(tops) == 1 and all("/" in n for n in names):
            prefix = tops.pop() + "/"
        for i, n in zip(infos, names):
            rel = n[len(prefix):]
            target = safe_join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted.append(rel)
    return extracted


def _walk(d):
    out = []
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for f in sorted(files):
            if f in SKIP_FILES:
                continue
            rel = os.path.relpath(os.path.join(root, f), d).replace(os.sep, "/")
            out.append(rel)
            if len(out) >= 100:
                return out
    return out


def list_files(d):
    return _walk(d) if os.path.isdir(d) else []


def list_py(d):
    return [f for f in list_files(d) if f.endswith(".py")]


def db_files(d):
    return [f for f in list_files(d) if f.lower().endswith(DB_EXT)]


def find_entry(d):
    for c in ENTRY_CANDIDATES:
        if os.path.isfile(os.path.join(d, c)):
            return c
    py = [f for f in list_py(d) if "/" not in f]
    if len(py) == 1:
        return py[0]
    for f in py:
        try:
            with open(os.path.join(d, f), encoding="utf-8", errors="ignore") as fh:
                txt = fh.read()
        except OSError:
            continue
        if any(k in txt for k in ("polling", "executor.start", "__main__")):
            return f
    return py[0] if py else None


def db_info(d):
    """Har bir baza fayli uchun jadvallar va qatorlar soni."""
    result = []
    for rel in db_files(d):
        path = os.path.join(d, rel)
        tables = []
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for t in names:
                q = '"' + t.replace('"', '""') + '"'
                tables.append((t, con.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]))
            con.close()
        except sqlite3.Error:
            tables = None
        result.append((rel, os.path.getsize(path), tables))
    return result


def clear_db(d):
    n = 0
    for rel in db_files(d):
        base = os.path.join(d, rel)
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = base + suffix
            if os.path.isfile(p):
                os.remove(p)
                n += 1 if suffix == "" else 0
    return n


# ----------------------------- jarayonlar -----------------------------
def child_env(host, d):
    """Bot jarayoniga faqat kerakli o'zgaruvchilar beriladi (asosiy bot tokeni YO'Q)."""
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT") if k in os.environ}
    tok = host["token"]
    env.update(
        HOME=d,
        BOT_TOKEN=tok,
        TELEGRAM_BOT_TOKEN=tok,
        TOKEN=tok,
        API_TOKEN=tok,
        BOT_DIR=d,
        DB_PATH=os.path.join(d, "data.db"),
        PYTHONUNBUFFERED="1",
        PYTHONPATH=os.path.join(d, "libs") + os.pathsep + d,
    )
    return env


def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1536 << 20, 1536 << 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (300 << 20, 300 << 20))
    except Exception:
        pass


def is_running(host_id):
    p = PROCS.get(host_id)
    return bool(p and p["proc"].returncode is None and not p.get("stopping"))


def _cleanup(host_id):
    p = PROCS.pop(host_id, None)
    if p:
        try:
            p["log"].close()
        except Exception:
            pass


async def _install_reqs(d, log):
    req = os.path.join(d, "requirements.txt")
    if not os.path.isfile(req):
        return True
    with open(req, "rb") as fh:
        h = hashlib.md5(fh.read()).hexdigest()
    marker = os.path.join(d, ".req_hash")
    libs = os.path.join(d, "libs")
    if os.path.isfile(marker) and os.path.isdir(libs):
        with open(marker) as fh:
            if fh.read() == h:
                return True
    log.write(b"[host] requirements o'rnatilmoqda...\n")
    log.flush()
    env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
    env["HOME"] = d
    p = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-r", req, "--target", libs,
        "-q", "--disable-pip-version-check",
        cwd=d, env=env, stdout=log, stderr=log, stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        rc = await asyncio.wait_for(p.wait(), 300)
    except asyncio.TimeoutError:
        p.kill()
        return False
    if rc == 0:
        with open(marker, "w") as fh:
            fh.write(h)
    return rc == 0


async def start(host):
    """(ok, xabar) qaytaradi."""
    i = host["id"]
    if is_running(i):
        return True, "Bot allaqachon ishlayapti."
    _cleanup(i)
    d = db.bot_dir(i)
    entry = host["entry"]
    if not entry or not os.path.isfile(os.path.join(d, entry)):
        return False, "Asosiy fayl topilmadi. Sozlamalardan tanlang."
    if zipfile.is_zipfile(os.path.join(d, entry)):
        return False, "Asosiy fayl .py emas, zip arxiv ekan. Sozlamalardan to'g'ri .py faylni tanlang."
    os.makedirs(d, exist_ok=True)
    log = open(os.path.join(d, "bot.log"), "ab")
    log.write(f"\n[host] {time.strftime('%Y-%m-%d %H:%M:%S')} ishga tushirilmoqda: {entry}\n".encode())
    log.flush()
    if not await _install_reqs(d, log):
        log.write(b"[host] requirements o'rnatilmadi\n")
        log.close()
        return False, "requirements.txt o'rnatilmadi (loglarni ko'ring)."
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", entry,
        cwd=d, env=child_env(host, d),
        stdout=log, stderr=log, stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True, preexec_fn=_limits,
    )
    PROCS[i] = {"proc": proc, "log": log, "started": time.time(), "stopping": False}
    await asyncio.sleep(2)
    if proc.returncode is not None:
        code = proc.returncode
        _cleanup(i)
        return False, f"Bot ishga tushib darrov to'xtadi (kod {code}). Loglarni tekshiring."
    return True, "Ishga tushdi."


async def stop(host_id):
    p = PROCS.get(host_id)
    if not p:
        return
    p["stopping"] = True
    proc = p["proc"]
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            await proc.wait()
    _cleanup(host_id)


async def stop_all():
    for i in list(PROCS):
        await stop(i)


def tail_log(host_id, n=3000):
    path = os.path.join(db.bot_dir(host_id), "bot.log")
    if not os.path.isfile(path):
        return ""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - n))
        return fh.read().decode("utf-8", errors="replace")


async def watchdog(notify=None, interval=5):
    """Qulagan botlarni qayta ishga tushiradi (ketma-ket 5 martagacha)."""
    restarts = {}
    while True:
        await asyncio.sleep(interval)
        for i, p in list(PROCS.items()):
            proc = p["proc"]
            if proc.returncode is None or p.get("stopping"):
                continue
            ran = time.time() - p["started"]
            code = proc.returncode
            _cleanup(i)
            host = db.get_host(i)
            if not host or not host["want_running"]:
                continue
            if code == 0:
                db.update_host(i, want_running=0)
                if notify:
                    await notify(host, "dastur o'zi yakunlandi (kod 0).")
                continue
            n = 0 if ran > 120 else restarts.get(i, 0) + 1
            restarts[i] = n
            if n > MAX_RESTARTS:
                db.update_host(i, want_running=0)
                if notify:
                    await notify(host, f"qayta-qayta qulayapti, to'xtatildi. Loglarni tekshiring (kod {code}).")
                continue
            if notify:
                await notify(host, f"qulab tushdi (kod {code}), qayta ishga tushirilmoqda ({n}/{MAX_RESTARTS})...")
            await asyncio.sleep(3)
            ok, _ = await start(db.get_host(i))
            if not ok:
                db.update_host(i, want_running=0)
