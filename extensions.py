"""v2 inline panels. All privileged callbacks AND state messages recheck admin IDs."""
import asyncio
import html
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from aiogram import F
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

import ai
import aiex
import backup
import bot_secrets
import db
import limits
import runner


class Input(StatesGroup):
    text = State()
    restore = State()
    restore_confirm = State()


def esc(text):
    return html.escape(str(text))


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=data) for label, data in row
    ] for row in rows])


def home():
    return kb([('🏠 Menyu / Bekor qilish', 'home')])


def admin_kb():
    return kb(
        [('📢 Baza / zaxira kanali', 'v2:channel'), ('🔎 Kanalni tekshirish', 'v2:channelcheck')],
        [('💾 Hozir zaxiralash', 'v2:backup'), ('♻️ Zaxiradan tiklash', 'v2:backups')],
        [('📎 ZIP yuklab tiklash', 'v2:restoreupload')],
        [('👥 Foydalanuvchilar', 'v2:users'), ('⚖️ Limit belgilash', 'v2:setlimits')],
        [('🎁 Promolar', 'v2:promos'), ('➕ Promo yaratish', 'v2:newpromo')],
        [('🔎 AI diagnostika', 'v2:diagnostic'), ('📰 Yangiliklar', 'news')],
        [('🏠 Menyu', 'home')],
    )


ADMIN_ACTIONS = {'admin', 'channel', 'channelcheck', 'backup', 'backups', 'restore',
                 'restoreyes', 'restoreupload', 'users', 'setlimits', 'promos',
                 'newpromo', 'promotoggle', 'revokepromo', 'diagnostic'}
ADMIN_INPUTS = {'channel', 'setlimits', 'newpromo'}
OWN_ACTIONS = {'secrets', 'secretadd', 'secretdel', 'res', 'explain', 'gitupdate', 'gitconfirm'}


def owned(uid, hid):
    try:
        host = db.get_host(int(hid))
    except (ValueError, TypeError):
        return None
    return host if host and host['owner_id'] == uid else None


async def edit(c, text, markup=None):
    from aiogram.exceptions import TelegramBadRequest
    try:
        await c.message.edit_text(text, reply_markup=markup or home())
    except TelegramBadRequest as e:
        if 'not modified' not in str(e):
            await c.message.answer(text, reply_markup=markup or home())


async def prompt(c, state, action, text, **data):
    await state.clear()
    await state.set_state(Input.text)
    await state.update_data(action=action, **data)
    await c.message.answer(text, reply_markup=home())


def clean_repo_tree(dest):
    files = {}
    for root, dirs, names in os.walk(dest, followlinks=False):
        for d in list(dirs):
            if (Path(root) / d).is_symlink():
                raise ValueError('GitHub loyihasida symlink bor.')
        dirs[:] = [d for d in dirs if d not in runner.SKIP_DIRS]
        for n in names:
            p = Path(root, n)
            rel = p.relative_to(dest).as_posix()
            if p.is_symlink() or ':' in rel:
                raise ValueError('GitHub loyihasida xavfli fayl bor.')
            if n in runner.SKIP_FILES:
                continue
            files[rel] = p
            if len(files) > runner.MAX_ZIP_FILES:
                raise ValueError('Repoda 300 dan ortiq fayl bor.')
    if sum(p.stat().st_size for p in files.values()) > runner.MAX_UNZIPPED:
        raise ValueError('Repo 60 MB dan oshdi.')
    return files


