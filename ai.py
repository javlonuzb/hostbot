"""AI xizmati: DeepSeek orqali tavsif bo'yicha kod (.py / .php) yozish va xatolarni tuzatish.

API kaliti GitHub Secrets'dagi API_KEY dan olinadi (workflow uni muhit o'zgaruvchisi qilib beradi).
Ixtiyoriy sozlamalar: AI_MODEL (standart: deepseek-v4-flash), AI_BASE_URL, AI_MAX_TOKENS.
"""
import ast
import asyncio
import os
import re
import tempfile
from dataclasses import dataclass, field

MAX_PROMPT = 4000          # foydalanuvchi tavsifi uzunligi
MAX_CODE_CTX = 60_000      # tuzatish uchun yuboriladigan kod hajmi (belgi)
MAX_HISTORY = 4            # suhbatda saqlanadigan oxirgi xabarlar soni
RETRIES = 1                # sintaksis xato bo'lsa AI'ga qayta urinish

LANGS = {
    "py": {"title": "🐍 Python", "fence": ("python", "py"), "file": "bot.py"},
    "php": {"title": "🐘 PHP", "fence": ("php",), "file": "bot.php"},
}

_COMMON = """You are a senior Telegram bot developer. The user describes a bot or program; you return complete, \
working, production-quality code as ONE file.

Hard rules:
- Read the bot token from the environment: BOT_TOKEN. Never hardcode tokens or secrets.
- If a database is needed, use SQLite at the path from the environment variable DB_PATH (fallback "data.db").
- Use long polling (no webhooks, no web server). Single file, no placeholders, no TODOs, fully runnable.
- Handle errors so the bot does not crash on a single bad update.
- Write bot-facing texts in the language the user wrote the request in (default: Uzbek).
- Never ask the user for secrets and never print the token.

Reply format (strict):
1. One to three short sentences in Uzbek describing what you built or changed.
2. Exactly one fenced code block containing the COMPLETE file ({fence}).
3. Only if third-party packages are needed: one more fenced block tagged `requirements` with one package per \
line (e.g. aiogram>=3.13,<4). Omit it when nothing is needed.
No other text after the blocks."""

_PY = """Language: Python 3.12. Prefer aiogram 3.x (async) unless the user asks for another library."""
_PHP = """Language: PHP 8.x, command-line script. Do NOT use Composer packages: use cURL (ext-curl) and \
getUpdates long polling (timeout=30) in an endless loop with try/catch and a short sleep after errors. \
Use PDO SQLite for storage. The file must start with <?php. Do not output a requirements block."""


def system_prompt(lang):
    fence = "```python" if lang == "py" else "```php"
    return _COMMON.format(fence=fence) + "\n\n" + (_PY if lang == "py" else _PHP)


class AIError(Exception):
    """Foydalanuvchiga ko'rsatish mumkin bo'lgan (o'zbekcha) xato."""


@dataclass
class Result:
    lang: str
    code: str
    note: str = ""
    reqs: list = field(default_factory=list)
    warning: str = ""
    history: list = field(default_factory=list)

    @property
    def filename(self):
        return LANGS[self.lang]["file"]


def api_key():
    return os.getenv("API_KEY", "").strip()


def enabled():
    return bool(api_key())


def model():
    return os.getenv("AI_MODEL", "deepseek-v4-flash")


def _url():
    base = os.getenv("AI_BASE_URL", "https://api.deepseek.com").rstrip("/")
    return base + "/chat/completions"


# ----------------------------- API -----------------------------
async def chat(messages, max_tokens=None, temperature=0.2):
    """DeepSeek chat/completions (OpenAI formatida). Javob matnini qaytaradi."""
    key = api_key()
    if not key:
        raise AIError("AI ulanmagan: GitHub Secrets'ga API_KEY qo'shing.")
    import aiohttp

    payload = {
        "model": model(),
        "messages": messages,
        "max_tokens": max_tokens or int(os.getenv("AI_MAX_TOKENS", "8000")),
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=240)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(_url(), json=payload, headers=headers) as r:
                if r.status != 200:
                    raise AIError(_http_error(r.status))
                data = await r.json(content_type=None)
    except AIError:
        raise
    except asyncio.TimeoutError:
        raise AIError("AI javob berishga ulgurmadi (vaqt tugadi). Tavsifni qisqartirib qayta urinib ko'ring.")
    except Exception:
        raise AIError("AI serveriga ulanib bo'lmadi. Birozdan so'ng qayta urinib ko'ring.")
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise AIError("AI kutilmagan javob qaytardi.")
    if choice.get("finish_reason") == "length":
        raise AIError("Javob juda uzun bo'lib, uzilib qoldi. Talabni qisqartiring yoki bo'lib-bo'lib so'rang.")
    return text


def _http_error(status):
    return {
        401: "API_KEY noto'g'ri yoki muddati o'tgan.",
        402: "DeepSeek hisobida mablag' yetarli emas.",
        429: "AI so'rovlar limiti oshdi. Biroz kutib qayta urinib ko'ring.",
    }.get(status, f"AI xizmati xato qaytardi (HTTP {status}).")


