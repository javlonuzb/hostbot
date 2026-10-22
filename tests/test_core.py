import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


@unittest.skipUnless(
    __import__("importlib").util.find_spec("aiogram"), "aiogram o'rnatilmagan"
)
class TestImport(unittest.TestCase):
    def test_main_imports(self):
        import main  # noqa: F401
