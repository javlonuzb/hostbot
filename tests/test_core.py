import asyncio
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.getcwd())  # testlar repo ildizidan ishga tushiriladi
import db  # noqa: E402
import runner  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DATA_DIR = self.tmp.name
        db.init()

    def tearDown(self):
        asyncio.run(runner.stop_all())
        self.tmp.cleanup()


class TestDb(Base):
    def test_crud(self):
        hid = db.add_host(1, "123:tok", "Bot", "bot_u")
        self.assertEqual(db.get_host(hid)["name"], "Bot")
        db.update_host(hid, entry="main.py", want_running=1)
        self.assertEqual(db.get_host(hid)["entry"], "main.py")
        self.assertEqual(len(db.user_hosts(1)), 1)
        with self.assertRaises(ValueError):
            db.update_host(hid, owner_id=5)
        db.delete_host(hid)
        self.assertIsNone(db.get_host(hid))


class TestFiles(Base):
    def test_zip_traversal_blocked(self):
        z = os.path.join(self.tmp.name, "bad.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../evil.py", "print(1)")
        with self.assertRaises(ValueError):
            runner.safe_extract(z, os.path.join(self.tmp.name, "out"))

    def test_zip_strips_root_and_finds_entry(self):
        z = os.path.join(self.tmp.name, "ok.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("proj/bot.py", "print(1)")
            zf.writestr("proj/utils.py", "x=1")
        out = os.path.join(self.tmp.name, "out")
        runner.safe_extract(z, out)
        self.assertEqual(runner.find_entry(out), "bot.py")

    def test_single_file_entry(self):
        d = os.path.join(self.tmp.name, "d")
        os.makedirs(d)
        with open(os.path.join(d, "hello.py"), "w") as f:
            f.write("print(1)")
        self.assertEqual(runner.find_entry(d), "hello.py")

    def test_db_info_and_clear(self):
        d = os.path.join(self.tmp.name, "d")
        os.makedirs(d)
        p = os.path.join(d, "data.db")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE users(id INTEGER)")
        con.execute("INSERT INTO users VALUES(1)")
        con.commit()
        con.close()
        info = runner.db_info(d)
        self.assertEqual(info[0][2], [("users", 1)])
        self.assertEqual(runner.clear_db(d), 1)
        self.assertEqual(runner.db_files(d), [])


class TestRun(Base):
    def test_start_stop_and_env_isolation(self):
        os.environ["MASTER_BOT_TOKEN"] = "SECRET_MASTER"
        hid = db.add_host(1, "111:CHILDTOKEN", "T", "t")
        d = db.bot_dir(hid)
        os.makedirs(d)
        with open(os.path.join(d, "main.py"), "w") as f:
            f.write(
                "import os, time\n"
                "print('TOKEN=', os.getenv('BOT_TOKEN'))\n"
                "print('MASTER=', os.getenv('MASTER_BOT_TOKEN'))\n"
                "time.sleep(60)\n"
            )
        db.update_host(hid, entry="main.py")

        async def go():
            ok, _ = await runner.start(db.get_host(hid))
            self.assertTrue(ok)
            self.assertTrue(runner.is_running(hid))
            log = runner.tail_log(hid)
            await runner.stop(hid)
            self.assertFalse(runner.is_running(hid))
            return log

        log = asyncio.run(go())
        self.assertIn("111:CHILDTOKEN", log)
        self.assertIn("MASTER= None", log)
        self.assertNotIn("SECRET_MASTER", log)

    def test_instant_crash_reported(self):
        hid = db.add_host(1, "111:T", "T", "t")
        d = db.bot_dir(hid)
        os.makedirs(d)
        with open(os.path.join(d, "main.py"), "w") as f:
            f.write("raise SystemExit(3)\n")
        db.update_host(hid, entry="main.py")
        ok, msg = asyncio.run(runner.start(db.get_host(hid)))
        self.assertFalse(ok)
        self.assertIn("kod 3", msg)


class TestRelativeDataDir(unittest.TestCase):
    """DATA_DIR='data' (nisbiy) bo'lganda ham requirements, DB_PATH to'g'ri ishlashi kerak."""

    def test_relative_paths(self):
        old_cwd, old_dir = os.getcwd(), db.DATA_DIR
        tmp = tempfile.TemporaryDirectory()
        try:
            os.chdir(tmp.name)
            db.DATA_DIR = "data"
            db.init()
            hid = db.add_host(1, "111:T", "T", "t")
            d = db.bot_dir(hid)
            self.assertTrue(os.path.isabs(d))
            os.makedirs(d)
            with open(os.path.join(d, "requirements.txt"), "w") as f:
                f.write("# bo'sh\n")
            with open(os.path.join(d, "main.py"), "w") as f:
                f.write(
                    "import os, sqlite3, time\n"
                    "c = sqlite3.connect(os.environ['DB_PATH']); c.execute('create table t(x)'); c.commit()\n"
                    "print('DB=', os.environ['DB_PATH'])\n"
                    "time.sleep(60)\n"
                )
            db.update_host(hid, entry="main.py")

            async def go():
                ok, msg = await runner.start(db.get_host(hid))
                log = runner.tail_log(hid)
                await runner.stop(hid)
                return ok, msg, log

            ok, msg, log = asyncio.run(go())
            self.assertTrue(ok, msg + log)
            self.assertTrue(os.path.isfile(os.path.join(d, "data.db")))
        finally:
            asyncio.run(runner.stop_all())
            os.chdir(old_cwd)
            db.DATA_DIR = old_dir
            tmp.cleanup()


class TestZipAsEntry(Base):
    def test_zip_saved_as_py_is_rejected(self):
        hid = db.add_host(1, "111:T", "T", "t")
        d = db.bot_dir(hid)
        os.makedirs(d)
        with zipfile.ZipFile(os.path.join(d, "echo_bot.py"), "w") as zf:
            zf.writestr("x.py", "print(1)")
        db.update_host(hid, entry="echo_bot.py")
        ok, msg = asyncio.run(runner.start(db.get_host(hid)))
        self.assertFalse(ok)
        self.assertIn("zip", msg)


@unittest.skipUnless(importlib.util.find_spec("aiogram"), "aiogram o'rnatilmagan")
class TestImport(unittest.TestCase):
    def test_main_imports(self):
        import main  # noqa: F401