async def install_github(m, state, text):
    from main import finish_add
    parts = text.split()
    if not parts or len(parts) > 2 or not runner.normalize_repo(parts[0]):
        return await m.answer('❌ Masalan: https://github.com/user/repo yoki user/repo main')
    url = runner.normalize_repo(parts[0])
    branch = parts[1] if len(parts) == 2 else ''
    data = await state.get_data()
    if not data.get('token'):
        return await m.answer('Avval «Bot qo‘shish» orqali token kiriting.')
    await m.answer('⏳ GitHub loyihasi yuklanmoqda...')
    with tempfile.TemporaryDirectory() as td:
        dest = os.path.join(td, 'repo')
        ok, msg = await runner.git_clone(url, branch, dest)
        if not ok:
            return await m.answer('❌ ' + esc(msg))
        files = clean_repo_tree(Path(dest))
        if not runner.find_entry(dest):
            return await m.answer('❌ Repoda Python/PHP asosiy fayli topilmadi.')
        # Record installed source files so later updates can remove deleted source only.
        import zipfile
        fd, archive = tempfile.mkstemp(suffix='.zip')
        os.close(fd)
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for rel, path in files.items():
                z.write(path, rel)
            z.writestr('.host-source.json', json.dumps(list(files)))
        await state.update_data(git_repo=url, git_branch=branch)
        await finish_add(m, state, archive, 'github.zip')


async def update_github(host):
    """Stage source, stop, keep rollback, preserve runtime databases and untracked files."""
    hid = host['id']
    d = Path(db.bot_dir(hid))
    with tempfile.TemporaryDirectory() as td:
        clone = Path(td, 'repo')
        ok, msg = await runner.git_clone(host['git_repo'], host['git_branch'] or '', str(clone))
        if not ok:
            raise ValueError(msg)
        files = clean_repo_tree(clone)
        if not runner.find_entry(str(clone)):
            raise ValueError('Yangi repoda asosiy fayl yo‘q.')
        was = runner.is_running(hid)
        await runner.stop(hid)
        rollback = Path(td, 'old')
        shutil.copytree(d, rollback, symlinks=True)
        try:
            manifest = d / '.host-source.json'
            previous = json.loads(manifest.read_text()) if manifest.exists() else []
            for rel in set(previous) - set(files):
                target = Path(runner.safe_join(d, rel))
                if target.is_file() and target.suffix.lower() not in runner.DB_EXT:
                    target.unlink()
            for rel, src in files.items():
                target = Path(runner.safe_join(d, rel))
                # Never overwrite existing user DBs or .env on a code update.
                if target.exists() and (target.suffix.lower() in runner.DB_EXT or target.name == '.env'):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            manifest.write_text(json.dumps(list(files)))
            entry = host['entry'] if (d / (host['entry'] or '_')).is_file() else runner.find_entry(str(d))
            db.update_host(hid, entry=entry)
            if was:
                ok, msg = await runner.start(db.get_host(hid))
                if not ok:
                    raise ValueError(msg)
        except Exception:
            await runner.stop(hid)
            shutil.rmtree(d)
            shutil.copytree(rollback, d, symlinks=True)
            db.update_host(hid, entry=host['entry'])
            if was:
                await runner.start(db.get_host(hid))
            raise


