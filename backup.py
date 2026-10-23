"""Telegram-backed snapshots, validated staged restore, daily and shutdown delivery.

Only private administrator-controlled channels: archive contains hosting tokens.
The pinned document is the durable discovery pointer when Actions cache is lost.
"""
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

import db
import runner

log = logging.getLogger(__name__)
LOCK = asyncio.Lock()
MARKER = 'HOSTBOT_BACKUP_V2'
# Public Telegram Bot API downloads are limited to 20 MB. Keep snapshots restorable.
MAX_ARCHIVE = 19 * 1024 * 1024
MAX_EXPANDED = 200 * 1024 * 1024
MAX_FILES = 10000


def channel_id():
    return db.backup_channel() or int(os.getenv('BACKUP_CHANNEL_ID') or 0) or None


async def check_channel(bot, channel):
    chat = await bot.get_chat(channel)
    if str(chat.type) not in ('channel', 'ChatType.CHANNEL'):
        raise ValueError('Baza uchun Telegram kanalini yuboring.')
    me = await bot.get_me()
    member = await bot.get_chat_member(chat.id, me.id)
    if str(member.status) not in ('administrator', 'creator', 'ChatMemberStatus.ADMINISTRATOR', 'ChatMemberStatus.CREATOR'):
        raise ValueError('Bot kanalda administrator emas.')
    if hasattr(member, 'can_post_messages') and not member.can_post_messages:
        raise ValueError('Botga kanalda xabar yuborish huquqini bering.')
    # Channel pin uses edit-messages permission, not group can_pin_messages.
    if hasattr(member, 'can_edit_messages') and not member.can_edit_messages:
        raise ValueError('Avtomatik tiklash uchun botga xabarlarni tahrirlash/pin qilish huquqini bering.')
    return chat


def sqlite_copy(src, dst):
    # SQLite backup API includes committed WAL data without dropping -wal content.
    source = sqlite3.connect(f'file:{Path(src).resolve().as_posix()}?mode=ro', uri=True, timeout=20)
    target = sqlite3.connect(dst)
    try:
        source.backup(target, pages=256, sleep=0.05)
    finally:
        target.close()
        source.close()