# ----------------------------- javobni tahlil qilish -----------------------------
_FENCE = re.compile(r"```[ \t]*([\w+.\-]*)[^\n]*\n(.*?)```", re.S)
_REQ_TAGS = {"requirements", "requirements.txt", "req", "txt", "text"}
# Faqat oddiy "paket" yoki "paket>=1.0,<2" ko'rinishi: -r, URL, git+... va flaglar o'tmaydi
_REQ_LINE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(\[[A-Za-z0-9_,\-]+\])?"
    r"(\s*(==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!\-]+(\s*,\s*(==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!\-]+)*)?$"
)


def clean_requirements(text):
    out = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if line and _REQ_LINE.match(line) and line not in out:
            out.append(line)
    return out


def parse_reply(text, lang):
    """(izoh, kod, requirements ro'yxati). Kod bloki topilmasa AIError."""
    if text.count("```") % 2:
        raise AIError("AI javobi to'liq kelmadi (kod bloki uzilgan). Qayta urinib ko'ring.")
    blocks = _FENCE.findall(text)
    code, reqs = None, []
    for tag, body in blocks:
        tag = tag.lower()
        if tag in _REQ_TAGS:
            reqs = clean_requirements(body)
        elif code is None and (not tag or tag in LANGS[lang]["fence"] or tag in ("python", "php", "py")):
            code = body
    if not code or not code.strip():
        raise AIError("AI kod qaytarmadi. Tavsifni aniqroq yozib qayta urinib ko'ring.")
    note = _FENCE.sub("", text).strip()
    note = re.sub(r"\n{3,}", "\n\n", note)[:800]
    if lang == "php":
        reqs = []
    return note, code.strip("\n") + "\n", reqs


def redact(text, extra=()):
    """AI'ga yuborishdan oldin bot tokenlarini yashiradi."""
    text = re.sub(r"\d{6,}:[A-Za-z0-9_-]{30,}", "<BOT_TOKEN>", text)
    for s in extra:
        if s and len(s) > 8:
            text = text.replace(s, "<BOT_TOKEN>")
    return text


# ----------------------------- sintaksis tekshiruvi -----------------------------
async def check_syntax(code, lang):
    """None — xato yo'q (yoki tekshirib bo'lmadi); aks holda xato matni."""
    if lang == "py":
        try:
            ast.parse(code)
        except SyntaxError as e:
            return f"SyntaxError: {e.msg} (qator {e.lineno})"
        except ValueError as e:
            return str(e)
        return None
    import shutil
    php = shutil.which("php")
    if not php:
        return None
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "check.php")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(code)
        try:
            p = await asyncio.create_subprocess_exec(
                php, "-l", path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(p.communicate(), 15)
        except Exception:
            return None
        if p.returncode == 0:
            return None
        return out.decode("utf-8", "replace").replace(path, "bot.php").strip()[:500]


# ----------------------------- generatsiya -----------------------------
def _trim(history):
    h = history[-MAX_HISTORY:]
    while h and h[0]["role"] != "user":  # suhbat doim foydalanuvchi xabaridan boshlansin
        h = h[1:]
    return h


async def generate(lang, history):
    """history: [{role, content}, ...] (oxirgisi — foydalanuvchi so'rovi). Result qaytaradi."""
    if lang not in LANGS:
        raise AIError("Noma'lum til.")
    msgs = _trim(list(history))
    warning = ""
    for attempt in range(RETRIES + 1):
        text = await chat([{"role": "system", "content": system_prompt(lang)}] + msgs)
        note, code, reqs = parse_reply(text, lang)
        err = await check_syntax(code, lang)
        msgs = _trim(msgs + [{"role": "assistant", "content": text}])
        if not err:
            warning = ""
            break
        warning = f"Kodda sintaksis xatosi bor: {err}"
        if attempt < RETRIES:
            msgs = _trim(msgs + [{"role": "user", "content":
                f"The code has a syntax error: {err}\nFix it and return the full corrected file in the same format."}])
    return Result(lang=lang, code=code, note=note, reqs=reqs, warning=warning, history=msgs)


def fix_request(entry, code, log, instruction="", extra_secrets=()):
    """Mavjud botni tuzatish uchun birinchi foydalanuvchi xabari."""
    code = redact(code, extra_secrets)[:MAX_CODE_CTX]
    log = redact(log, extra_secrets)[-3000:]
    ask = instruction.strip() or "Loglardagi xatoni toping va tuzating."
    return (
        f"Quyidagi bot faylini ({entry}) tuzating. Faylning TO'LIQ tuzatilgan versiyasini qaytaring; "
        f"ishlayotgan qismlarni o'zgartirmang.\n\nTalab: {ask}\n\n"
        f"Oxirgi loglar:\n```\n{log or '(bo`sh)'}\n```\n\nFayl kodi:\n```\n{code}\n```"
    )


def merge_requirements(existing, new):
    """Mavjud requirements.txt matniga yangi paketlarni (nomi bo'yicha takrorlamasdan) qo'shadi."""
    def name(line):
        return re.split(r"[<>=!~\[\s;]", line.strip(), maxsplit=1)[0].lower().replace("_", "-")

    lines = [l for l in existing.splitlines()]
    have = {name(l) for l in lines if l.strip() and not l.strip().startswith("#")}
    for r in new:
        if name(r) not in have:
            lines.append(r)
            have.add(name(r))
    return "\n".join(lines).strip("\n") + "\n"