async def callback(c, state, bot):
    bits = c.data.split(':')
    action = bits[1] if len(bits) > 1 else ''
    uid = c.from_user.id
    # Do not trust hidden buttons or forged callback data.
    if action in ADMIN_ACTIONS and not limits.is_admin(uid):
        return await c.answer('⛔ Faqat admin uchun', show_alert=True)
    h = None
    if action in OWN_ACTIONS:
        h = owned(uid, bits[2] if len(bits) > 2 else None)
        if not h:
            return await c.answer('⛔ Bot topilmadi yoki ruxsat yo‘q', show_alert=True)
    await c.answer()
    try:
        if action == 'admin':
            await state.clear()
            channel = backup.channel_id()
            text = (f'👑 <b>Admin paneli</b>\n👥 {len(db.known_user_ids())} foydalanuvchi · '
                    f'🤖 {db.count_hosts()} bot\n📢 Kanal: <code>{channel or "sozlanmagan"}</code>\n'
                    f'📊 Ishlayotgan: {len(runner.PROCS)}\n'
                    '⚠️ Faqat ishonchli kodni hosting qiling. Subprocess xavfsiz sandbox emas.')
            return await edit(c, text, admin_kb())
        if action == 'channel':
            return await prompt(c, state, action,
                '📢 Botni <b>private zaxira kanaliga admin</b> qiling. Xabar yuborish va tahrirlash/pin huquqini bering.\n'
                'Kanal ID sini (-100...) yoki @username yuboring. Bu kanalga maxfiy tokenli ZIP lar yuboriladi.')
        if action == 'channelcheck':
            if not backup.channel_id():
                raise ValueError('Avval zaxira kanalini qo‘shing.')
            chat = await backup.check_channel(bot, backup.channel_id())
            return await edit(c, f'✅ {esc(chat.title)}: bot administrator, kerakli huquqlar bor.', admin_kb())
        if action == 'backup':
            size = await backup.send_snapshot(bot, 'manual')
            return await edit(c, f'✅ Zaxira kanalga yuborildi va pin qilindi: {runner.human(size)}.', admin_kb())
        if action == 'backups':
            rows = []
            for b in db.list_backups(15):
                label = time.strftime('%m-%d %H:%M', time.gmtime(b['created']))
                rows.append([(f'♻️ #{b["id"]} {label} · {runner.human(b["size"])}', f'v2:restore:{b["id"]}')])
            rows += [[('📎 ZIP yuklab tiklash', 'v2:restoreupload')], [('⬅️ Admin', 'v2:admin')]]
            return await edit(c, '💾 Tiklash uchun nusxani tanlang. Amaldagi ma’lumotlar almashtiriladi.', kb(*rows))
        if action == 'restoreupload':
            await state.clear()
            await state.set_state(Input.restore)
            return await c.message.answer('📎 Host Bot v2 to‘liq zaxira ZIP faylini yuboring (19 MB gacha).', reply_markup=home())
        if action == 'restore':
            b = db.get_backup(int(bits[2]))
            if not b:
                raise ValueError('Zaxira topilmadi.')
            await state.clear()
            await state.set_state(Input.restore_confirm)
            await state.update_data(restore_file=b['path'])
            return await edit(c, '⚠️ Barcha botlar to‘xtatilib, bazalar va fayllar almashtiriladi. Davom etish',
                kb([('✅ Tiklash', 'v2:restoreyes')], [('❌ Bekor', 'v2:admin')]))
        if action == 'restoreyes':
            data = await state.get_data()
            if await state.get_state() != Input.restore_confirm.state or not data.get('restore_file'):
                raise ValueError('Tasdiqlash sessiyasi tugagan.')
            await state.clear()
            await backup.restore_telegram(bot, data['restore_file'])
            return await edit(c, '✅ Zaxira tiklandi. Oldingi nusxa serverdagi .rollback papkasida saqlandi.', admin_kb())
        if action == 'limits':
            await state.clear()
            botlim, ailim = limits.bot_limit(uid), limits.ai_limit(uid)
            return await edit(c,
                f'📊 <b>Limitlarim</b>\nID: <code>{uid}</code>\n'
                f'🤖 Botlar: {db.count_hosts(uid)}/{botlim or "∞"}\n'
                f'🧠 AI bugun (UTC): {db.ai_used(uid)}/{ailim or "∞"}\n'
                f'{esc(limits.promo_status(uid))}\n'
                '∞ — dastur kvotasi yo‘q. Telegram/API, fizik RAM/CPU va xavfsizlik chegaralari saqlanadi.',
                kb([('🎁 Promo', 'v2:promo')], [('🏠 Menyu', 'home')]))
        if action == 'promo':
            return await prompt(c, state, action, '🎁 Promo kodni yuboring. Kod kunlik AI va botlar soni kvotasini ochadi.')
        if action == 'users':
            rows = []
            for u in db.all_users()[:40]:
                rows.append([(f'👤 {u["user_id"]} · {db.count_hosts(u["user_id"])} bot · promo bekor', f'v2:revokepromo:{u["user_id"]}')])
            rows += [[('⚖️ Limit belgilash', 'v2:setlimits')], [('⬅️ Admin', 'v2:admin')]]
            return await edit(c, '👥 Oxirgi 40 foydalanuvchi. Tugma foydalanuvchining faol promosini bekor qiladi.', kb(*rows))
        if action == 'setlimits':
            return await prompt(c, state, action, '⚖️ Format: <code>USER_ID BOT_SONI AI_KUNLIK</code>\nMasalan: <code>123456789 5 30</code>\n0 — standart limit. Admin va faol promo kvotadan ozod.')
        if action == 'revokepromo':
            limits.revoke_promo(int(bits[2]))
            return await edit(c, '✅ Promo huquqi bekor qilindi.', admin_kb())
        if action == 'promos':
            rows = []
            promos = db.list_promos()[:30]
            for p in promos:
                rows.append([(f'{"✅" if p["active"] else "❌"} {p["code"]} · {p["uses"]}', f'v2:promotoggle:{p["code"]}')])
            rows += [[('➕ Promo yaratish', 'v2:newpromo')], [('⬅️ Admin', 'v2:admin')]]
            return await edit(c, '🎁 Tugma promo kodni yoqadi/o‘chiradi. Avval berilgan huquqlar avtomatik bekor bo‘lmaydi.', kb(*rows))
        if action == 'promotoggle':
            code = bits[2]
            p = db.get_promo(code)
            if not p:
                raise ValueError('Promo topilmadi.')
            db.set_promo_active(code, not p['active'])
            return await edit(c, '✅ Promo holati o‘zgardi.', admin_kb())
        if action == 'newpromo':
            return await prompt(c, state, action, '🎁 Format: <code>KOD KUN ISHLATISH_SONI</code>\nMasalan: <code>summer 30 100</code>\n0 kun — doimiy; 0 ishlatish — cheksiz. Kod 32 belgigacha.')
        if action == 'diagnostic':
            from main import m_aicheck
            return await m_aicheck(c.message, state)
        if action == 'models':
            rows = [[(('✅ ' if model == aiex.selected(uid) else '') + model, f'v2:model:{i}')]
                    for i, model in enumerate(aiex.models())]
            return await edit(c, '🧠 Bir xil AI_BASE_URL/API_KEY xizmatidagi modellar. Provayder modelni qo‘llashi kerak.',
                              kb(*rows, [('⬅️ AI', 'ai')]))
        if action == 'model':
            model = aiex.models()[int(bits[2])]
            db.kv_set(f'ai_model:{uid}', model)
            return await edit(c, f'✅ Model: <code>{esc(model)}</code>', kb([('🧠 AI menyu', 'ai')]))
        if action == 'project':
            if not ai.enabled():
                raise ValueError('API_KEY sozlanmagan.')
            from main import AI
            await state.clear()
            await state.set_state(AI.prompt)
            await state.update_data(multi=True, lang='py', hid=None, history=[])
            return await c.message.answer('🗂 Ko‘p fayllik loyiha tavsifini yozing (backend Python/PHP bo‘lishi mumkin).\n'
                'Mini App uchun index.html + server yaratiladi. GitHub Actions doimiy ochiq HTTPS manzil bermaydi; '
                'Mini Appni ishlatish uchun tashqi HTTPS domen/hosting kerak.', reply_markup=home())
        if action == 'secrets':
            rows = [[('➕ Kalit qo‘shish / yangilash', f'v2:secretadd:{h["id"]}')]]
            text = '🔐 <b>Maxfiy o‘zgaruvchilar</b>\nQiymatlar shifrlangan, ekranga chiqarilmaydi.\n'
            for i, (name, value) in enumerate(bot_secrets.items(h['id'], mask=True)):
                text += f'• <code>{esc(name)}</code>: ••••••••\n'
                rows.append([(f'🗑 {name}', f'v2:secretdel:{h["id"]}:{i}')])
            rows.append([('⬅️ Panel', f'p:{h["id"]}')])
            return await edit(c, text, kb(*rows))
        if action == 'secretadd':
            return await prompt(c, state, action, '🔐 <code>OPENAI_KEY=qiymat</code> ko‘rinishida yuboring. Xabar o‘chiriladi.\n'
                'Host kalitlari meros qilinmaydi; siz kiritgan kalit faqat shu botga beriladi. Saqlangach botni qayta ishga tushiring.', hid=h['id'])
        if action == 'secretdel':
            items = db.list_secrets(h['id'])
            item = items[int(bits[3])]
            bot_secrets.delete(h['id'], item['key'])
            return await edit(c, '✅ Kalit o‘chirildi. Botni qayta ishga tushiring.', kb([('🔐 Kalitlar', f'v2:secrets:{h["id"]}')]))
        if action == 'res':
            stats = runner.res_stats(h['id'])
            text = ('🔴 Bot ishlamayapti.' if not stats else
                    f'📊 <b>{esc(h["name"])}</b>\nRAM: {runner.human(stats["ram"])}\n'
                    f'CPU: {stats["cpu"]:.1f}%\nPID: {stats["pid"]} · oqimlar: {stats["threads"]}\n'
                    'CPU birinchi o‘lchashda 0 bo‘lishi mumkin; yangilang.')
            return await edit(c, text, kb([('🔄 Yangilash', f'v2:res:{h["id"]}')], [('⬅️ Panel', f'p:{h["id"]}')]))
        if action == 'explain':
            from main import AI_BUSY, AI_LAST, AI_COOLDOWN
            if not ai.enabled():
                raise ValueError('API_KEY sozlanmagan.')
            available, used, lim = limits.ai_left(uid)
            if not available:
                raise ValueError(f'Kunlik AI limiti tugadi: {used}/{lim}.')
            if uid in AI_BUSY or time.time() - AI_LAST.get(uid, 0) < AI_COOLDOWN:
                raise ValueError('Oldingi AI so‘rovi tugashini kuting.')
            if not h['entry']:
                raise ValueError('Asosiy fayl yo‘q.')
            path = runner.safe_join(db.bot_dir(h['id']), h['entry'])
            if os.path.getsize(path) > ai.MAX_CODE_CTX:
                raise ValueError('Asosiy fayl AI uchun juda katta.')
            text = bot_secrets.redact(Path(path).read_text(errors='replace'), h['id'])
            AI_BUSY.add(uid)
            limits.ai_consume(uid)
            token = aiex.SELECTED_MODEL.set(aiex.selected(uid))
            try:
                answer = await aiex.explain(text, h['entry'])
            finally:
                aiex.SELECTED_MODEL.reset(token)
                AI_BUSY.discard(uid)
                AI_LAST[uid] = time.time()
            # Chunk plain text safely after HTML escaping.
            for i in range(0, len(answer), 2800):
                await c.message.answer(esc(answer[i:i+2800]), reply_markup=kb([('⬅️ Panel', f'p:{h["id"]}')]))
            return
        if action == 'gitupdate':
            return await edit(c, '📦 GitHub kodi yangilanadi. Runtime bazalari saqlanadi, o‘zgartirilgan kod almashtiriladi.',
                kb([('✅ Yangilash', f'v2:gitconfirm:{h["id"]}')], [('❌ Bekor', f'p:{h["id"]}')]))
        if action == 'gitconfirm':
            if not h['git_repo']:
                raise ValueError('GitHub repo sozlanmagan.')
            await update_github(h)
            from main import show_panel
            return await show_panel(c, h['id'], '✅ GitHub kodi yangilandi.')
        raise ValueError('Tugma eskirgan yoki mavjud emas.')
    except (ValueError, IndexError, KeyError, OSError, ai.AIError) as e:
        await c.message.answer('❌ ' + esc(str(e)[:700]), reply_markup=home())
    except Exception:
        import logging
        logging.getLogger(__name__).exception('v2 callback failed')
        await c.message.answer('❌ Amal bajarilmadi. Kanal huquqi, tarmoq va sozlamalarni tekshiring.', reply_markup=home())