def make_snapshot(out):
    root = Path(db.DATA_DIR).resolve()
    with tempfile.TemporaryDirectory(prefix='host-snapshot-') as td:
        stage = Path(td)
        count = 0
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in runner.SKIP_DIRS | {'tmp', 'backups', 'rollback'}
                       and not (Path(current) / d).is_symlink()]
            for name in files:
                src = Path(current) / name
                if src.is_symlink() or name in runner.SKIP_FILES or name.endswith(('-wal', '-shm', '-journal')):
                    continue
                dest = stage / src.relative_to(root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.suffix.lower() in runner.DB_EXT:
                    sqlite_copy(src, dest)
                else:
                    shutil.copy2(src, dest)
                count += 1
                if count > MAX_FILES:
                    raise ValueError('Zaxirada fayllar juda ko‘p.')
        (stage / 'backup_manifest.json').write_text(json.dumps({
            'format': MARKER, 'created': time.time(), 'files': count,
        }), encoding='utf-8')
        if sum(p.stat().st_size for p in stage.rglob('*') if p.is_file()) > MAX_EXPANDED:
            raise ValueError('Zaxira ochilganda 200 MB dan oshadi.')
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in stage.rglob('*'):
                if p.is_file():
                    z.write(p, p.relative_to(stage).as_posix())
    size = os.path.getsize(out)
    if size > MAX_ARCHIVE:
        os.remove(out)
        raise ValueError('Zaxira 19 MB dan katta. Telegramdan avtomatik yuklab tiklash uchun hajmni kamaytiring.')
    return size


def validate_restore(archive, dest):
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError('Arxiv hajmi/fayllar soni chegaradan oshdi.')
        seen = set()
        for i in infos:
            rel = i.filename.replace('\\', '/')
            if rel in seen or rel.startswith('/') or '..' in rel.split('/') or ':' in rel or '\x00' in rel:
                raise ValueError('Xavfli yoki takrorlangan arxiv yo‘li.')
            seen.add(rel)
            if ((i.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError('Symlink taqiqlangan.')
            if i.is_dir():
                continue
            target = runner.safe_join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(i) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out)
    manifest = Path(dest, 'backup_manifest.json')
    if not manifest.is_file() or json.loads(manifest.read_text())['format'] != MARKER:
        raise ValueError('Bu Host Bot v2 to‘liq zaxirasi emas.')
    path = Path(dest, 'hosting.db')
    if not path.is_file():
        raise ValueError('hosting.db topilmadi.')
    with sqlite3.connect(path) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('hosting.db buzilgan.')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'hosts', 'users', 'kv', 'secrets'} <= tables:
            raise ValueError('Zaxira sxemasi noto‘g‘ri.')
        for r in c.execute('SELECT id, entry FROM hosts'):
            if r[1] and not os.path.isfile(runner.safe_join(os.path.join(dest, 'bots', str(r[0])), r[1])):
                raise ValueError(f'Bot #{r[0]} asosiy fayli yo‘q.')
        import bot_secrets
        for (value,) in c.execute('SELECT value FROM secrets'):
            if bot_secrets.decrypt(value) is None:
                raise ValueError('Zaxira maxfiy kalitlari ochilmadi. Avvalgi SECRET_KEY ni sozlang.')


def restore_snapshot(archive):
    """Caller must hold LOCK and stop children first. Old tree retained as sibling."""
    root = Path(db.DATA_DIR).resolve()
    parent = root.parent
    with tempfile.TemporaryDirectory(prefix='host-restore-', dir=parent) as td:
        validate_restore(archive, td)
        old = Path(str(root) + '.rollback')
        if old.exists():
            shutil.rmtree(old)
        if root.exists():
            os.replace(root, old)
        try:
            os.replace(td, root)
            db.init()
        except Exception:
            if root.exists():
                shutil.rmtree(root)
            if old.exists():
                os.replace(old, root)
            raise
    return str(root) + '.rollback'


async def send_snapshot(bot, kind='manual'):
    from aiogram.types import FSInputFile
    async with LOCK:
        channel = channel_id()
        if not channel:
            raise ValueError('Avval admin panelida zaxira kanalini qo‘shing.')
        await check_channel(bot, channel)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'host-backup.zip')
            size = await asyncio.to_thread(make_snapshot, path)
            msg = await bot.send_document(
                channel, FSInputFile(path, filename=f'host-backup-{time.strftime("%Y%m%d-%H%M%S", time.gmtime())}.zip'),
                caption=f'{MARKER}\n💾 To‘liq zaxira · {kind}\nUTC: {time.strftime("%Y-%m-%d %H:%M", time.gmtime())}\n'
                        '⚠️ Maxfiy: bot tokenlari va bazalar mavjud.',
            )
            # Pin success is part of success: otherwise cold discovery cannot work.
            await bot.pin_chat_message(channel, msg.message_id, disable_notification=True)
            db.add_backup(msg.document.file_id, size, kind, str(msg.message_id))
            db.kv_set('backup_last_at', str(time.time()))
            db.set_last_backup_day(db.today())
            db.prune_backups(30)
            return size


async def restore_telegram(bot, file_id):
    async with LOCK:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'restore.zip')
            await bot.download(file_id, destination=path)
            # Validate before interrupting running bots.
            stage = os.path.join(td, 'check')
            os.makedirs(stage)
            await asyncio.to_thread(validate_restore, path, stage)
            wanted = [h for h in db.all_hosts() if h['want_running']]
            await runner.stop_all()
            try:
                await asyncio.to_thread(restore_snapshot, path)
            except Exception:
                for h in wanted:
                    await runner.start(h)
                raise
            for h in db.all_hosts():
                if h['autorun'] and h['want_running']:
                    await runner.start(h)


async def cold_restore(bot):
    """Needs BACKUP_CHANNEL_ID independent of the lost local database."""
    channel = int(os.getenv('BACKUP_CHANNEL_ID') or 0)
    if not channel or db.all_hosts():
        return False
    chat = await check_channel(bot, channel)
    pinned = chat.pinned_message
    if not pinned:
        return False  # first ever setup, no backup exists yet
    if not pinned.document or not (pinned.caption or '').startswith(MARKER):
        raise ValueError('Kanaldagi pin Host Bot zaxirasi emas. Oxirgi zaxira ZIP xabarini pin qiling.')
    await restore_telegram(bot, pinned.document.file_id)
    db.add_backup(pinned.document.file_id, pinned.document.file_size or 0,
                  'recovered', str(pinned.message_id))
    return True


async def daily(bot, alert=None, interval=60):
    while True:
        try:
            if channel_id() and db.last_backup_day() != db.today():
                await send_snapshot(bot, 'daily')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning('Backup delivery failed: %s', type(e).__name__)
            if alert:
                await alert('⚠️ Kunlik zaxira yuborilmadi: ' + type(e).__name__ + '. Kanal huquqi va hajmini tekshiring.')
            await asyncio.sleep(1800)
        await asyncio.sleep(interval)
