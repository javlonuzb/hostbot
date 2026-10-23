import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.getcwd())
import ai  # noqa: E402
import db  # noqa: E402
import runner  # noqa: E402

HAS_PHP = bool(shutil.which("php"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DATA_DIR = self.tmp.name
        db.init()

    def tearDown(self):
        asyncio.run(runner.stop_all())
        self.tmp.cleanup()


class TestNews(Base):
    def test_seed_and_crud(self):
        self.assertEqual(len(db.list_news()), 1)  # boshlang'ich yangilik
        db.init()  # qayta init takror qo'shmasligi kerak
        self.assertEqual(len(db.list_news()), 1)
        nid = db.add_news(7, "salom")
        self.assertEqual(db.list_news(1)[0]["id"], nid)
        self.assertEqual(db.delete_news(nid), 1)

    def test_unread_badge(self):
        self.assertEqual(db.unread_news(5), 1)
        db.mark_news_seen(5)
        self.assertEqual(db.unread_news(5), 0)
        db.add_news(1, "yana")
        self.assertEqual(db.unread_news(5), 1)
        self.assertEqual(db.unread_news(6), 2)  # boshqa foydalanuvchiga ta'sir qilmaydi


class TestEntry(Base):
    def _d(self, files):
        d = os.path.join(self.tmp.name, "d")
        os.makedirs(d, exist_ok=True)
        for name, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        return d

    def test_php_only(self):
        d = self._d({"x.php": "<?php echo 1;"})
        self.assertEqual(runner.find_entry(d), "x.php")
        self.assertEqual(runner.lang_of("x.php"), "php")
        self.assertEqual(runner.lang_of("x.py"), "py")

    def test_php_candidate_and_marker(self):
        d = self._d({"bot.php": "<?php", "util.php": "<?php"})
        self.assertEqual(runner.find_entry(d), "bot.php")
        d2 = os.path.join(self.tmp.name, "e")
        os.makedirs(d2)
        for n, t in (("a.php", "<?php"), ("z.php", "<?php file_get_contents('https://api.telegram.org/x/getUpdates');")):
            with open(os.path.join(d2, n), "w") as f:
                f.write(t)
        self.assertEqual(runner.find_entry(d2), "z.php")

    def test_python_wins_over_php(self):
        d = self._d({"hello.py": "print(1)", "other.php": "<?php"})
        self.assertEqual(runner.find_entry(d), "hello.py")

    def test_list_code_skips_vendor(self):
        d = self._d({"bot.php": "<?php", "vendor/x/y.php": "<?php", "a.py": "x=1", "n.txt": "t"})
        self.assertEqual(sorted(runner.list_code(d)), ["a.py", "bot.php"])


class TestZipBackup(Base):
    def test_skips_libs_and_logs(self):
        d = os.path.join(self.tmp.name, "b")
        for rel in ("main.py", "libs/pkg/x.py", "bot.log", "data.db", "sub/u.py"):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("x")
        out = os.path.join(self.tmp.name, "o.zip")
        runner.make_zip(d, out)
        with zipfile.ZipFile(out) as z:
            self.assertEqual(sorted(z.namelist()), ["data.db", "main.py", "sub/u.py"])

    def test_vendor_kept_without_composer(self):
        d = os.path.join(self.tmp.name, "b")
        os.makedirs(os.path.join(d, "vendor"))
        with open(os.path.join(d, "vendor", "a.php"), "w") as f:
            f.write("x")
        out = os.path.join(self.tmp.name, "o.zip")
        runner.make_zip(d, out)
        with zipfile.ZipFile(out) as z:
            self.assertIn("vendor/a.php", z.namelist())


class TestEnv(Base):
    def test_api_key_not_leaked_to_child(self):
        os.environ["API_KEY"] = "DEEPSEEK_SECRET_123456"
        try:
            hid = db.add_host(1, "111:T", "T", "t")
            d = db.bot_dir(hid)
            os.makedirs(d)
            with open(os.path.join(d, "main.py"), "w") as f:
                f.write("import os,time\nprint('KEY=', os.getenv('API_KEY'))\ntime.sleep(30)\n")
            db.update_host(hid, entry="main.py")

            async def go():
                ok, _ = await runner.start(db.get_host(hid))
                log = runner.tail_log(hid)
                self.assertIsNotNone(runner.uptime(hid))
                await runner.stop(hid)
                self.assertIsNone(runner.uptime(hid))
                return ok, log

            ok, log = asyncio.run(go())
            self.assertTrue(ok)
            self.assertIn("KEY= None", log)
            self.assertNotIn("DEEPSEEK_SECRET", log)
        finally:
            os.environ.pop("API_KEY", None)


@unittest.skipUnless(HAS_PHP, "php o'rnatilmagan")
class TestPhpRun(Base):
    def test_php_bot_runs_with_env(self):
        hid = db.add_host(1, "111:PHPTOKEN", "T", "t")
        d = db.bot_dir(hid)
        os.makedirs(d)
        with open(os.path.join(d, "bot.php"), "w") as f:
            f.write("<?php echo 'T=' . getenv('BOT_TOKEN') . PHP_EOL; echo 'K=' . var_export(getenv('API_KEY'), true) . PHP_EOL; sleep(30);")
        db.update_host(hid, entry="bot.php")

        async def go():
            ok, msg = await runner.start(db.get_host(hid))
            log = runner.tail_log(hid)
            await runner.stop(hid)
            return ok, msg, log

        ok, msg, log = asyncio.run(go())
        self.assertTrue(ok, msg + log)
        self.assertIn("T=111:PHPTOKEN", log)
        self.assertIn("K=false", log)


class TestAiParsing(unittest.TestCase):
    def test_python_with_requirements(self):
        text = "Mana bot.\n```python\nprint('hi')\n```\n```requirements\naiogram>=3.13,<4\n-r evil.txt\ngit+https://x/y\nrequests # comment\n```"
        note, code, reqs = ai.parse_reply(text, "py")
        self.assertEqual(note, "Mana bot.")
        self.assertEqual(code, "print('hi')\n")
        self.assertEqual(reqs, ["aiogram>=3.13,<4", "requests"])

    def test_php_ignores_requirements(self):
        text = "Tayyor\n```php\n<?php echo 1;\n```\n```requirements\nfoo\n```"
        _, code, reqs = ai.parse_reply(text, "php")
        self.assertTrue(code.startswith("<?php"))
        self.assertEqual(reqs, [])

    def test_errors(self):
        with self.assertRaises(ai.AIError):
            ai.parse_reply("faqat matn", "py")
        with self.assertRaises(ai.AIError):
            ai.parse_reply("```python\nprint(1)", "py")  # uzilgan blok

    def test_redact(self):
        t = "token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw9 va SECRETVALUE1"
        r = ai.redact(t, ("SECRETVALUE1",))
        self.assertNotIn("AAHdq", r)
        self.assertNotIn("SECRETVALUE1", r)

    def test_merge_requirements(self):
        out = ai.merge_requirements("aiogram>=3\nrequests==2.0\n", ["Requests>=2", "aiohttp>=3.9"])
        self.assertEqual(out, "aiogram>=3\nrequests==2.0\naiohttp>=3.9\n")

    def test_trim_starts_with_user(self):
        h = [{"role": "user", "content": str(i)} if i % 2 == 0 else {"role": "assistant", "content": str(i)}
             for i in range(7)]
        self.assertEqual(ai._trim(h)[0]["role"], "user")


class TestAiGenerate(unittest.TestCase):
    def setUp(self):
        self.orig = ai.chat

    def tearDown(self):
        ai.chat = self.orig

    def test_retry_on_syntax_error(self):
        replies = ["Xato\n```python\ndef f(:\n```", "To'g'ri\n```python\nprint('ok')\n```"]
        calls = []

        async def fake(messages, **kw):
            calls.append(messages)
            return replies[len(calls) - 1]

        ai.chat = fake
        res = asyncio.run(ai.generate("py", [{"role": "user", "content": "bot"}]))
        self.assertEqual(len(calls), 2)
        self.assertEqual(res.code, "print('ok')\n")
        self.assertEqual(res.warning, "")
        self.assertEqual(res.history[-1]["role"], "assistant")
        self.assertEqual(calls[0][0]["role"], "system")

    def test_warning_when_still_broken(self):
        async def fake(messages, **kw):
            return "```python\ndef f(:\n```"

        ai.chat = fake
        res = asyncio.run(ai.generate("py", [{"role": "user", "content": "bot"}]))
        self.assertIn("sintaksis", res.warning)

    def test_no_key(self):
        os.environ.pop("API_KEY", None)
        self.assertFalse(ai.enabled())
        with self.assertRaises(ai.AIError):
            asyncio.run(ai.chat([{"role": "user", "content": "x"}]))

    def test_fix_request_redacts(self):
        msg = ai.fix_request("bot.py", "T='123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw9'", "log", "", ())
        self.assertNotIn("AAHdq", msg)
        self.assertIn("bot.py", msg)


if __name__ == "__main__":
    unittest.main()