async def input_text(m, state, bot):
    data = await state.get_data()
    action = data.get('action')
    uid = m.from_user.id
    if action in ADMIN_INPUTS and not limits.is_admin(uid):
        await state.clear()
        return await m.answer('⛔ Faqat admin uchun', reply_markup=home())
    text = m.text or ''
    try:
        if action == 'channel':
            channel = int(text.strip()) if re.fullmatch(r'-?\d+', text.strip()) else text.strip()
            chat = await backup.check_channel(bot, channel)
            db.set_backup_channel(chat.id, chat.title)
            await state.clear()
            return await m.answer(f'✅ Kanal saqlandi: <code>{chat.id}</code>.\n'
                'Kesh butunlay yo‘qolganda ham tiklash uchun shu ID ni GitHub Secret '
                '<code>BACKUP_CHANNEL_ID</code> ga saqlang. Oxirgi zaxira xabarini pin holatida qoldiring.', reply_markup=admin_kb())
        if action == 'promo':
            ok, msg = limits.apply_promo(uid, text)
            if ok:
                await state.clear()
            return await m.answer(esc(msg), reply_markup=home())
        if action == 'setlimits':
            parts = text.split()
            if len(parts) != 3:
                raise ValueError('Format: USER_ID BOT_SONI AI_KUNLIK')
            target, bots, daily = map(int, parts)
            if target <= 0 or not 0 <= bots <= 100000 or not 0 <= daily <= 100000:
                raise ValueError('Qiymatlar noto‘g‘ri.')
            db.set_user(target, max_bots=bots, ai_daily=daily)
            await state.clear()
            return await m.answer('✅ Foydalanuvchi limitlari saqlandi.', reply_markup=admin_kb())
        if action == 'newpromo':
            code, days, uses = text.lower().split()
            days, uses = int(days), int(uses)
            if not re.fullmatch(r'[a-z0-9_-]{2,32}', code) or not 0 <= days <= 36500 or uses < 0:
                raise ValueError('Kod yoki sonlar noto‘g‘ri.')
            if db.get_promo(code):
                raise ValueError('Kod mavjud. Boshqa kod yozing.')
            db.add_promo(code, days, uses, uid)
            await state.clear()
            return await m.answer('✅ Promo yaratildi.', reply_markup=admin_kb())
        if action == 'secretadd':
            h = owned(uid, data.get('hid'))
            if not h:
                raise ValueError('Ruxsat yo‘q.')
            try:
                await m.delete()
            except Exception:
                pass
            if '=' not in text:
                raise ValueError('Format: OPENAI_KEY=qiymat')
            name, value = text.split('=', 1)
            name = bot_secrets.set_value(h['id'], name, value.strip())
            await state.clear()
            return await m.answer(f'✅ <code>{esc(name)}</code> shifrlanib saqlandi. Restart qiling.',
                reply_markup=kb([('🔄 Restart', f'rs:{h["id"]}'), ('🔐 Kalitlar', f'v2:secrets:{h["id"]}')]))
        raise ValueError('Sessiya tugagan. Menyuga qayting.')
    except (ValueError, OSError) as e:
        return await m.answer('❌ ' + esc(str(e)), reply_markup=home())
    except Exception:
        return await m.answer('❌ Bajarilmadi. Kanal huquqi va sozlamalarni tekshiring.', reply_markup=home())


async def restore_upload(m, state):
    if not limits.is_admin(m.from_user.id):
        await state.clear()
        return await m.answer('⛔ Faqat admin uchun', reply_markup=home())
    doc = m.document
    if not doc or not (doc.file_name or '').lower().endswith('.zip') or (doc.file_size or 0) > backup.MAX_ARCHIVE:
        return await m.answer('❌ To‘liq v2 zaxira ZIP faylini yuboring (19 MB gacha).', reply_markup=home())
    await state.set_state(Input.restore_confirm)
    await state.update_data(restore_file=doc.file_id)
    await m.answer('⚠️ Hozirgi baza va barcha bot fayllari almashtiriladi. Ishonchli zaxira ekanini tekshiring.',
                   reply_markup=kb([('✅ Tiklash', 'v2:restoreyes')], [('❌ Bekor', 'v2:admin')]))


def register(router):
    router.callback_query.register(callback, F.data.startswith('v2:'))
    router.message.register(input_text, Input.text)
    router.message.register(restore_upload, Input.restore)

    async def fallback(m):
        from main import main_menu
        await m.answer('🏠 Kerakli amalni tugmalardan tanlang.', reply_markup=main_menu(m.from_user.id))
    router.message.register(fallback)
