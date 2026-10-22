"""
HOSTING BOT: Telegram bot kodini (.py, .php yoki .zip) qabul qilib, avtomatik ishga tushiradi.
Panel: Restart, To'xtatish, Faylni o'zgartirish, Yangi fayl, Loglar, Baza, Fayllar,
Sozlamalar, Zip yuklab olish, AI bilan tuzatish, Botni o'chirish, Bazani tozalash.
Qo'shimcha: PHP botlar, AI bilan kod yozish (DeepSeek), yangiliklar bo'limi.
"""
import asyncio
import html
import logging
import os
import platform
import re
import shutil
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai
import db
import runner

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hostbot")

MASTER_TOKEN = os.getenv("MASTER_BOT_TOKEN", "")
MAX_RUNTIME_MIN = int(os.getenv("MAX_RUNTIME_MIN", "0"))
_ALLOWED_LIST = [int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USERS", ""))]
ALLOWED = set(_ALLOWED_LIST)
# Adminlar (yangilik qo'sha oladi): ADMIN_USERS yoki, bo'sh bo'lsa, ALLOWED_USERS dagi birinchi ID
ADMINS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_USERS", ""))} or set(_ALLOWED_LIST[:1])
AI_COOLDOWN = 8  # soniya: bir foydalanuvchining AI so'rovlari orasidagi pauza
MAX_BOTS = 5
MAX_FILE = 10 * 1024 * 1024


def esc(s):
    return html.escape(str(s or ""))


def tmp_dir():
    p = os.path.join(db.DATA_DIR, "tmp")
    os.makedirs(p, exist_ok=True)
    return p


def safe_name(name):
    name = os.path.basename(name or "bot.py")
    name = re.sub(r"[^\w.\-]", "_", name)
    return name or "file"


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


master = Router()


# ----------------------------- ruxsat -----------------------------
class Access(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if isinstance(event, Message) and (event.text or "").strip().startswith("/id"):
            return await handler(event, data)
        if not user or user.id not in ALLOWED:
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Ruxsat yo'q", show_alert=True)
            elif isinstance(event, Message):
                uid = user.id if user else "?"
                await event.answer(
                    f"⛔ Ruxsat yo'q.\nSizning ID: <code>{uid}</code>\n"
                    "Egasi bu ID'ni ALLOWED_USERS ga qo'shishi kerak."
                )
            return
        return await handler(event, data)


master.message.outer_middleware(Access())
master.callback_query.outer_middleware(Access())


# ----------------------------- holatlar -----------------------------
class Add(StatesGroup):
    token = State()
    file = State()


class Upload(StatesGroup):
    file = State()
    confirm = State()


class Rename(StatesGroup):
    name = State()


class NewToken(StatesGroup):
    token = State()


class AI(StatesGroup):
    prompt = State()   # yangi kod tavsifini kutish
    fix = State()      # mavjud botni tuzatish uchun izoh kutish
    result = State()   # natija ko'rsatilgan
    refine = State()   # o'zgartirish talabini kutish


class News(StatesGroup):
    text = State()


# ----------------------------- yordamchilar -----------------------------
def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await msg.answer(text, reply_markup=kb)


async def render(target, text, kb=None):
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


def main_menu(uid=None):
    n = db.unread_news(uid) if uid else 0
    b = InlineKeyboardBuilder()
    b.row(btn("➕ Bot qo'shish", "add"))
    b.row(btn("🤖 Botlarim", "mine"))
    b.row(btn("🧠 AI bilan kod yozish", "ai"))
    b.row(btn("📰 Yangiliklar" + (f" · 🔴 {n}" if n else ""), "news"))
    return b.as_markup()


WELCOME = (
    "👋 <b>Hosting Bot</b>\n"
    "Bot kodingizni (.py, .php yoki .zip) yuboring, men uni ishga tushiraman.\n"
    "Kod yozishni xohlamasangiz — «🧠 AI bilan kod yozish» tugmasidan foydalaning."
)


def fmt_dur(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d} kun {h} soat"
    if h:
        return f"{h} soat {m} daq"
    return f"{m} daq" if m else f"{int(sec)} soniya"


def back_kb(hid):
    b = InlineKeyboardBuilder()
    b.row(btn("⬅️ Orqaga", f"p:{hid}"))
    return b.as_markup()


def panel_text(h):
    up = runner.uptime(h["id"])
    status = f"🟢 Ishlamoqda · {fmt_dur(up)}" if up is not None else "🔴 To'xtatilgan"
    uname = f"\n@{esc(h['username'])}" if h["username"] else ""
    if runner.lang_of(h["entry"]) == "php":
        lang = f"🐘 PHP {runner.php_version() or '?'}"
    else:
        lang = f"🐍 Python {platform.python_version()}"
    return (
        f"🤖 <b>{esc(h['name'])}</b>{uname}\n{status}\n"
        f"ID: <code>{h['id']}</code> · {lang}"
    )


def panel_kb(h):
    i = h["id"]
    running = runner.is_running(i)
    b = InlineKeyboardBuilder()
    b.row(btn("🔄 Restart", f"rs:{i}"),
          btn("⏹ To'xtatish" if running else "▶️ Ishga tushirish", f"st:{i}"))
    b.row(btn("✏️ Faylni o'zgartirish", f"ef:{i}"), btn("📄 Yangi fayl", f"nf:{i}"))
    b.row(btn("📜 Loglar", f"lg:{i}"), btn("🗄 Bazani boshqarish", f"dbm:{i}"))
    b.row(btn("📁 Fayllar", f"fl:{i}"), btn("⚙️ Sozlamalar", f"se:{i}"))
    b.row(btn("🛠 AI bilan tuzatish", f"aif:{i}"), btn("📦 Zip yuklab olish", f"zp:{i}"))
    b.row(btn("🗑 Botni o'chirish", f"dl:{i}"), btn("🧹 Bazani tozalash", f"cd:{i}"))
    b.row(btn("⬅️ Orqaga", "mine"))
    return b.as_markup()


async def show_panel(target, hid, note=""):
    h = db.get_host(hid)
    if not h:
        return await render(target, "Bot topilmadi.", main_menu())
    text = (f"{note}\n\n" if note else "") + panel_text(h)
    await render(target, text, panel_kb(h))


async def get_owned(c: CallbackQuery):
    try:
        hid = int(c.data.split(":")[1])
    except (IndexError, ValueError):
        await c.answer()
        return None
    h = db.get_host(hid)
    if not h or h["owner_id"] != c.from_user.id:
        await c.answer("Ruxsat yo'q", show_alert=True)
        return None
    return h


async def restart_host(hid):
    await runner.stop(hid)
    db.update_host(hid, want_running=1)
    ok, msg = await runner.start(db.get_host(hid))
    if not ok:
        db.update_host(hid, want_running=0)
    return ok, msg


async def download(bot: Bot, doc, uid):
    name = safe_name(doc.file_name)
    path = os.path.join(tmp_dir(), f"{uid}_{int(time.time())}_{name}")
    await bot.download(doc, destination=path)
    return path, name


def install_upload(d, tmp, name):
    """Zip bo'lsa ochadi va fayllar ro'yxatini qaytaradi, aks holda nusxalaydi."""
    if name.lower().endswith(".zip"):
        return runner.safe_extract(tmp, d)
    shutil.copy(tmp, os.path.join(d, name))
    return []


def doc_error(doc, only_code=False):
    if doc.file_size and doc.file_size > MAX_FILE:
        return "❌ Fayl juda katta (maksimum 10 MB)."
    if only_code and not (doc.file_name or "").lower().endswith((".py", ".php", ".zip")):
        return "❌ Faqat .py, .php yoki .zip fayl yuboring."
    return None


# ----------------------------- boshlash -----------------------------
@master.message(Command("id"))
async def m_id(m: Message):
    await m.answer(f"Sizning ID: <code>{m.from_user.id}</code>")


@master.message(CommandStart())
@master.message(Command("menu"))
async def m_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=main_menu(m.from_user.id))


@master.message(Command("cancel"))
async def m_cancel(m: Message, state: FSMContext):
    tmp = (await state.get_data()).get("ai_tmp")
    if tmp:
        try:
            os.remove(tmp)
        except OSError:
            pass
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=main_menu(m.from_user.id))


