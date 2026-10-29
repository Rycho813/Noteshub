import re
from urllib.parse import unquote

from fastapi.testclient import TestClient

from noteshub.web import create_app
from tests.test_web import WebCase


class ReviewTests(WebCase):
    def setUp(self):
        super().setUp()
        self.book = self.create_book("论爱欲")
        preview = self.upload(self.book, "“原文一”一读一\n\n“原文二”\n\n“原文三”一读三".encode()).headers["location"]
        self.assertEqual(self.client.post(preview + "/confirm", data={"csrf": self.token()}).status_code, 303)
        with self.store.connection() as db:
            self.notes = db.execute("SELECT * FROM notes ORDER BY sort_order").fetchall()
        self.api = [f"/api{self.book}/notes/{note['id']}" for note in self.notes]

    def save(self, index, text, status=None, client=None):
        client = client or self.client
        payload = {"csrf": self.token(client), "second_comment": text}
        if status is not None:
            payload["status"] = status
        return client.post(self.api[index], data=payload)

    def stored(self, index):
        with self.store.connection() as db:
            return db.execute("SELECT * FROM notes WHERE id = ?", (self.notes[index]["id"],)).fetchone()

    def test_edit_does_not_complete_and_refresh_preserves_text(self):
        text = "\n二读内容\n\n第二段 *纯文本*  "
        response = self.save(0, text)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["second_comment"], text)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(response.json()["done"], 0)
        self.assertEqual(self.stored(0)["second_comment"], text)
        self.assertIn("第二段 *纯文本*", self.client.get(self.book).text)
        self.assertEqual(self.stored(0)["original_text"], "原文一")
        self.assertEqual(self.stored(0)["first_comment"], "一读一")

    def test_empty_comment_done_edit_and_restore_pending(self):
        completed = self.save(0, "", "done")
        self.assertEqual(completed.json()["done"], 1)
        self.assertEqual(completed.json()["status"], "done")
        edited = self.save(0, "完成后修改")
        self.assertEqual(edited.json()["status"], "done")
        self.assertEqual(edited.json()["done"], 1)
        restored = self.save(0, "完成后修改", "pending")
        self.assertEqual(restored.json()["done"], 0)
        self.assertIn("已完成 <strong id=\"book-progress\">0/3", self.client.get(self.book).text)

    def test_repeating_completion_does_not_double_count(self):
        for _ in range(2):
            self.assertEqual(self.save(0, "", "done").json()["done"], 1)
        self.assertIn("已完成 1/3", self.client.get("/").text)

    def test_server_saves_last_submission_without_version_conflict(self):
        with TestClient(self.app, follow_redirects=False) as other:
            self.login(other)
            self.assertEqual(self.save(0, "电脑提交", "done").status_code, 200)
            last = self.save(0, "手机后提交", client=other)
            self.assertEqual(last.status_code, 200)
            self.assertEqual(last.json()["status"], "done")
        self.assertEqual(self.stored(0)["second_comment"], "手机后提交")

    def test_one_note_at_a_time_and_ordered_navigation(self):
        first = self.client.get(self.book).text
        self.assertIn("原文一", first)
        self.assertNotIn("原文二", first)
        self.assertIn(f"?note={self.notes[1]['id']}", first)
        middle = self.client.get(self.book, params={"note": self.notes[1]["id"]}).text
        self.assertIn("原文二", middle)
        self.assertNotIn("原文一", middle)
        self.assertIn(f"?note={self.notes[0]['id']}", middle)
        self.assertIn(f"?note={self.notes[2]['id']}", middle)
        last = self.client.get(self.book, params={"note": self.notes[2]["id"]}).text
        self.assertIn('data-next-url=""', last)
        self.assertEqual(self.client.get(self.book, params={"note": "missing"}).status_code, 404)

    def test_assets_do_not_use_internal_http_origin_behind_tls_proxy(self):
        page = self.client.get(self.book, headers={"host": "notes.example.com", "x-forwarded-proto": "https"})
        self.assertIn('href="/static/app.css"', page.text)
        self.assertIn('src="/static/review.js"', page.text)
        self.assertNotIn('src="http://', page.text)
        self.assertNotIn('href="http://', page.text)

    def test_save_requires_authentication_and_csrf(self):
        with TestClient(self.app, follow_redirects=False) as anonymous:
            response = anonymous.post(self.api[0], data={"second_comment": "恶意修改"})
            self.assertEqual(response.status_code, 401)
            self.assertIn("登录已失效", response.json()["detail"])
            self.assertEqual(anonymous.get(self.book + "/export").status_code, 303)
        response = self.client.post(self.api[0], data={"csrf": "bad", "second_comment": "恶意修改"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stored(0)["second_comment"], "")

    def test_cannot_save_another_books_note(self):
        other_book = self.create_book("其他书")
        response = self.client.post(f"/api{other_book}/notes/{self.notes[0]['id']}", data={"csrf": self.token(), "second_comment": "错书"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.stored(0)["second_comment"], "")
        self.assertEqual(self.client.get(other_book, params={"note": self.notes[0]["id"]}).status_code, 404)

    def test_invalid_requests_never_clear_existing_content(self):
        self.save(0, "保留")
        invalid = [
            {"status": "done"}, {"second_comment": "", "status": "skipped"},
            {"second_comment": "\x00"}, {"second_comment": "字" * 50001},
        ]
        for fields in invalid:
            with self.subTest(fields=list(fields)):
                response = self.client.post(self.api[0], data={"csrf": self.token(), **fields})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.stored(0)["second_comment"], "保留")
        self.assertEqual(self.save(0, "字" * 50000).status_code, 200)
        self.assertEqual(self.save(0, "").status_code, 200)
        self.assertEqual(self.stored(0)["second_comment"], "")

    def test_text_and_status_roll_back_together_on_failure(self):
        self.save(0, "之前的内容")
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_save BEFORE UPDATE ON notes BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        response = self.save(0, "新内容", "done")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.stored(0)["second_comment"], "之前的内容")
        self.assertEqual(self.stored(0)["status"], "pending")
        with self.store.connection() as db:
            db.execute("DROP TRIGGER fail_save")
        self.assertEqual(self.save(0, "新内容", "done").status_code, 200)

    def test_saved_html_stays_plain_text(self):
        text = '</textarea><script>alert("x")</script>'
        self.save(0, text)
        page = self.client.get(self.book).text
        self.assertNotIn(text, page)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;", page)
        self.assertEqual(self.stored(0)["second_comment"], text)

    def test_export_includes_all_statuses_and_utf8_filename(self):
        self.save(0, "已完成评述", "done")
        self.save(1, "待处理也导出")
        response = self.client.get(self.book + "/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/markdown", response.headers["content-type"])
        self.assertIn("charset=utf-8", response.headers["content-type"])
        self.assertIn("论爱欲.md", unquote(response.headers["content-disposition"]))
        self.assertEqual(response.headers["x-noteshub-pending"], "2")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(re.findall(r"^## 笔记 \d+$", response.text, re.MULTILINE), ["## 笔记 1", "## 笔记 2", "## 笔记 3"])
        for value in ("原文一", "原文二", "原文三", "已完成评述", "待处理也导出"):
            self.assertIn(value, response.text)
        self.assertNotIn("zotero://", response.text)
        self.assertEqual(response.content.decode("utf-8"), response.text)
        self.assertEqual(self.client.get(self.book + "/export").content, response.content)

    def test_export_empty_or_missing_book(self):
        empty = self.create_book("没有笔记")
        self.assertEqual(self.client.get(empty + "/export").status_code, 409)
        self.assertEqual(self.client.get("/books/missing/export").status_code, 404)

    def test_existing_data_and_saved_review_survive_restart(self):
        self.save(0, "重启保留", "done")
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        restarted = create_app(self.settings)
        with TestClient(restarted, follow_redirects=False) as client:
            client.cookies.update(self.client.cookies)
            self.assertIn("重启保留", client.get(self.book).text)
            self.assertIn("重启保留", client.get(self.book + "/export").text)
            self.assertIn("已完成 1/3", client.get("/").text)
