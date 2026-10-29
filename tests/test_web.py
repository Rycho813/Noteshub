from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from noteshub.auth import provision
from noteshub.config import Settings
from noteshub.web import COOKIE, create_app


PASSWORD = "only-for-test-password"
SAMPLE = Path(__file__).parent / "fixtures" / "论爱欲.md"


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


class WebCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name), cookie_secure=False)
        provision(self.settings.data_dir, "reader", PASSWORD)
        self.app = create_app(self.settings)
        self.store = self.app.state.store
        self.client = self.enterContext(TestClient(self.app, follow_redirects=False))
        self.login()

    def login(self, client=None):
        client = client or self.client
        page = client.get("/login")
        response = client.post("/login", data={"csrf": csrf(page), "username": "reader", "password": PASSWORD})
        self.assertEqual(response.status_code, 303)
        return csrf(client.get("/"))

    def token(self, client=None):
        return csrf((client or self.client).get("/"))

    def create_book(self, title="测试书", client=None):
        client = client or self.client
        response = client.post("/books", data={"csrf": self.token(client), "title": title, "author": "作者"})
        self.assertEqual(response.status_code, 303)
        return response.headers["location"]

    def upload(self, book, raw="“原文”我的评述".encode(), filename="notes.md", client=None):
        client = client or self.client
        return client.post(book + "/imports", data={"csrf": self.token(client)},
                           files={"file": (filename, raw, "text/markdown")})

    def db_count(self, table):
        with self.store.connection() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class WebTests(WebCase):
    def test_all_private_routes_require_login(self):
        book = self.create_book()
        preview = self.upload(book).headers["location"]
        with TestClient(self.app, follow_redirects=False) as anonymous:
            for method, path in [
                ("get", "/"), ("get", book), ("get", preview),
                ("get", book + "/source"), ("post", "/books"),
                ("post", book + "/imports"), ("post", preview + "/confirm"),
                ("post", preview + "/cancel"), ("post", preview + "/records/1"),
                ("post", "/logout"),
            ]:
                with self.subTest(path=path):
                    response = getattr(anonymous, method)(path)
                    self.assertEqual(response.status_code, 303)
                    self.assertEqual(response.headers["location"], "/login")
            self.assertEqual(anonymous.get("/sources/notes.md").status_code, 404)
        self.assertEqual(self.db_count("notes"), 0)

    def test_csrf_required_for_mutation_and_login(self):
        for value in ("", "wrong", "中文"):
            response = self.client.post("/books", data={"csrf": value, "title": "bad"})
            self.assertEqual(response.status_code, 403)
        with TestClient(self.app, follow_redirects=False) as anonymous:
            self.assertEqual(anonymous.post("/login", data={"username": "reader", "password": PASSWORD}).status_code, 403)
        self.assertEqual(self.db_count("books"), 0)

    def test_book_validation_search_and_html_escaping(self):
        book = self.create_book('<script>alert("x")</script>100%')
        page = self.client.get(book)
        self.assertNotIn('<script>alert("x")</script>', page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.create_book("另一本")
        page = self.client.get("/", params={"q": "100%"})
        self.assertIn(book, page.text)
        self.assertNotIn("另一本", page.text)
        response = self.client.post("/books", data={"csrf": self.token(), "title": "  "})
        self.assertEqual(response.status_code, 422)

    def test_success_import_persists_notes_and_exact_original(self):
        book = self.create_book()
        raw = '\ufeff“外层“内层””紧贴评述\r\n\r\n“无评述”'.encode()
        preview = self.upload(book, raw, "../../原始笔记.md").headers["location"]
        self.assertEqual(self.db_count("notes"), 0)
        self.assertEqual(list(self.store.sources.iterdir()), [])
        response = self.client.post(preview + "/confirm", data={"csrf": self.token()})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.db_count("notes"), 2)
        self.assertEqual(self.db_count("imports"), 1)
        self.assertEqual(self.db_count("previews"), 0)
        with self.store.connection() as db:
            notes = db.execute("SELECT * FROM notes ORDER BY sort_order").fetchall()
            self.assertEqual(notes[0]["original_text"], "外层“内层”")
            self.assertEqual(notes[0]["first_comment"], "紧贴评述")
            self.assertEqual(notes[1]["first_comment"], "")
            self.assertEqual(notes[1]["status"], "pending")
        source = self.client.get(book + "/source")
        self.assertEqual(source.content, raw)
        self.assertIn("attachment", source.headers["content-disposition"])
        # Reconstruct the app, preserving the disk-backed session and data.
        restarted = create_app(self.settings)
        with TestClient(restarted, follow_redirects=False) as client:
            client.cookies.update(self.client.cookies)
            self.assertIn("外层“内层”", client.get(book).text)
            self.assertEqual(client.get(book + "/source").content, raw)

    def test_duplicate_upload_and_double_confirm_do_not_append(self):
        book = self.create_book()
        preview = self.upload(book).headers["location"]
        self.client.post(preview + "/confirm", data={"csrf": self.token()})
        same = self.upload(book)
        self.assertEqual(same.status_code, 409)
        self.assertIn("这个文件已经导入过", same.text)
        self.assertEqual(self.upload(book, "“其他原文”".encode()).status_code, 409)
        self.assertEqual(self.client.post(preview + "/confirm", data={"csrf": self.token()}).status_code, 409)
        self.assertEqual(self.db_count("notes"), 1)
        self.assertEqual(self.db_count("previews"), 0)
        self.assertEqual(len(list(self.store.sources.iterdir())), 1)

    def test_uploaded_and_corrected_html_is_escaped(self):
        book = self.create_book()
        payload = '</textarea><script>alert("x")</script>'
        preview = self.upload(book, ("“原文”" + payload).encode()).headers["location"]
        page = self.client.get(preview)
        self.assertNotIn(payload, page.text)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;", page.text)
        response = self.client.post(preview + "/records/1", data={
            "csrf": self.token(), "original_text": payload, "first_comment": payload,
        })
        self.assertEqual(response.status_code, 303)
        self.client.post(preview + "/confirm", data={"csrf": self.token()})
        self.assertNotIn(payload, self.client.get(book).text)
        with self.store.connection() as db:
            note = db.execute("SELECT * FROM notes").fetchone()
            self.assertEqual(note["original_text"], payload)
            self.assertEqual(note["first_comment"], payload)

    @unittest.skipUnless(SAMPLE.is_file(), "Private Zotero fixture is not included in Git")
    def test_real_sample_corrections_and_complete_transaction(self):
        book = self.create_book("论爱欲")
        raw = SAMPLE.read_bytes()
        preview = self.upload(book, raw).headers["location"]
        page = self.client.get(preview)
        self.assertIn("待修正 7 条", page.text)
        self.assertIn("源文件行 70–71", page.text)
        self.assertIn("record-4", page.text)
        self.assertEqual(self.client.post(preview + "/confirm", data={"csrf": self.token(), "can_confirm": "true", "corrected": "true"}).status_code, 422)
        self.assertEqual(self.db_count("notes"), 0)
        self.assertEqual(list(self.store.sources.iterdir()), [])
        # Synthetic user-entered corrections exercise the workflow. They are
        # deliberately not presented as the user's final readings of this book.
        for record_id in (3, 4, 15, 16, 32, 42, 56):
            response = self.client.post(preview + f"/records/{record_id}", data={
                "csrf": self.token(), "original_text": f"测试修正原文 {record_id}", "first_comment": "测试评述",
            })
            self.assertEqual(response.status_code, 303)
        self.assertIn("待修正 0 条", self.client.get(preview).text)
        self.assertEqual(self.client.post(preview + "/confirm", data={"csrf": self.token()}).status_code, 303)
        self.assertEqual(self.db_count("notes"), 154)
        self.assertEqual(self.client.get(book + "/source").content, raw)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT SUM(manually_corrected) FROM notes").fetchone()[0], 7)

    def test_cancel_replacement_expiry_and_logout_cleanup(self):
        book = self.create_book()
        first = self.upload(book, "“坏引号".encode()).headers["location"]
        self.assertEqual(self.db_count("previews"), 1)
        replacement = self.upload(book).headers["location"]
        self.assertEqual(self.client.get(first).status_code, 404)
        self.assertEqual(self.db_count("previews"), 1)
        self.assertEqual(self.client.post(replacement + "/cancel", data={"csrf": self.token()}).status_code, 303)
        self.assertEqual(self.db_count("previews"), 0)
        expiring = self.upload(book).headers["location"]
        with self.store.connection() as db:
            db.execute("UPDATE previews SET expires_at = ?", (int(time.time()) - 1,))
        self.assertEqual(self.client.get(expiring).status_code, 404)
        self.assertEqual(self.db_count("previews"), 0)
        self.upload(book)
        self.client.post("/logout", data={"csrf": self.token()})
        self.assertEqual(self.db_count("previews"), 0)
        self.assertEqual(list(self.store.sources.iterdir()), [])
        self.assertEqual(self.client.get(book).status_code, 303)

    def test_preview_owned_by_session(self):
        book = self.create_book()
        preview = self.upload(book).headers["location"]
        with TestClient(self.app, follow_redirects=False) as other:
            self.login(other)
            self.assertEqual(other.get(preview).status_code, 404)
            self.assertEqual(other.post(preview + "/confirm", data={"csrf": self.token(other)}).status_code, 404)
            self.assertEqual(other.post(preview + "/cancel", data={"csrf": self.token(other)}).status_code, 404)
            self.assertEqual(other.post(preview + "/records/1", data={"csrf": self.token(other), "original_text": "修改"}).status_code, 404)
        self.assertEqual(self.db_count("previews"), 1)

    def test_bad_uploads_leave_no_material(self):
        book = self.create_book()
        for raw, filename, expected in [
            (b"\xff\xfe", "bad.md", 422), (b"", "empty.md", 422),
            (b"\x00", "binary.md", 422), (b"abc", "bad.pdf", 422),
            (b"a" * (self.settings.max_upload_bytes + 1), "large.md", 413),
        ]:
            with self.subTest(filename=filename):
                self.assertEqual(self.upload(book, raw, filename).status_code, expected)
        self.assertEqual(self.db_count("previews"), 0)
        self.assertEqual(self.db_count("imports"), 0)
        self.assertEqual(list(self.store.sources.iterdir()), [])

    def test_oversized_chunked_body_is_rejected(self):
        def chunks():
            yield b"x" * self.settings.max_upload_bytes
            yield b"x" * (64 * 1024 + 1)
        response = self.client.post("/books", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.db_count("books"), 0)

    def test_invalid_correction_and_partial_fix_do_not_confirm(self):
        book = self.create_book()
        preview = self.upload(book, "“坏一\n\n“坏二".encode()).headers["location"]
        for record_id, original in ((1, " "), (999, "文本")):
            response = self.client.post(preview + f"/records/{record_id}", data={"csrf": self.token(), "original_text": original})
            self.assertEqual(response.status_code, 422)
        self.client.post(preview + "/records/1", data={"csrf": self.token(), "original_text": "修好一"})
        self.assertEqual(self.client.post(preview + "/confirm", data={"csrf": self.token()}).status_code, 422)
        self.assertEqual(self.db_count("notes"), 0)

    def test_database_failure_rolls_back_notes_import_and_source(self):
        book = self.create_book()
        preview = self.upload(book, "“第一条”\n\n“第二条”".encode()).headers["location"]
        with self.store.connection() as db:
            db.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON notes
            WHEN NEW.sort_order = 1 BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        response = self.client.post(preview + "/confirm", data={"csrf": self.token()})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.db_count("notes"), 0)
        self.assertEqual(self.db_count("imports"), 0)
        self.assertEqual(self.db_count("previews"), 1)
        self.assertEqual(list(self.store.sources.iterdir()), [])
        with self.store.connection() as db:
            db.execute("DROP TRIGGER fail_second")
        self.assertEqual(self.client.post(preview + "/confirm", data={"csrf": self.token()}).status_code, 303)
        self.assertEqual(self.db_count("notes"), 2)

    def test_file_failure_does_not_commit(self):
        book = self.create_book()
        preview = self.upload(book).headers["location"]
        with patch("noteshub.db.os.fsync", side_effect=OSError("disk failure")):
            self.assertEqual(self.client.post(preview + "/confirm", data={"csrf": self.token()}).status_code, 503)
        self.assertEqual(self.db_count("notes"), 0)
        self.assertEqual(self.db_count("imports"), 0)
        self.assertEqual(list(self.store.sources.iterdir()), [])

    def test_concurrent_confirm_allows_only_one_import(self):
        book = self.create_book()
        preview_path = self.upload(book).headers["location"]
        preview_id = preview_path.rsplit("/", 1)[1]
        session_id = hashlib.sha256(self.client.cookies[COOKIE].encode()).hexdigest()
        def confirm():
            try:
                return self.store.confirm(book.rsplit("/", 1)[1], preview_id, session_id)
            except ValueError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: confirm(), range(2)))
        self.assertCountEqual(results, [1, "conflict"])
        self.assertEqual(self.db_count("notes"), 1)
        self.assertEqual(len(list(self.store.sources.iterdir())), 1)

    def test_orphan_source_recovered_on_restart(self):
        orphan = self.store.sources / ("a" * 32 + ".md")
        orphan.write_text("crash leftover")
        create_app(self.settings)
        self.assertFalse(orphan.exists())

    def test_login_throttle_logout_session_and_headers(self):
        self.client.post("/logout", data={"csrf": self.token()})
        page = self.client.get("/login")
        for _ in range(5):
            response = self.client.post("/login", data={"csrf": csrf(page), "username": "reader", "password": "wrong"})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post("/login", data={"csrf": csrf(page), "username": "reader", "password": PASSWORD}).status_code, 429)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])

    def test_session_expiry_blocks_access_and_cleans_previews(self):
        book = self.create_book()
        self.upload(book)
        with self.store.connection() as db:
            db.execute("UPDATE sessions SET expires_at = 0")
        self.assertEqual(self.client.get(book).status_code, 303)
        self.store.cleanup()
        self.assertEqual(self.db_count("previews"), 0)


class ProvisionTests(unittest.TestCase):
    def test_no_default_login_and_existing_account_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            with self.assertRaises(RuntimeError):
                create_app(settings)
            with self.assertRaises(ValueError):
                provision(settings.data_dir, "reader", "short")
            provision(settings.data_dir, "reader", PASSWORD)
            before = (settings.data_dir / "account.json").read_bytes()
            with self.assertRaises(FileExistsError):
                provision(settings.data_dir, "another", PASSWORD)
            self.assertEqual((settings.data_dir / "account.json").read_bytes(), before)
            self.assertNotIn(PASSWORD.encode(), before)

    def test_secure_cookies_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp))
            provision(settings.data_dir, "reader", PASSWORD)
            with TestClient(create_app(settings), base_url="https://testserver") as client:
                page = client.get("/login")
                response = client.post("/login", data={"csrf": csrf(page), "username": "reader", "password": PASSWORD}, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                cookie = response.headers.get_list("set-cookie")[0]
                for flag in ("Secure", "HttpOnly", "SameSite=strict"):
                    self.assertIn(flag, cookie)


if __name__ == "__main__":
    unittest.main()
