"""Foydalanuvchi botlarini alohida jarayon sifatida ishga tushiradi (v2).

v2 qo'shimchalari: qulash sababi (traceback) yig'iladi, RAM/CPU statistikasi,
GitHub'dan o'rnatish/yangilash.
"""
import asyncio
import hashlib
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import zipfile

import db
import bot_secrets

MAX_ZIP_FILES = 300
MAX_UNZIPPED = 60 * 1024 * 1024
MAX_RESTARTS = 5
ENTRY_CANDIDATES = (
    "main.py", "bot.py", "app.py", "run.py", "index.py", "__main__.py",
    "bot.php", "index.php", "main.php", "app.php",
)
CODE_EXT = (".py", ".php")
DB_EXT = (".db", ".sqlite", ".sqlite3")
SKIP_DIRS = {"libs", "__pycache__", ".git", "venv", ".venv", "node_modules", "vendor", ".composer"}
SKIP_FILES = {"bot.log", ".req_hash", ".composer_hash", "bot.log.old"}
MAX_BACKUP = 49 * 1024 * 1024  # Telegram yuklash limiti ~50 MB

PROCS: dict = {}    # host_id -> {"proc", "log", "started", "stopping"}
CRASHES: dict = {}  # host_id -> {"code", "at", "tail", "entry"}


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


def list_code(d):
    """Ishga tushirish mumkin bo'lgan fayllar: .py va .php."""
    return [f for f in list_files(d) if f.lower().endswith(CODE_EXT)]


def lang_of(entry):
    return "php" if (entry or "").lower().endswith(".php") else "py"


def php_bin():
    return shutil.which("php")


_PHP_VER = None


def php_version():
    global _PHP_VER
    if _PHP_VER is None:
        _PHP_VER = ""
        b = php_bin()
        if b:
            try:
                out = subprocess.run([b, "-r", "echo PHP_VERSION;"], capture_output=True,
                                     text=True, timeout=5)
                _PHP_VER = out.stdout.strip()
            except Exception:
                pass
    return _PHP_VER


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
        if _has_marker(d, f, ("polling", "executor.start", "__main__")):
            return f
    if py:
        return py[0]
    php = [f for f in list_code(d) if "/" not in f and f.lower().endswith(".php")]
    if len(php) == 1:
        return php[0]
    for f in php:
        if _has_marker(d, f, ("getUpdates", "api.telegram.org")):
            return f
    return php[0] if php else None


def _has_marker(d, f, markers):
    try:
        with open(os.path.join(d, f), encoding="utf-8", errors="ignore") as fh:
            txt = fh.read()
    except OSError:
        return False
    return any(k in txt for k in markers)


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


def make_zip(d, out):
    """Bot papkasini zip qiladi (loglar, kutubxonalar va keshsiz). Hajmni qaytaradi."""
    has_composer = os.path.isfile(os.path.join(d, "composer.json"))
    skip = (SKIP_DIRS - {"vendor"}) | ({"vendor"} if has_composer else set())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if x not in skip)
            for f in sorted(files):
                if f in SKIP_FILES or f.endswith(("-wal", "-shm", "-journal")):
                    continue
                full = os.path.join(root, f)
                if os.path.islink(full):
                    continue
                z.write(full, os.path.relpath(full, d))
    size = os.path.getsize(out)
    if size > MAX_BACKUP:
        os.remove(out)
        raise ValueError("Zip juda katta (50 MB dan oshadi)")
    return size


def uptime(host_id):
    p = PROCS.get(host_id)
    if p and p["proc"].returncode is None and not p.get("stopping"):
        return int(time.time() - p["started"])
    return None


# ----------------------------- resurslar -----------------------------
def _proc_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def res_stats(host_id):
    """{"pid", "ram", "cpu", "threads", "since"} yoki None (ishlamayapti)."""
    p = PROCS.get(host_id)
    if not p or p["proc"].returncode is not None or p.get("stopping"):
        return None
    pid = p["proc"].pid
    info = {"pid": pid, "since": p["started"], "ram": 0, "cpu": 0.0, "threads": 0}
    try:
        import psutil
        pr = p.get('psutil_proc')
        if pr is None:
            pr = psutil.Process(pid)
            p['psutil_proc'] = pr
        with pr.oneshot():
            info["ram"] = pr.memory_info().rss
            info["cpu"] = pr.cpu_percent(None)
            info["threads"] = pr.num_threads()
            kids = pr.children(recursive=True)
            for k in kids:
                try:
                    info["ram"] += k.memory_info().rss
                except Exception:
                    pass
            info["children"] = len(kids)
        return info
    except Exception:
        pass
    # psutil yo'q: /proc orqali
    try:
        with open(f"/proc/{pid}/stat") as fh:
            parts = fh.read().split()
        ticks = int(parts[13]) + int(parts[14])
        now = time.time()
        prev = p.get("cpu_sample")
        if prev and now > prev[0]:
            hz = os.sysconf("SC_CLK_TCK") or 100
            info["cpu"] = min(100.0 * ((ticks - prev[1]) / hz) / (now - prev[0]), 999.0)
        p["cpu_sample"] = (now, ticks)
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    info["ram"] = int(line.split()[1]) * 1024
                    break
        try:
            info["threads"] = len(os.listdir(f"/proc/{pid}/task"))
        except OSError:
            pass
        return info
    except Exception:
        return None