@master.message(Command("ai"))
async def m_ai(m: Message, state: FSMContext):
    await state.clear()
    if not ai.enabled():
        return await m.answer(AI_OFF)
    await m.answer(AI_INTRO, reply_markup=ai_lang_kb())


@master.message(Command("news"))
async def m_news(m: Message, state: FSMContext):
    await state.clear()
    text, kb = news_view(m.from_user.id)
    db.mark_news_seen(m.from_user.id)
    await m.answer(text, reply_markup=kb)


@master.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, WELCOME, main_menu(c.from_user.id))
    await c.answer()


@master.callback_query(F.data == "mine")
async def cb_mine(c: CallbackQuery, state: FSMContext):
    await state.clear()
    hosts = db.user_hosts(c.from_user.id)
    b = InlineKeyboardBuilder()
    for h in hosts:
        dot = "🟢" if runner.is_running(h["id"]) else "🔴"
        b.row(btn(f"{dot} {h['name']}", f"p:{h['id']}"))
    b.row(btn("➕ Bot qo'shish", "add"))
    await safe_edit(c.message, "🤖 Botlaringiz:" if hosts else "Sizda hali bot yo'q.", b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("p:"))
async def cb_panel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    h = await get_owned(c)
    if h:
        await show_panel(c, h["id"])
        await c.answer()


# ----------------------------- bot qo'shish -----------------------------
@master.callback_query(F.data == "add")
async def cb_add(c: CallbackQuery, state: FSMContext):
    if len(db.user_hosts(c.from_user.id)) >= MAX_BOTS:
        return await c.answer(f"Maksimum {MAX_BOTS} ta bot", show_alert=True)
    await state.set_state(Add.token)
    await c.message.answer("1️⃣ Bot <b>tokenini</b> yuboring (@BotFather'dan).\nBekor qilish: /cancel")
    await c.answer()


@master.message(Add.token)
async def add_token(m: Message, state: FSMContext):
    token = (m.text or "").strip()
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        return await m.answer("❌ Token formati noto'g'ri. Qayta yuboring yoki /cancel.")
    try:
        tb = Bot(token)
        me = await tb.get_me()
        await tb.session.close()
    except Exception:
        return await m.answer("❌ Token ishlamayapti. Tekshirib qayta yuboring.")
    try:
        await m.delete()  # tokenni chatdan olib tashlash
    except Exception:
        pass
    await state.update_data(token=token, name=me.full_name, username=me.username)
    data = await state.get_data()
    if data.get("ai_tmp") and os.path.isfile(data["ai_tmp"]):
        await m.answer(f"✅ @{esc(me.username)} topildi. AI yozgan kod o'rnatilmoqda...")
        return await finish_add(m, state, data["ai_tmp"], data["ai_name"], data.get("ai_reqs"))
    await state.set_state(Add.file)
    await m.answer(
        f"✅ @{esc(me.username)} topildi.\n\n2️⃣ Endi bot kodini yuboring: bitta <b>.py</b> yoki "
        "<b>.php</b> fayl, yoki <b>.zip</b> arxiv (requirements.txt / composer.json bo'lsa, o'zi o'rnatiladi)."
    )


@master.message(Add.file, F.document)
async def add_file(m: Message, state: FSMContext, bot: Bot):
    err = doc_error(m.document, only_code=True)
    if err:
        return await m.answer(err)
    tmp, name = await download(bot, m.document, m.from_user.id)
    await finish_add(m, state, tmp, name)


