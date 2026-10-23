"""Offline v2 integration/regression tests; no production tokens or network used."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

import ai
import aiex
import backup
import bot_secrets
import db
import extensions
import limits
import main
import runner


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.DATA_DIR
        db.DATA_DIR = str(Path(self.tmp.name, 'data'))
        self.env = patch.dict(os.environ, {'SECRET_KEY': 'offline-test-key', 'MASTER_BOT_TOKEN': 'test-master'})
        self.env.start()
        db.init()

    def tearDown(self):
        asyncio.run(runner.stop_all())
        runner.CRASHES.clear()
        db.DATA_DIR = self.old_dir
        self.env.stop()
        self.tmp.cleanup()

    def host(self, owner=701):
        hid = db.add_host(owner, f'123:{owner}TEST_TOKEN', 'Offline', 'offline_bot')
        d = Path(db.bot_dir(hid))
        d.mkdir(parents=True, exist_ok=True)
        (d/'main.py').write_text('import time\ntime.sleep(60)\n')
        db.update_host(hid, entry='main.py')
        return hid, d


class TestSecrets(Base):
    def test_authenticated_encryption_no_plaintext(self):
        hid, d = self.host()
        bot_secrets.set_value(hid, 'OPENAI_KEY', 'private-service-key')
        row = db.list_secrets(hid)[0]
        self.assertNotIn('private-service-key', row['value'])
        self.assertEqual(bot_secrets.env_for(hid)['OPENAI_KEY'], 'private-service-key')
        self.assertEqual(bot_secrets.items(hid, mask=True), [('OPENAI_KEY', '••••••••')])
        self.assertIsNone(bot_secrets.decrypt(row['value'][:-5] + 'abcde'))

    def test_wrong_key_fails_closed(self):
        hid, _ = self.host()
        bot_secrets.set_value(hid, 'OPENAI_KEY', 'service-key')
        with patch.dict(os.environ, {'SECRET_KEY': 'another-key'}):
            with self.assertRaises(ValueError):
                bot_secrets.env_for(hid)

    def test_reserved_and_nul_rejected(self):
        hid, _ = self.host()
        for key in ('MASTER_BOT_TOKEN', 'PATH', 'LD_PRELOAD', 'PYTHONPATH', '../X', 'SECRET_KEY'):
            with self.assertRaises(ValueError, msg=key):
                bot_secrets.set_value(hid, key, 'bad')
        with self.assertRaises(ValueError):
            bot_secrets.set_value(hid, 'OPENAI_KEY', 'a\x00b')

    def test_secret_injection_and_scope(self):
        hid, d = self.host()
        hid2, d2 = self.host(owner=702)
        with patch.dict(os.environ, {'API_KEY': 'master-api-key'}):
            bot_secrets.set_value(hid, 'API_KEY', 'child-explicit-key')
            bot_secrets.set_value(hid, 'OPENAI_KEY', 'child-openai-key')
            env = runner.child_env(db.get_host(hid), str(d))
            env2 = runner.child_env(db.get_host(hid2), str(d2))
            self.assertEqual(env['API_KEY'], 'child-explicit-key')
            self.assertNotIn('API_KEY', env2)
            self.assertNotIn('OPENAI_KEY', env2)
            self.assertNotIn('MASTER_BOT_TOKEN', env)
            self.assertNotIn('SECRET_KEY', env)

    def test_redaction_short_secret(self):
        hid, _ = self.host()
        bot_secrets.set_value(hid, 'PASS', 'xyza')
        out = bot_secrets.redact('password xyza and test-master', hid)
        self.assertNotIn('xyza', out)
        self.assertNotIn('test-master', out)

    def test_real_child_sees_own_secret(self):
        hid, d = self.host()
        bot_secrets.set_value(hid, 'OPENAI_KEY', 'child-secret')
        (d/'main.py').write_text("import os,time\nprint(os.getenv('OPENAI_KEY'), flush=True)\ntime.sleep(60)\n")
        async def go():
            ok, msg = await runner.start(db.get_host(hid))
            self.assertTrue(ok, msg)
            self.assertIn('child-secret', runner.tail_log(hid))
            await runner.stop(hid)
        asyncio.run(go())


class TestLimitsPromo(Base):
    def test_quotas_and_unlimited_promo(self):
        uid = 700001
        db.ensure_user(uid)
        db.set_user(uid, max_bots=1, ai_daily=1)
        self.host(uid)
        self.assertFalse(limits.can_add_bot(uid)[0])
        self.assertTrue(limits.ai_left(uid)[0])
        limits.ai_consume(uid)
        self.assertFalse(limits.ai_left(uid)[0])
        ok, msg = limits.apply_promo(uid, 'infinix-host-no-limit')
        self.assertTrue(ok, msg)
        self.assertEqual(limits.bot_limit(uid), 0)
        self.assertEqual(limits.ai_limit(uid), 0)
        self.assertTrue(limits.can_add_bot(uid)[0])
        self.assertTrue(limits.ai_left(uid)[0])
        self.assertFalse(limits.apply_promo(uid, 'infinix-host-no-limit')[0])
        self.assertEqual(db.get_promo('infinix-host-no-limit')['uses'], 1)

    def test_limited_code_and_expiry(self):
        db.add_promo('one-use', days=1, max_uses=1)
        self.assertTrue(limits.apply_promo(800001, 'one-use')[0])
        self.assertFalse(limits.apply_promo(800002, 'one-use')[0])
        db.set_user(800001, promo_until=time.time()-1)
        self.assertFalse(limits.promo_active(800001))
        db.set_promo_active('infinix-host-no-limit', False)
        self.assertFalse(limits.apply_promo(800003, 'infinix-host-no-limit')[0])

    def test_parallel_redemption_is_atomic(self):
        from concurrent.futures import ThreadPoolExecutor
        db.add_promo('last-code', days=1, max_uses=1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda uid: limits.apply_promo(uid, 'last-code')[0], range(810001,810005)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(db.get_promo('last-code')['uses'], 1)

    def test_utc_days_independent(self):
        db.ai_inc(888, '2001-01-01')
        self.assertEqual(db.ai_used(888, '2001-01-01'), 1)
        self.assertEqual(db.ai_used(888, '2001-01-02'), 0)


class TestBackups(Base):
    def test_sqlite_wal_snapshot_and_restore(self):
        hid, d = self.host()
        (d/'index.html').write_text('<h1>Mini App</h1>')
        (d/'libs').mkdir()
        (d/'libs'/'ignore.txt').write_text('not backed up')
        con = sqlite3.connect(d/'data.db')
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE items(value TEXT)')
        con.execute("INSERT INTO items VALUES('committed WAL')")
        con.commit()
        archive = str(Path(self.tmp.name, 'snapshot.zip'))
        try:
            size = backup.make_snapshot(archive)
            self.assertGreater(size, 0)
            with zipfile.ZipFile(archive) as z:
                self.assertNotIn(f'bots/{hid}/libs/ignore.txt', z.namelist())
                self.assertIn('hosting.db', z.namelist())
                stage = Path(self.tmp.name, 'validate')
                stage.mkdir()
                backup.validate_restore(archive, stage)
            with sqlite3.connect(stage/'bots'/str(hid)/'data.db') as c:
                self.assertEqual(c.execute('SELECT value FROM items').fetchone()[0], 'committed WAL')
        finally:
            con.close()
        db.update_host(hid, name='changed')
        rollback = backup.restore_snapshot(archive)
        self.assertEqual(db.get_host(hid)['name'], 'Offline')
        self.assertTrue(Path(rollback, 'hosting.db').is_file())
        self.assertEqual(Path(db.bot_dir(hid), 'index.html').read_text(), '<h1>Mini App</h1>')

    def test_invalid_restore_keeps_data(self):
        hid, _ = self.host()
        archive = Path(self.tmp.name, 'evil.zip')
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../escape.py', 'bad')
        with self.assertRaises(ValueError):
            backup.restore_snapshot(archive)
        self.assertEqual(db.get_host(hid)['name'], 'Offline')
        self.assertFalse(Path(self.tmp.name, 'escape.py').exists())

    def test_unrecognized_zip_rejected(self):
        archive = Path(self.tmp.name, 'ordinary.zip')
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('main.py', 'print(1)')
        with self.assertRaises(ValueError):
            backup.restore_snapshot(archive)

    def test_backup_delivery_pin_and_registry(self):
        self.host()
        db.set_backup_channel(-100123)
        bot = types.SimpleNamespace(send_document=AsyncMock(return_value=types.SimpleNamespace(
            document=types.SimpleNamespace(file_id='durable_file'), message_id=42)), pin_chat_message=AsyncMock())
        with patch.object(backup, 'check_channel', AsyncMock()):
            asyncio.run(backup.send_snapshot(bot, 'test'))
        bot.pin_chat_message.assert_awaited_once_with(-100123, 42, disable_notification=True)
        self.assertEqual(db.list_backups()[0]['path'], 'durable_file')
        self.assertEqual(db.last_backup_day(), db.today())

    def test_channel_admin_and_post_permissions(self):
        bot = types.SimpleNamespace(get_chat=AsyncMock(return_value=types.SimpleNamespace(type='channel', id=-1001)),
            get_me=AsyncMock(return_value=types.SimpleNamespace(id=100)),
            get_chat_member=AsyncMock(return_value=types.SimpleNamespace(status='member')))
        with self.assertRaises(ValueError):
            asyncio.run(backup.check_channel(bot, -1001))
        bot.get_chat_member.return_value=types.SimpleNamespace(status='administrator', can_post_messages=False, can_edit_messages=True)
        with self.assertRaises(ValueError):
            asyncio.run(backup.check_channel(bot, -1001))
        bot.get_chat_member.return_value=types.SimpleNamespace(status='administrator', can_post_messages=True, can_edit_messages=True)
        asyncio.run(backup.check_channel(bot, -1001))
        bot.get_chat_member.assert_awaited_with(-1001, 100)

    def test_cold_restore_uses_pinned_document(self):
        bot = types.SimpleNamespace()
        pinned = types.SimpleNamespace(document=types.SimpleNamespace(file_id='pinned_zip', file_size=1024), caption=backup.MARKER, message_id=999)
        with patch.dict(os.environ, {'BACKUP_CHANNEL_ID': '-1009'}), \
             patch.object(backup, 'check_channel', AsyncMock(return_value=types.SimpleNamespace(pinned_message=pinned))), \
             patch.object(backup, 'restore_telegram', AsyncMock()) as restore:
            self.assertTrue(asyncio.run(backup.cold_restore(bot)))
            restore.assert_awaited_once_with(bot, 'pinned_zip')


class TestAdminAuthorization(Base):
    def test_every_admin_callback_rejects_nonadmin(self):
        for action in extensions.ADMIN_ACTIONS:
            c = types.SimpleNamespace(data=f'v2:{action}:1', from_user=types.SimpleNamespace(id=800008),
                answer=AsyncMock(), message=types.SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()))
            with patch.object(limits, 'is_admin', return_value=False):
                asyncio.run(extensions.callback(c, AsyncMock(), AsyncMock()))
            c.answer.assert_awaited_once_with('⛔ Faqat admin uchun', show_alert=True)
            c.message.answer.assert_not_awaited()
            c.message.edit_text.assert_not_awaited()

    def test_every_admin_fsm_rejects_nonadmin(self):
        for action in extensions.ADMIN_INPUTS:
            state = types.SimpleNamespace(get_data=AsyncMock(return_value={'action':action}), clear=AsyncMock())
            m = types.SimpleNamespace(from_user=types.SimpleNamespace(id=800008), answer=AsyncMock())
            with patch.object(limits, 'is_admin', return_value=False):
                asyncio.run(extensions.input_text(m, state, AsyncMock()))
            state.clear.assert_awaited_once()
            self.assertIn('Faqat admin', m.answer.call_args.args[0])

    def test_admin_menu_hidden_for_regular_users(self):
        with patch.object(limits, 'is_admin', return_value=False):
            regular = main.main_menu(800008)
        with patch.object(limits, 'is_admin', return_value=True):
            admin = main.main_menu(800009)
        callbacks = lambda markup: [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn('v2:admin', callbacks(regular))
        self.assertIn('v2:admin', callbacks(admin))

    def test_other_owner_callbacks_rejected(self):
        hid, _ = self.host(701)
        for action in extensions.OWN_ACTIONS:
            c = types.SimpleNamespace(data=f'v2:{action}:{hid}', from_user=types.SimpleNamespace(id=702), answer=AsyncMock())
            asyncio.run(extensions.callback(c, AsyncMock(), AsyncMock()))
            self.assertTrue(c.answer.call_args.kwargs.get('show_alert'))


class TestAiExtensions(Base):
    def test_project_generation_validates_and_zips(self):
        payload = json.dumps({'note':'Tayyor', 'entry':'server.py', 'files':{
            'server.py':'print(1)\n', 'index.html':'<html>miniapp</html>', 'requirements.txt':'aiohttp>=3.9'}})
        with patch.object(ai, 'chat', AsyncMock(return_value=payload)):
            result = asyncio.run(aiex.generate_project('py', [{'role':'user','content':'Mini App'}]))
        self.assertEqual(result.entry, 'server.py')
        import io
        with zipfile.ZipFile(io.BytesIO(aiex.zip_files(result.files))) as z:
            self.assertIn('index.html', z.namelist())
            self.assertIn('server.py', z.namelist())

    def test_unsafe_paths_and_bad_syntax_rejected(self):
        for path in ('../evil.py', '/main.py', '.env', 'a\\b.py', 'libs/main.py'):
            with self.assertRaises(ai.AIError):
                aiex.project_files(json.dumps({'entry':'main.py', 'files':{'main.py':'print(1)', path:'bad'}}))
        broken = json.dumps({'entry':'main.py','files':{'main.py':'def broken(:'}})
        with patch.object(ai, 'chat', AsyncMock(return_value=broken)):
            with self.assertRaises(ai.AIError):
                asyncio.run(aiex.generate_project('py', []))

    def test_parallel_model_selection_does_not_leak(self):
        async def worker(name):
            token = aiex.SELECTED_MODEL.set(name)
            await asyncio.sleep(0)
            try:
                return ai.model()
            finally:
                aiex.SELECTED_MODEL.reset(token)
        async def go():
            return await asyncio.gather(worker('model-a'), worker('model-b'))
        self.assertEqual(asyncio.run(go()), ['model-a', 'model-b'])


class TestGitResourcesSupervisor(Base):
    def test_git_urls_allow_github_only(self):
        self.assertEqual(runner.normalize_repo('owner/repo'), 'https://github.com/owner/repo.git')
        self.assertEqual(runner.normalize_repo('https://github.com/owner/repo.git'), 'https://github.com/owner/repo.git')
        for url in ('http://127.0.0.1/private', 'file:///etc/passwd', '-evil/repo', 'https://evil.com/a/b', 'owner/../repo'):
            self.assertFalse(runner.normalize_repo(url), url)

    def test_git_update_preserves_runtime_database(self):
        hid, d = self.host()
        (d/'data.db').write_text('existing-runtime-data')
        db.update_host(hid, git_repo='https://github.com/test/repo.git')
        async def fake_clone(repo, branch, dest):
            dest = Path(dest)
            dest.mkdir()
            (dest/'main.py').write_text('print("updated")\n')
            (dest/'data.db').write_text('empty-from-repo')
            return True, 'ok'
        with patch.object(runner, 'git_clone', fake_clone):
            asyncio.run(extensions.update_github(db.get_host(hid)))
        self.assertEqual((d/'data.db').read_text(), 'existing-runtime-data')
        self.assertIn('updated', (d/'main.py').read_text())
        self.assertFalse(runner.is_running(hid))

    def test_process_resources_and_traceback(self):
        hid, d = self.host()
        async def go():
            ok, _ = await runner.start(db.get_host(hid))
            self.assertTrue(ok)
            stats = runner.res_stats(hid)
            self.assertGreater(stats['ram'], 0)
            await runner.stop(hid)
        asyncio.run(go())
        error = runner.extract_error('info\nTraceback (most recent call last):\n  File x.py\nValueError: bad')
        self.assertIn('ValueError: bad', error)

    def test_crash_supervisor_notifies_and_restarts(self):
        hid, d = self.host()
        db.update_host(hid, want_running=1)
        (d/'main.py').write_text('import time\ntime.sleep(2.3)\nraise ValueError("offline-boom")\n')
        notices = []
        async def notify(host, message, tail='', code=None):
            notices.append((message, tail, code))
        async def go():
            ok, _ = await runner.start(db.get_host(hid))
            self.assertTrue(ok)
            # Replacement succeeds, so test can observe restart and stop it safely.
            await asyncio.sleep(.6)
            (d/'main.py').write_text('import time\ntime.sleep(60)\n')
            task = asyncio.create_task(runner.watchdog(notify, interval=.05))
            try:
                await asyncio.sleep(5.5)
                self.assertTrue(runner.is_running(hid))
                self.assertTrue(notices)
                self.assertIn('ValueError: offline-boom', notices[0][1])
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await runner.stop(hid)
        asyncio.run(go())