def total_ram():
    try:
        import psutil
        return psutil.virtual_memory().total
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


# ----------------------------- xato tahlili -----------------------------
_ERROR_RE = re.compile(
    r"((?:[A-Za-z_][\w.]*(?:Error|Exception|Warning|Fatal)|Exception|Error)\b.*)", re.M
)


def extract_error(text, limit=1400):
    """Logdan eng mazmunli xato qismini ajratib oladi (traceback ustun)."""
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        return ""
    idx = text.rfind("Traceback (most recent call last)")
    if idx != -1:
        block = text[idx:]
        return block[-limit:] if len(block) > limit else block
    hits = _ERROR_RE.findall(text)
    if hits:
        lines = [h.strip() for h in hits if h.strip()]
        return "\n".join(lines[-8:])[-limit:]
    tail = text.strip().splitlines()[-12:]
    return "\n".join(tail)[-limit:]


# ----------------------------- GitHub -----------------------------
def normalize_repo(url):
    u = (url or '').strip()
    m = re.fullmatch(r'(?:(?:https://)?(?:www\.)?github\.com/)?([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9_][A-Za-z0-9_.-]*?)(?:\.git)?/?', u)
    if not m or m.group(1) in ('.', '..') or m.group(2) in ('.', '..'):
        return ''
    return f'https://github.com/{m.group(1)}/{m.group(2)}.git'


async def _run(cmd, cwd=None, timeout=300):
    env = {k: os.environ[k] for k in ("PATH", "LANG", "HOME") if k in os.environ}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/true")
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode("utf-8", "replace")
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 1, "Vaqt tugadi (git buyrug'i 300 soniyada tugamadi)."
    except Exception as e:
        return 1, str(e)


def git_available():
    return bool(shutil.which("git"))