async def finish_add(m: Message, state: FSMContext, tmp, name, reqs=None):
    """Yangi bot yozuvini yaratadi, faylni o'rnatadi va ishga tushiradi."""
    data = await state.get_data()
    hid = db.add_host(m.from_user.id, data["token"], data["name"], data["username"])
    d = db.bot_dir(hid)
    os.makedirs(d, exist_ok=True)
    try:
        install_upload(d, tmp, name)
        if reqs:
            with open(os.path.join(d, "requirements.txt"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(reqs) + "\n")
        entry = runner.find_entry(d)
        if not entry:
            raise ValueError("Python (.py) yoki PHP (.php) fayl topilmadi")
    except Exception as e:
        db.delete_host(hid)
        shutil.rmtree(d, ignore_errors=True)
        return await m.answer(f"❌ {esc(e)}\nBoshqa fayl yuboring yoki /cancel.")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    db.update_host(hid, entry=entry, want_running=1)
    await state.clear()
    wait = await m.answer("⏳ Ishga tushirilmoqda...")
    ok, msg = await runner.start(db.get_host(hid))
    if not ok:
        db.update_host(hid, want_running=0)
    try:
        await wait.delete()
    except Exception:
        pass
    await show_panel(m, hid, ("✅ " if ok else "⚠️ ") + esc(msg) + f"\nAsosiy fayl: <code>{esc(entry)}</code>")


@master.message(Add.file)
async def add_file_wrong(m: Message):
    await m.answer("Iltimos, .py, .php yoki .zip fayl yuboring (yoki /cancel).")


# ----------------------------- restart / stop -----------------------------
@master.callback_query(F.data.startswith("rs:"))
async def cb_restart(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    await c.answer("Qayta ishga tushirilmoqda...")
    ok, msg = await restart_host(h["id"])
    await show_panel(c, h["id"], ("✅ Restart qilindi." if ok else "⚠️ " + esc(msg)))


@master.callback_query(F.data.startswith("st:"))
async def cb_toggle(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    if runner.is_running(h["id"]):
        await runner.stop(h["id"])
        db.update_host(h["id"], want_running=0)
        note = "⏹ To'xtatildi."
    else:
        await c.answer("Ishga tushirilmoqda...")
        db.update_host(h["id"], want_running=1)
        ok, msg = await runner.start(db.get_host(h["id"]))
        if not ok:
            db.update_host(h["id"], want_running=0)
        note = ("✅ " if ok else "⚠️ ") + esc(msg)
    await show_panel(c, h["id"], note)
    try:
        await c.answer()
    except Exception:
        pass


# ----------------------------- fayl o'zgartirish / yangi fayl -----------------------------
@master.callback_query(F.data.startswith("ef:"))
async def cb_edit_list(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    files = runner.list_files(db.bot_dir(h["id"]))[:30]
    b = InlineKeyboardBuilder()
    for idx, f in enumerate(files):
        b.row(btn(f"✏️ {f}", f"efp:{h['id']}:{idx}"))
    b.row(btn("⬅️ Orqaga", f"p:{h['id']}"))
    await safe_edit(c.message, "Qaysi faylni almashtiramiz?" if files else "Fayllar yo'q.", b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("efp:"))
async def cb_edit_pick(c: CallbackQuery, state: FSMContext):
    h = await get_owned(c)
    if not h:
        return
    files = runner.list_files(db.bot_dir(h["id"]))[:30]
    try:
        target = files[int(c.data.split(":")[2])]
    except (IndexError, ValueError):
        return await c.answer("Fayl topilmadi", show_alert=True)
    await state.set_state(Upload.file)
    await state.update_data(hid=h["id"], mode="edit", target=target)
    await c.message.answer(f"📎 <code>{esc(target)}</code> uchun <b>yangi faylni</b> yuboring.\nBekor: /cancel")
    await c.answer()


@master.callback_query(F.data.startswith("nf:"))
async def cb_new_file(c: CallbackQuery, state: FSMContext):
    h = await get_owned(c)
    if not h:
        return
    await state.set_state(Upload.file)
    await state.update_data(hid=h["id"], mode="new", target=None)
    await c.message.answer("📄 Yangi faylni yuboring (.py, .zip yoki boshqa fayl; .zip ochib qo'yiladi).\nBekor: /cancel")
    await c.answer()


@master.message(Upload.file, F.document)
async def upload_file(m: Message, state: FSMContext, bot: Bot):
    err = doc_error(m.document)
    if err:
        return await m.answer(err)
    data = await state.get_data()
    h = db.get_host(data["hid"])
    if not h or h["owner_id"] != m.from_user.id:
        await state.clear()
        return
    if data["mode"] == "edit" and (m.document.file_name or "").lower().endswith(".zip") \
            and not data["target"].lower().endswith(".zip"):
        return await m.answer(
            "❌ Zip bilan bitta faylni almashtirib bo'lmaydi.\n"
            "Zip uchun «📄 Yangi fayl» tugmasidan foydalaning, yoki shu yerga oddiy fayl yuboring (/cancel — bekor)."
        )
    tmp, name = await download(bot, m.document, m.from_user.id)
    await state.update_data(tmp=tmp, name=name)
    await state.set_state(Upload.confirm)
    if data["mode"] == "edit":
        text = f"<code>{esc(data['target'])}</code> fayli <code>{esc(name)}</code> bilan almashtiriladi."
    else:
        extra = " (arxiv ochiladi)" if name.lower().endswith(".zip") else ""
        text = f"<code>{esc(name)}</code> botga qo'shiladi{extra}."
    b = InlineKeyboardBuilder()
    b.row(btn("✅ Tasdiqlash va ishga tushirish", "cf"))
    b.row(btn("❌ Bekor qilish", "cx"))
    await m.answer(text + "\nTasdiqlaysizmi?", reply_markup=b.as_markup())


@master.message(Upload.file)
async def upload_wrong(m: Message):
    await m.answer("Iltimos, fayl yuboring (yoki /cancel).")


@master.callback_query(Upload.confirm, F.data == "cf")
async def upload_confirm(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    h = db.get_host(data["hid"])
    await state.clear()
    if not h or h["owner_id"] != c.from_user.id:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    d = db.bot_dir(h["id"])
    tmp = data["tmp"]
    await c.answer("Qo'llanmoqda...")
    try:
        if data["mode"] == "edit":
            shutil.copy(tmp, runner.safe_join(d, data["target"]))
        else:
            extracted = install_upload(d, tmp, data["name"])
            cand = next((e for e in runner.ENTRY_CANDIDATES if e in extracted), None)
            if cand:
                db.update_host(h["id"], entry=cand)
        cur = db.get_host(h["id"])
        if not cur["entry"] or not os.path.isfile(os.path.join(d, cur["entry"])):
            db.update_host(h["id"], entry=runner.find_entry(d))
    except Exception as e:
        return await safe_edit(c.message, f"❌ {esc(e)}", back_kb(h["id"]))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    await safe_edit(c.message, "⏳ Qayta ishga tushirilmoqda...")
    ok, msg = await restart_host(h["id"])
    await show_panel(c, h["id"], ("✅ Fayl yangilandi va bot ishga tushdi." if ok else "⚠️ " + esc(msg)))


@master.callback_query(Upload.confirm, F.data == "cx")
async def upload_cancel(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    try:
        os.remove(data.get("tmp", ""))
    except OSError:
        pass
    await show_panel(c, data["hid"], "Bekor qilindi.")
    await c.answer()


# ----------------------------- loglar -----------------------------
@master.callback_query(F.data.startswith("lg:"))
async def cb_logs(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    txt = runner.tail_log(h["id"], 3000) or "(log bo'sh)"
    b = InlineKeyboardBuilder()
    b.row(btn("🔄 Yangilash", f"lg:{h['id']}"), btn("📄 To'liq log", f"lgf:{h['id']}"))
    b.row(btn("⬅️ Orqaga", f"p:{h['id']}"))
    await safe_edit(c.message, f"📜 <b>Loglar</b>\n<pre>{esc(txt[-3000:])}</pre>", b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("lgf:"))
async def cb_log_file(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    path = os.path.join(db.bot_dir(h["id"]), "bot.log")
    if not os.path.isfile(path):
        return await c.answer("Log fayli yo'q", show_alert=True)
    await c.message.answer_document(FSInputFile(path, filename=f"bot_{h['id']}.log"))
    await c.answer()


# ----------------------------- fayllar -----------------------------
@master.callback_query(F.data.startswith("fl:"))
async def cb_files(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    d = db.bot_dir(h["id"])
    files = runner.list_files(d)[:30]
    b = InlineKeyboardBuilder()
    lines = []
    for idx, f in enumerate(files):
        size = os.path.getsize(os.path.join(d, f))
        lines.append(f"📄 {esc(f)} ({human(size)})")
        b.row(btn(f"⬇️ {f}", f"fld:{h['id']}:{idx}"))
    b.row(btn("⬅️ Orqaga", f"p:{h['id']}"))
    await safe_edit(c.message, "📁 <b>Fayllar</b>\n" + ("\n".join(lines) or "Bo'sh"), b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("fld:"))
async def cb_file_dl(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    d = db.bot_dir(h["id"])
    files = runner.list_files(d)[:30]
    try:
        rel = files[int(c.data.split(":")[2])]
        path = runner.safe_join(d, rel)
    except (IndexError, ValueError):
        return await c.answer("Fayl topilmadi", show_alert=True)
    await c.message.answer_document(FSInputFile(path, filename=os.path.basename(rel)))
    await c.answer()


# ----------------------------- baza -----------------------------
@master.callback_query(F.data.startswith("dbm:"))
async def cb_db(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    info = runner.db_info(db.bot_dir(h["id"]))
    b = InlineKeyboardBuilder()
    if not info:
        text = (
            "🗄 Baza fayli topilmadi.\nBot SQLite fayl yaratsa (.db / .sqlite) shu yerda chiqadi.\n"
            "Bot ichida <code>os.getenv('DB_PATH')</code> orqali tayyor yo'ldan foydalanishingiz mumkin."
        )
    else:
        parts = []
        for idx, (rel, size, tables) in enumerate(info):
            parts.append(f"🗄 <b>{esc(rel)}</b> ({human(size)})")
            if tables is None:
                parts.append("  ⚠️ o'qib bo'lmadi")
            for t, n in tables or []:
                parts.append(f"  • {esc(t)}: {n} qator")
            b.row(btn(f"⬇️ {rel}", f"dbd:{h['id']}:{idx}"))
        text = "\n".join(parts)
    b.row(btn("🧹 Bazani tozalash", f"cd:{h['id']}"))
    b.row(btn("⬅️ Orqaga", f"p:{h['id']}"))
    await safe_edit(c.message, text, b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("dbd:"))
async def cb_db_dl(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    d = db.bot_dir(h["id"])
    files = runner.db_files(d)
    try:
        rel = files[int(c.data.split(":")[2])]
        path = runner.safe_join(d, rel)
    except (IndexError, ValueError):
        return await c.answer("Fayl topilmadi", show_alert=True)
    await c.message.answer_document(FSInputFile(path, filename=os.path.basename(rel)))
    await c.answer()


@master.callback_query(F.data.startswith("cd:"))
async def cb_clear_ask(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    b = InlineKeyboardBuilder()
    b.row(btn("✅ Ha, tozalash", f"cdo:{h['id']}"), btn("⬅️ Yo'q", f"p:{h['id']}"))
    await safe_edit(c.message, "⚠️ Botning barcha baza fayllari (.db/.sqlite) o'chadi. Davom etamizmi?", b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("cdo:"))
async def cb_clear_ok(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    was = runner.is_running(h["id"])
    await runner.stop(h["id"])
    n = runner.clear_db(db.bot_dir(h["id"]))
    note = f"🧹 {n} ta baza fayli o'chirildi."
    if was:
        ok, msg = await restart_host(h["id"])
        note += "\n" + ("✅ Bot qayta ishga tushdi." if ok else "⚠️ " + esc(msg))
    await show_panel(c, h["id"], note)
    await c.answer()


# ----------------------------- sozlamalar -----------------------------
def settings_view(h):
    text = (
        f"⚙️ <b>Sozlamalar</b>\n"
        f"Nom: {esc(h['name'])}\n"
        f"Bot: @{esc(h['username'])}\n"
        f"Asosiy fayl: <code>{esc(h['entry'])}</code>\n"
        f"Avtozapusk: {'✅ yoqiq' if h['autorun'] else '❌ o‘chiq'}"
    )
    i = h["id"]
    b = InlineKeyboardBuilder()
    b.row(btn("📝 Nomni o'zgartirish", f"sen:{i}"))
    b.row(btn("🔑 Tokenni almashtirish", f"set:{i}"))
    b.row(btn("🚀 Asosiy faylni tanlash", f"sx:{i}"))
    b.row(btn(f"🔁 Avtozapusk: {'✅' if h['autorun'] else '❌'}", f"sa:{i}"))
    b.row(btn("⬅️ Orqaga", f"p:{i}"))
    return text, b.as_markup()


@master.callback_query(F.data.startswith("se:"))
async def cb_settings(c: CallbackQuery, state: FSMContext):
    await state.clear()
    h = await get_owned(c)
    if not h:
        return
    text, kb = settings_view(h)
    await safe_edit(c.message, text, kb)
    await c.answer()


@master.callback_query(F.data.startswith("sa:"))
async def cb_autorun(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    db.update_host(h["id"], autorun=0 if h["autorun"] else 1)
    text, kb = settings_view(db.get_host(h["id"]))
    await safe_edit(c.message, text, kb)
    await c.answer()


@master.callback_query(F.data.startswith("sx:"))
async def cb_entry_list(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    py = runner.list_code(db.bot_dir(h["id"]))[:30]
    b = InlineKeyboardBuilder()
    for idx, f in enumerate(py):
        b.row(btn(("✅ " if f == h["entry"] else "") + f, f"sxp:{h['id']}:{idx}"))
    b.row(btn("⬅️ Orqaga", f"se:{h['id']}"))
    await safe_edit(c.message, "Qaysi fayl ishga tushirilsin?" if py else ".py / .php fayl yo'q.", b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("sxp:"))
async def cb_entry_pick(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    py = runner.list_code(db.bot_dir(h["id"]))[:30]
    try:
        entry = py[int(c.data.split(":")[2])]
    except (IndexError, ValueError):
        return await c.answer("Fayl topilmadi", show_alert=True)
    db.update_host(h["id"], entry=entry)
    was = runner.is_running(h["id"])
    note = f"🚀 Asosiy fayl: {esc(entry)}"
    if was:
        ok, msg = await restart_host(h["id"])
        note += "\n" + ("✅ Qayta ishga tushdi." if ok else "⚠️ " + esc(msg))
    await show_panel(c, h["id"], note)
    await c.answer()


@master.callback_query(F.data.startswith("sen:"))
async def cb_rename(c: CallbackQuery, state: FSMContext):
    h = await get_owned(c)
    if not h:
        return
    await state.set_state(Rename.name)
    await state.update_data(hid=h["id"])
    await c.message.answer("Yangi nomni yuboring (/cancel — bekor):")
    await c.answer()


@master.message(Rename.name)
async def got_name(m: Message, state: FSMContext):
    data = await state.get_data()
    h = db.get_host(data["hid"])
    if not h or h["owner_id"] != m.from_user.id:
        return await state.clear()
    db.update_host(h["id"], name=(m.text or "Bot").strip()[:64])
    await state.clear()
    await show_panel(m, h["id"], "✅ Nom o'zgartirildi.")


@master.callback_query(F.data.startswith("set:"))
async def cb_token(c: CallbackQuery, state: FSMContext):
    h = await get_owned(c)
    if not h:
        return
    await state.set_state(NewToken.token)
    await state.update_data(hid=h["id"])
    await c.message.answer("Yangi tokenni yuboring (/cancel — bekor):")
    await c.answer()


@master.message(NewToken.token)
async def got_token(m: Message, state: FSMContext):
    data = await state.get_data()
    h = db.get_host(data["hid"])
    if not h or h["owner_id"] != m.from_user.id:
        return await state.clear()
    token = (m.text or "").strip()
    try:
        tb = Bot(token)
        me = await tb.get_me()
        await tb.session.close()
    except Exception:
        return await m.answer("❌ Token ishlamayapti. Qayta yuboring yoki /cancel.")
    try:
        await m.delete()
    except Exception:
        pass
    await state.clear()
    db.update_host(h["id"], token=token, username=me.username)
    note = "✅ Token almashtirildi."
    if runner.is_running(h["id"]):
        ok, msg = await restart_host(h["id"])
        note += "\n" + ("✅ Bot qayta ishga tushdi." if ok else "⚠️ " + esc(msg))
    await show_panel(m, h["id"], note)


# ----------------------------- o'chirish -----------------------------
@master.callback_query(F.data.startswith("dl:"))
async def cb_delete_ask(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    b = InlineKeyboardBuilder()
    b.row(btn("✅ Ha, o'chirish", f"dlo:{h['id']}"), btn("⬅️ Yo'q", f"p:{h['id']}"))
    await safe_edit(c.message, f"⚠️ <b>{esc(h['name'])}</b> va uning barcha fayllari o'chadi. Aniqmi?", b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("dlo:"))
async def cb_delete_ok(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    await runner.stop(h["id"])
    db.delete_host(h["id"])
    shutil.rmtree(db.bot_dir(h["id"]), ignore_errors=True)
    await safe_edit(c.message, "🗑 Bot o'chirildi.", main_menu())
    await c.answer()


# ----------------------------- zip yuklab olish -----------------------------
@master.callback_query(F.data.startswith("zp:"))
async def cb_zip(c: CallbackQuery):
    h = await get_owned(c)
    if not h:
        return
    d = db.bot_dir(h["id"])
    if not os.path.isdir(d) or not runner.list_files(d):
        return await c.answer("Fayllar yo'q", show_alert=True)
    await c.answer("Zip tayyorlanmoqda...")
    out = os.path.join(tmp_dir(), f"bot_{h['id']}_{int(time.time())}.zip")
    try:
        await asyncio.to_thread(runner.make_zip, d, out)
        await c.message.answer_document(FSInputFile(out, filename=f"{safe_name(h['name'])}.zip"))
    except Exception as e:
        await c.message.answer(f"❌ {esc(e)}")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


# ----------------------------- yangiliklar -----------------------------
NEWS_MAX = 700


def fmt_news(n):
    day = time.strftime("%d.%m.%Y", time.localtime(n["created"]))
    return f"📰 <b>#{n['id']}</b> · {day}\n{esc(n['text'])}"


def news_view(uid):
    parts, total = [], 0
    for n in db.list_news(5):
        item = fmt_news(n)
        if total + len(item) > 3400:
            break
        parts.append(item)
        total += len(item)
    text = "📰 <b>Yangiliklar</b>\n\n" + ("\n\n".join(parts) if parts else "Hozircha yangilik yo'q.")
    b = InlineKeyboardBuilder()
    if uid in ADMINS:
        b.row(btn("➕ Yangilik qo'shish", "nadd"), btn("🗑 O'chirish", "ndel"))
    b.row(btn("⬅️ Menyu", "home"))
    return text, b.as_markup()


@master.callback_query(F.data == "news")
async def cb_news(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = news_view(c.from_user.id)
    db.mark_news_seen(c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer()


@master.callback_query(F.data == "nadd")
async def cb_news_add(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMINS:
        return await c.answer("Faqat admin uchun", show_alert=True)
    await state.set_state(News.text)
    await c.message.answer(
        f"📰 Yangilik matnini yuboring (maks. {NEWS_MAX} belgi). U barcha foydalanuvchilarga yuboriladi.\n"
        "Bekor: /cancel"
    )
    await c.answer()


@master.message(News.text, F.text)
async def news_got(m: Message, state: FSMContext, bot: Bot):
    if m.from_user.id not in ADMINS:
        return await state.clear()
    text = m.text.strip()
    if len(text) > NEWS_MAX:
        return await m.answer(f"❌ Matn juda uzun ({len(text)}/{NEWS_MAX}). Qisqartirib yuboring.")
    nid = db.add_news(m.from_user.id, text)
    await state.clear()
    db.mark_news_seen(m.from_user.id)
    sent = 0
    for uid in sorted(ALLOWED - {m.from_user.id}):
        try:
            await bot.send_message(uid, "🆕 <b>Yangilik</b>\n\n" + esc(text))
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Yangilik #{nid} qo'shildi. {sent} ta foydalanuvchiga yuborildi.",
                   reply_markup=main_menu(m.from_user.id))


@master.message(News.text)
async def news_wrong(m: Message):
    await m.answer("Iltimos, yangilik matnini yozib yuboring (yoki /cancel).")


@master.callback_query(F.data == "ndel")
async def cb_news_del(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer("Faqat admin uchun", show_alert=True)
    b = InlineKeyboardBuilder()
    items = db.list_news(10)
    for n in items:
        b.row(btn(f"🗑 #{n['id']} {n['text'][:30]}", f"nd:{n['id']}"))
    b.row(btn("⬅️ Orqaga", "news"))
    await safe_edit(c.message, "Qaysi yangilik o'chirilsin?" if items else "Yangilik yo'q.", b.as_markup())
    await c.answer()


@master.callback_query(F.data.startswith("nd:"))
async def cb_news_del_ok(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer("Faqat admin uchun", show_alert=True)
    try:
        db.delete_news(int(c.data.split(":")[1]))
    except ValueError:
        return await c.answer()
    text, kb = news_view(c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer("O'chirildi")


# ----------------------------- AI (DeepSeek) -----------------------------
AI_BUSY: set = set()
AI_LAST: dict = {}

AI_INTRO = (
    "🧠 <b>AI bilan kod yozish</b> (DeepSeek)\n"
    "Qanday bot kerakligini tavsiflab yozasiz — AI tayyor kod faylini yuboradi.\n\nQaysi tilda yozamiz?"
)
AI_OFF = (
    "⚠️ AI ulanmagan.\nGitHub repo: Settings → Secrets and variables → Actions → "
    "<code>API_KEY</code> (DeepSeek API kaliti) qo'shing va workflow'ni qayta ishga tushiring."
)


def ai_lang_kb():
    b = InlineKeyboardBuilder()
    b.row(btn(ai.LANGS["py"]["title"], "ail:py"), btn(ai.LANGS["php"]["title"], "ail:php"))
    b.row(btn("⬅️ Menyu", "home"))
    return b.as_markup()


def ai_result_kb(hid):
    b = InlineKeyboardBuilder()
    if hid:
        b.row(btn("✅ Shu botga qo'llash va restart", "aiy"))
    else:
        b.row(btn("➕ Yangi bot sifatida ishga tushirish", "aia"))
    b.row(btn("✏️ O'zgartirish kiritish", "aix"))
    b.row(btn("⬅️ Panel", f"p:{hid}") if hid else btn("🏠 Menyu", "home"))
    return b.as_markup()


@master.callback_query(F.data == "ai")
async def cb_ai(c: CallbackQuery, state: FSMContext):
    await state.clear()
    if not ai.enabled():
        await c.message.answer(AI_OFF)
        return await c.answer()
    await safe_edit(c.message, AI_INTRO, ai_lang_kb())
    await c.answer()


@master.callback_query(F.data.startswith("ail:"))
async def cb_ai_lang(c: CallbackQuery, state: FSMContext):
    lang = c.data.split(":")[1]
    if lang not in ai.LANGS:
        return await c.answer()
    if not ai.enabled():
        await c.message.answer(AI_OFF)
        return await c.answer()
    await state.clear()
    await state.set_state(AI.prompt)
    await state.update_data(lang=lang, hid=None, history=[])
    await c.message.answer(
        f"{ai.LANGS[lang]['title']} tanlandi.\n\nQanday bot kerak? Tavsiflab yozing, masalan:\n"
        "<i>«Ob-havo boti: /weather &lt;shahar&gt; buyrug'i, foydalanuvchilar SQLite'da saqlansin, "
        "admin /stats bilan statistikani ko'rsin»</i>\nBekor: /cancel"
    )
    await c.answer()


async def ai_run(m: Message, state: FSMContext, lang, history, hid=None):
    """AI'dan kod oladi, faylni yuboradi va tugmalarni ko'rsatadi."""
    uid = m.from_user.id
    if uid in AI_BUSY:
        return await m.answer("⏳ Oldingi so'rov hali bajarilmoqda, kuting...")
    left = AI_COOLDOWN - (time.time() - AI_LAST.get(uid, 0))
    if left > 0:
        return await m.answer(f"⏳ {int(left) + 1} soniyadan so'ng qayta yuboring.")
    AI_BUSY.add(uid)
    wait = await m.answer("🧠 AI kod yozmoqda... (odatda 20–90 soniya)")
    try:
        res = await ai.generate(lang, history)
    except ai.AIError as e:
        err = f"❌ {esc(e)}"
    except Exception:
        log.exception("AI kutilmagan xato")
        err = "❌ AI bilan ishlashda kutilmagan xato. Qayta urinib ko'ring."
    else:
        err = None
    finally:
        AI_BUSY.discard(uid)
        AI_LAST[uid] = time.time()
    try:
        await wait.delete()
    except Exception:
        pass
    if err:
        return await m.answer(err + "\nTavsifni qayta yuborishingiz yoki /cancel qilishingiz mumkin.")
    await state.set_state(AI.result)
    await state.update_data(lang=lang, hid=hid, history=res.history, code=res.code,
                            reqs=res.reqs, fname=res.filename)
    await m.answer_document(BufferedInputFile(res.code.encode("utf-8"), filename=res.filename))
    parts = ["✅ <b>Kod tayyor</b>"]
    if res.note:
        parts.append(esc(res.note[:500]))
    if res.reqs:
        parts.append("📦 requirements: <code>" + esc(", ".join(res.reqs)[:150]) + "</code>")
    if res.warning:
        parts.append("⚠️ " + esc(res.warning[:200]))
    await m.answer("\n\n".join(parts), reply_markup=ai_result_kb(hid))


@master.message(AI.prompt, F.text)
async def ai_prompt(m: Message, state: FSMContext):
    text = m.text.strip()
    if len(text) > ai.MAX_PROMPT:
        return await m.answer(f"❌ Tavsif juda uzun ({len(text)}/{ai.MAX_PROMPT}). Qisqartiring.")
    data = await state.get_data()
    await ai_run(m, state, data.get("lang", "py"), [{"role": "user", "content": text}])


@master.callback_query(AI.result, F.data == "aix")
async def ai_refine_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(AI.refine)
    await c.message.answer("✏️ Nimani o'zgartirish yoki qo'shish kerak? Yozing.\nBekor: /cancel")
    await c.answer()


@master.message(AI.refine, F.text)
async def ai_refine(m: Message, state: FSMContext):
    text = m.text.strip()
    if len(text) > ai.MAX_PROMPT:
        return await m.answer(f"❌ Matn juda uzun ({len(text)}/{ai.MAX_PROMPT}). Qisqartiring.")
    data = await state.get_data()
    history = list(data.get("history") or []) + [{"role": "user", "content": text}]
    await ai_run(m, state, data.get("lang", "py"), history, hid=data.get("hid"))


@master.callback_query(AI.result, F.data == "aia")
async def ai_new_bot(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("code"):
        return await c.answer("Sessiya tugagan, qaytadan boshlang", show_alert=True)
    if len(db.user_hosts(c.from_user.id)) >= MAX_BOTS:
        return await c.answer(f"Maksimum {MAX_BOTS} ta bot", show_alert=True)
    path = os.path.join(tmp_dir(), f"ai_{c.from_user.id}_{int(time.time())}_{data['fname']}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data["code"])
    await state.clear()
    await state.set_state(Add.token)
    await state.update_data(ai_tmp=path, ai_name=data["fname"], ai_reqs=data.get("reqs") or [])
    await c.message.answer(
        "1️⃣ Bot <b>tokenini</b> yuboring (@BotFather'dan) — AI yozgan kod shu bot bilan ishga tushadi.\n"
        "Bekor qilish: /cancel"
    )
    await c.answer()


@master.callback_query(F.data.startswith("aif:"))
async def cb_ai_fix(c: CallbackQuery, state: FSMContext):
    h = await get_owned(c)
    if not h:
        return
    if not ai.enabled():
        await c.message.answer(AI_OFF)
        return await c.answer()
    if not h["entry"]:
        return await c.answer("Asosiy fayl tanlanmagan", show_alert=True)
    await state.clear()
    await state.set_state(AI.fix)
    await state.update_data(hid=h["id"])
    await c.message.answer(
        "🛠 <b>AI bilan tuzatish</b>\nNimani tuzatish yoki qo'shish kerak? Yozing, yoki «-» yuboring — "
        "AI loglardagi xatoni o'zi topadi.\n"
        "ℹ️ Asosiy fayl kodi va oxirgi loglar DeepSeek'ga yuboriladi (bot tokenlari yashiriladi).\n"
        "Bekor: /cancel"
    )
    await c.answer()


@master.message(AI.fix, F.text)
async def ai_fix_run(m: Message, state: FSMContext):
    data = await state.get_data()
    h = db.get_host(data.get("hid") or 0)
    if not h or h["owner_id"] != m.from_user.id or not h["entry"]:
        await state.clear()
        return await m.answer("Bot topilmadi.", reply_markup=main_menu(m.from_user.id))
    try:
        path = runner.safe_join(db.bot_dir(h["id"]), h["entry"])
        if os.path.getsize(path) > ai.MAX_CODE_CTX:
            return await m.answer("❌ Asosiy fayl AI uchun juda katta (60 000 belgidan oshadi).")
        with open(path, encoding="utf-8", errors="replace") as fh:
            code = fh.read()
    except (OSError, ValueError):
        return await m.answer("❌ Asosiy fayl o'qib bo'lmadi.")
    text = m.text.strip()
    if len(text) > ai.MAX_PROMPT:
        return await m.answer(f"❌ Matn juda uzun ({len(text)}/{ai.MAX_PROMPT}). Qisqartiring.")
    first = ai.fix_request(h["entry"], code, runner.tail_log(h["id"], 3000),
                           "" if text == "-" else text, (h["token"],))
    await ai_run(m, state, runner.lang_of(h["entry"]), [{"role": "user", "content": first}], hid=h["id"])


@master.message(AI.prompt)
@master.message(AI.refine)
@master.message(AI.fix)
async def ai_text_only(m: Message):
    await m.answer("Iltimos, matn ko'rinishida yozing (yoki /cancel).")


@master.callback_query(AI.result, F.data == "aiy")
async def ai_apply(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    h = db.get_host(data.get("hid") or 0)
    if not h or h["owner_id"] != c.from_user.id or not data.get("code") or not h["entry"]:
        return await c.answer("Sessiya tugagan, qaytadan boshlang", show_alert=True)
    d = db.bot_dir(h["id"])
    try:
        target = runner.safe_join(d, h["entry"])
        if os.path.isfile(target):
            shutil.copy(target, target + ".bak")  # eski nusxa saqlanadi
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(data["code"])
        reqs = data.get("reqs") or []
        if reqs and runner.lang_of(h["entry"]) == "py":
            rp = os.path.join(d, "requirements.txt")
            old = open(rp, encoding="utf-8").read() if os.path.isfile(rp) else ""
            with open(rp, "w", encoding="utf-8") as fh:
                fh.write(ai.merge_requirements(old, reqs))
    except Exception as e:
        return await safe_edit(c.message, f"❌ {esc(e)}", back_kb(h["id"]))
    await state.clear()
    await c.answer("Qo'llanmoqda...")
    await safe_edit(c.message, "⏳ Qayta ishga tushirilmoqda...")
    ok, msg = await restart_host(h["id"])
    await show_panel(c, h["id"], ("✅ AI kodi qo'llandi (eski nusxa: " + esc(h["entry"]) + ".bak)."
                                  if ok else "⚠️ " + esc(msg)))


@master.callback_query(F.data.in_({"aia", "aix", "aiy"}))
async def ai_stale(c: CallbackQuery):
    await c.answer("Sessiya tugagan, qaytadan boshlang", show_alert=True)


# ----------------------------- ishga tushirish -----------------------------
def _protect_process():
    """Boshqa jarayonlar (hosting qilinayotgan botlar) /proc orqali tokenni o'qiy olmasin."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE=0
    except Exception:
        pass


async def stopper(dp, minutes):
    await asyncio.sleep(minutes * 60)
    log.info("Vaqt limiti tugadi, to'xtatilmoqda...")
    await dp.stop_polling()


async def main():
    if not MASTER_TOKEN:
        raise SystemExit("MASTER_BOT_TOKEN o'rnatilmagan!")
    if not ALLOWED:
        log.warning("ALLOWED_USERS bo'sh: faqat /id ishlaydi. O'z ID'ingizni qo'shing.")
    _protect_process()
    db.init()
    log.info("AI: %s | PHP: %s", "yoqilgan (%s)" % ai.model() if ai.enabled() else "o'chiq (API_KEY yo'q)",
             runner.php_version() or "yo'q")
    bot = Bot(MASTER_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await bot.set_my_commands([
            BotCommand(command="menu", description="Bosh menyu"),
            BotCommand(command="ai", description="AI bilan kod yozish"),
            BotCommand(command="news", description="Yangiliklar"),
            BotCommand(command="id", description="Mening ID'im"),
            BotCommand(command="cancel", description="Bekor qilish"),
        ])
    except Exception:
        pass
    dp = Dispatcher()
    dp.include_router(master)

    async def notify(host, text):
        try:
            await bot.send_message(host["owner_id"], f"🤖 <b>{esc(host['name'])}</b>: {esc(text)}")
        except Exception:
            pass

    async def autostart(h):
        ok, _ = await runner.start(h)
        if not ok:
            db.update_host(h["id"], want_running=0)

    todo = [h for h in db.all_hosts() if h["autorun"] and h["want_running"]]
    await asyncio.gather(*(autostart(h) for h in todo))
    log.info("%d ta bot avtomatik ishga tushirildi", len(runner.PROCS))

    wd = asyncio.create_task(runner.watchdog(notify))
    if MAX_RUNTIME_MIN > 0:
        asyncio.create_task(stopper(dp, MAX_RUNTIME_MIN))
    try:
        await dp.start_polling(bot)
    finally:
        wd.cancel()
        await runner.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