async def git_clone(repo, branch, dest):
    """(ok, xabar) — repo'ni dest papkaga (bo'sh bo'lishi shart) yuklaydi."""
    if not git_available():
        return False, "Serverda git o'rnatilmagan."
    url = normalize_repo(repo)
    if not url:
        return False, "Repo havolasi noto'g'ri. Masalan: github.com/user/repo yoki user/repo"
    os.makedirs(dest, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    if branch and (branch.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_./-]{1,120}', branch)):
        return False, 'Branch nomi noto‘g‘ri.'
    cmd += ['--', url, dest]
    rc, out = await _run(cmd, timeout=300)
    if rc != 0:
        tail = out.strip().splitlines()[-4:]
        return False, "Git clone xatosi: " + " | ".join(tail)[:400]
    return True, "Repo yuklandi."


async def git_pull(d, branch=""):
    """(ok, xabar) — mavjud nusxani yangilaydi."""
    if not git_available():
        return False, "Serverda git o'rnatilmagan."
    if not os.path.isdir(os.path.join(d, ".git")):
        return False, "Bu papka git repo emas. GitHub'dan qaytadan o'rnating."
    cfg = ["git", "-c", "safe.directory=*", "-C", d]
    rc, out = await _run(cfg + ["fetch", "--depth", "1", "origin"], timeout=300)
    if rc != 0:
        return False, "Fetch xatosi: " + out.strip()[-300:]
    if branch:
        rc, out = await _run(cfg + ["reset", "--hard", f"origin/{branch}"], timeout=120)
    else:
        rc, out = await _run(cfg + ["reset", "--hard", "FETCH_HEAD"], timeout=120)
    if rc != 0:
        return False, "Yangilash xatosi: " + out.strip()[-300:]
    return True, "Repo yangilandi."


def git_head(d):
    if not os.path.isdir(os.path.join(d, ".git")):
        return ""
    try:
        out = subprocess.run(["git", "-c", "safe.directory=*", "-C", d, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=8)
        return out.stdout.strip()
    except Exception:
        return ""


# ----------------------------- jarayonlar -----------------------------
def build_cmd(entry):
    """Fayl turiga qarab ishga tushirish buyrug'i."""
    if lang_of(entry) == "php":
        php = php_bin()
        if not php:
            raise RuntimeError("Serverda PHP o'rnatilmagan.")
        return [php, "-d", "display_errors=1", "-d", "error_reporting=E_ALL",
                "-d", "implicit_flush=1", "-d", "memory_limit=512M", entry]
    return [sys.executable, "-u", entry]


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
    try:
        hid = host['id']
    except (KeyError, IndexError):
        hid = None
    if hid is not None:
        env.update(bot_secrets.env_for(hid))
    return env  # only per-bot explicit keys; never inherit master API_KEY


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


async def _install_composer(d, log):
    cj = os.path.join(d, "composer.json")
    if not os.path.isfile(cj):
        return True
    h = hashlib.md5()
    for name in ("composer.json", "composer.lock"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                h.update(fh.read())
    h = h.hexdigest()
    marker = os.path.join(d, ".composer_hash")
    if os.path.isfile(marker) and os.path.isdir(os.path.join(d, "vendor")):
        with open(marker) as fh:
            if fh.read() == h:
                return True
    composer = shutil.which("composer")
    if not composer:
        log.write(b"[host] composer topilmadi, composer.json o'rnatilmadi\n")
        log.flush()
        return False
    log.write(b"[host] composer paketlari o'rnatilmoqda...\n")
    log.flush()
    env = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
    env.update(HOME=d, COMPOSER_HOME=os.path.join(d, ".composer"), COMPOSER_ALLOW_SUPERUSER="1")
    p = await asyncio.create_subprocess_exec(
        composer, "install", "--no-dev", "--no-interaction", "--no-progress", "--prefer-dist",
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


async def start(host, extra_env=None):
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
    try:
        cmd = build_cmd(entry)
    except RuntimeError as e:
        return False, str(e)
    os.makedirs(d, exist_ok=True)
    log = open(os.path.join(d, "bot.log"), "ab")
    log.write(f"\n[host] {time.strftime('%Y-%m-%d %H:%M:%S')} ishga tushirilmoqda: {entry}\n".encode())
    log.flush()
    if lang_of(entry) == "php":
        installed, what = await _install_composer(d, log), "composer.json"
    else:
        installed, what = await _install_reqs(d, log), "requirements.txt"
    if not installed:
        log.write(f"[host] {what} o'rnatilmadi\n".encode())
        log.close()
        return False, f"{what} o'rnatilmadi (loglarni ko'ring)."
    try:
        env = child_env(host, d)
    except ValueError as e:
        log.close()
        return False, str(e)
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if k not in env})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=d, env=env,
            stdout=log, stderr=log, stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True, preexec_fn=_limits,
        )
    except (OSError, ValueError) as e:
        log.close()
        return False, f'Jarayon ishga tushmadi: {type(e).__name__}'

    PROCS[i] = {"proc": proc, "log": log, "started": time.time(), "stopping": False}
    CRASHES.pop(i, None)
    await asyncio.sleep(2)
    if proc.returncode is not None:
        code = proc.returncode
        log.flush()
        tail = bot_secrets.redact(extract_error(tail_log(i, 4000)), i)
        CRASHES[i] = {"code": code, "at": time.time(), "tail": tail, "entry": entry}
        db.update_host(i, last_error=tail[:2000])
        return False, f"Bot ishga tushib darrov to'xtadi (kod {code})."
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


def crash_of(host_id):
    """Oxirgi qulash ma'lumoti (yoki bazadan)."""
    c = CRASHES.get(host_id)
    if c:
        return c
    h = db.get_host(host_id)
    if h and h["last_error"]:
        return {"code": None, "at": None, "tail": h["last_error"], "entry": h["entry"]}
    return None


async def watchdog(notify=None, interval=5):
    """Qulagan botlarni qayta ishga tushiradi va egasiga xato yuboradi."""
    restarts = {}
    while True:
        await asyncio.sleep(interval)
        import backup
        if backup.LOCK.locked():
            continue
        for i, p in list(PROCS.items()):
            proc = p["proc"]
            if proc.returncode is None or p.get("stopping"):
                continue
            ran = time.time() - p["started"]
            code = proc.returncode
            tail = bot_secrets.redact(extract_error(tail_log(i, 4000)), i)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            _cleanup(i)
            host = db.get_host(i)
            if not host:
                continue
            tries = (host["crashes"] or 0) + 1
            db.update_host(i, crashes=tries, last_error=tail[:2000] or host["last_error"])
            if not host["want_running"]:
                continue
            if code == 0:
                db.update_host(i, want_running=0)
                if notify:
                    await notify(host, "Dastur o'zi yakunlandi (kod 0).", tail="", code=0)
                continue
            n = 1 if ran > 120 else restarts.get(i, 0) + 1
            restarts[i] = n
            CRASHES[i] = {"code": code, "at": time.time(), "tail": tail, "entry": host["entry"]}
            if n > MAX_RESTARTS:
                db.update_host(i, want_running=0)
                if notify:
                    await notify(host, f"Qayta-qayta qulayapti, to'xtatildi (kod {code}).", tail, code)
                continue
            if notify:
                await notify(host, f"Qulab tushdi (kod {code}). Qayta ishga tushirilmoqda "
                                   f"({n}/{MAX_RESTARTS})...", tail, code)
            await asyncio.sleep(3)
            ok, _ = await start(db.get_host(i))
            if not ok and i not in PROCS:
                db.update_host(i, want_running=0)
                if notify:
                    await notify(host, 'Qayta ishga tushirish amalga oshmadi.',
                                 extract_error(tail_log(i, 3000)), code)
