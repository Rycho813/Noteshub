from contextlib import closing
"""Permanent deletion using isolated, synthetic imports only."""

from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import sqlite3
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from noteshub.db import Store
from noteshub.maintenance import backup, restore
from tests.test_web import WebCase


class DeletionTests(WebCase):
    def setUp(self):
        super().setUp()
        self.book = self.create_book("删除测试")
        self.raw = "“原文一”评述一\n\n“原文二”\n\n“原文三”评述三".encode()
        preview = self.upload(self.book, self.raw).headers["location"]
        self.assertEqual(self.client.post(preview + "/confirm", data={"csrf": self.token()}).status_code, 303)
        self.book_id = self.book.rsplit("/", 1)[1]
        with self.store.connection() as db:
            self.notes = db.execute("SELECT * FROM notes ORDER BY sort_order").fetchall()
            filename = db.execute("SELECT original_file_path FROM imports").fetchone()[0]
        self.source = self.store.sources / filename
        self.note_urls = [f"{self.book}/notes/{note['id']}/delete" for note in self.notes]
        self.payload = {"csrf": self.token(), "confirm": "delete"}

    def assert_integrity(self):
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_confirmation_get_never_deletes_and_escapes_content(self):
        with self.store.connection() as db:
            db.execute("UPDATE books SET title = '<script>book</script>' WHERE id = ?", (self.book_id,))
            db.execute("UPDATE notes SET original_text = '<script>note</script>' WHERE id = ?", (self.notes[0]["id"],))
        book_page = self.client.get(self.book + "/delete")
        self.assertIn("全部 3 条笔记", book_page.text)
        self.assertIn("临时预览和原始文件", book_page.text)
        self.assertIn("无法撤销", book_page.text)
        self.assertIn("&lt;script&gt;book", book_page.text)
        note_page = self.client.get(self.note_urls[0])
        self.assertIn("其他笔记和原始导入文件会保留", note_page.text)
        self.assertIn("&lt;script&gt;note", note_page.text)
        self.assertEqual(self.db_count("notes"), 3)
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_auth_csrf_and_explicit_confirmation_required(self):
        paths = [self.book + "/delete", self.note_urls[0]]
        with TestClient(self.app, follow_redirects=False) as anonymous:
            for path in paths:
                self.assertEqual(anonymous.get(path).headers["location"], "/login")
                self.assertEqual(anonymous.post(path, data=self.payload).headers["location"], "/login")
        for path in paths:
            for token in ("", "wrong", "中文"):
                self.assertEqual(self.client.post(path, data={"csrf": token, "confirm": "delete"}).status_code, 403)
            self.assertEqual(self.client.post(path, data={"csrf": self.token()}).status_code, 422)
        self.assertEqual(self.db_count("notes"), 3)

    def test_book_deletes_all_associated_data_and_preserves_other_book(self):
        other = self.create_book("保留的书")
        preview = self.upload(other).headers["location"]
        self.client.post(preview + "/confirm", data=self.payload)
        other_raw = self.client.get(other + "/source").content
        response = self.client.post(self.book + "/delete", data=self.payload)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/"))
        self.assertEqual(self.db_count("books"), 1)
        self.assertEqual(self.db_count("notes"), 1)
        self.assertEqual(self.db_count("imports"), 1)
        self.assertFalse(self.source.exists())
        self.assertEqual(len(list(self.store.sources.iterdir())), 1)
        self.assertEqual(self.client.get(other + "/source").content, other_raw)
        self.assertNotIn("删除测试", self.client.get("/").text)
        for suffix in ("", "/source", "/export", "/delete"):
            self.assertEqual(self.client.get(self.book + suffix).status_code, 404)
        self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 404)
        self.assert_integrity()

    def test_empty_and_preview_only_book_delete_all_sessions_previews(self):
        empty = self.create_book("空书")
        self.assertEqual(self.client.post(empty + "/delete", data=self.payload).status_code, 303)
        preview_book = self.create_book("预览书")
        preview = self.upload(preview_book).headers["location"]
        with TestClient(self.app, follow_redirects=False) as other:
            self.login(other)
            self.upload(preview_book, client=other)
            self.assertEqual(self.db_count("previews"), 2)
            self.assertEqual(self.client.post(preview_book + "/delete", data=self.payload).status_code, 303)
            self.assertEqual(other.get(preview).status_code, 404)
        self.assertEqual(self.db_count("previews"), 0)
        self.assertEqual(self.db_count("sessions"), 2)
        self.assertEqual(self.db_count("books"), 1)
        self.assert_integrity()

    def test_note_next_previous_empty_statistics_and_export(self):
        self.client.post(f"/api{self.book}/notes/{self.notes[1]['id']}", data={
            "csrf": self.token(), "second_comment": "二读二", "status": "done",
        })
        deleted = self.client.post(self.note_urls[1], data=self.payload)
        self.assertEqual(deleted.headers["location"], f"{self.book}?note={self.notes[2]['id']}")
        page = self.client.get(deleted.headers["location"]).text
        self.assertIn("笔记 2 / 2", page)
        self.assertIn('id="book-progress">0/2', page)
        exported = self.client.get(self.book + "/export")
        self.assertNotIn("原文二", exported.text)
        self.assertNotIn("二读二", exported.text)
        self.assertIn("原文一", exported.text)
        self.assertIn("原文三", exported.text)
        self.assertEqual(exported.headers["X-Noteshub-Pending"], "2")
        self.assertIn("已完成 0/2", self.client.get("/").text)
        deleted = self.client.post(self.note_urls[2], data=self.payload)
        self.assertEqual(deleted.headers["location"], f"{self.book}?note={self.notes[0]['id']}")
        deleted = self.client.post(self.note_urls[0], data=self.payload)
        self.assertEqual(deleted.headers["location"], self.book)
        page = self.client.get(self.book).text
        self.assertIn("当前没有笔记", page)
        self.assertIn('0/0', page)
        self.assertNotIn("全部笔记已完成", page)
        self.assertNotIn('id="review-editor"', page)
        self.assertNotIn('id="export-book"', page)
        self.assertEqual(self.client.get(self.book + "/export").status_code, 409)
        self.assertEqual(self.client.get(self.book + "/source").content, self.raw)
        self.assertEqual(self.db_count("imports"), 1)
        self.assertEqual(self.upload(self.book, self.raw).status_code, 409)
        Store(self.settings)
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 303)
        self.assertEqual(list(self.store.sources.iterdir()), [])
        self.assert_integrity()

    def test_first_note_deletion_selects_next_and_pending_changes_counts(self):
        self.client.post(f"/api{self.book}/notes/{self.notes[2]['id']}", data={
            "csrf": self.token(), "second_comment": "保留的二读", "status": "done",
        })
        response = self.client.post(self.note_urls[0], data=self.payload)
        self.assertEqual(response.headers["location"], f"{self.book}?note={self.notes[1]['id']}")
        self.assertIn("已完成 1/2", self.client.get("/").text)
        self.assertIn("保留的二读", self.client.get(self.book + "/export").text)
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_wrong_book_repeated_delete_and_stale_save_do_not_affect_survivors(self):
        other = self.create_book("其他书")
        wrong = f"{other}/notes/{self.notes[0]['id']}/delete"
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertEqual(self.client.post(wrong, data=self.payload).status_code, 404)
        self.client.post(self.note_urls[0], data=self.payload)
        self.assertEqual(self.client.post(self.note_urls[0], data=self.payload).status_code, 404)
        self.assertEqual(self.client.post(f"/api{self.book}/notes/{self.notes[0]['id']}", data={
            "csrf": self.token(), "second_comment": "过期页面",
        }).status_code, 404)
        self.assertEqual(self.db_count("notes"), 2)

    def test_book_database_failure_restores_file_and_all_rows(self):
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON books BEGIN SELECT RAISE(ABORT, 'injected'); END")
        self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 503)
        self.assertEqual(self.db_count("notes"), 3)
        self.assertEqual(self.db_count("imports"), 1)
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(list(self.store.sources.iterdir()), [self.source])
        self.assert_integrity()

    def test_book_commit_failure_restores_file(self):
        connect = sqlite3.connect
        failed = False

        class FailCommit(sqlite3.Connection):
            def commit(connection):
                nonlocal failed
                if not failed and not connection.execute("SELECT 1 FROM books").fetchone():
                    failed = True
                    raise sqlite3.OperationalError("injected commit failure")
                return super().commit()

        with patch("noteshub.db.sqlite3.connect", side_effect=lambda *args, **kwargs: connect(*args, **kwargs, factory=FailCommit)):
            self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 503)
        self.assertTrue(failed)
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(self.db_count("notes"), 3)
        self.assert_integrity()

    def test_file_rename_failure_preserves_book(self):
        with patch.object(Path, "rename", side_effect=PermissionError("injected rename failure")):
            self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 503)
        self.assertEqual(self.db_count("notes"), 3)
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_directory_sync_failure_rolls_back_and_recovery_can_retry(self):
        # First call: preflight recovery. Second: rename durability barrier.
        with patch.object(self.store, "sync_sources", side_effect=[OSError("injected sync failure"), None]):
            self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 503)
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.assertEqual(self.db_count("notes"), 3)
        self.assertEqual(list(self.store.sources.iterdir()), [self.source])

    def test_preexisting_missing_source_does_not_prevent_removing_book(self):
        self.source.unlink()
        self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 303)
        self.assertEqual(self.db_count("books"), 0)
        self.assertEqual(self.db_count("imports"), 0)
        self.assertEqual(list(self.store.sources.iterdir()), [])
        self.assert_integrity()

    def test_source_download_holds_write_lock_until_bytes_are_read(self):
        read_bytes = Path.read_bytes
        reading, release = Event(), Event()

        def hold_read(path):
            if path == self.source:
                reading.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release source read")
            return read_bytes(path)

        with patch.object(Path, "read_bytes", hold_read), ThreadPoolExecutor(max_workers=1) as executor:
            download = executor.submit(self.client.get, self.book + "/source")
            try:
                self.assertTrue(reading.wait(5))
                with closing(sqlite3.connect(self.store.root / "noteshub.sqlite3", timeout=.05)) as contender, contender:
                    with self.assertRaises(sqlite3.OperationalError):
                        contender.execute("BEGIN IMMEDIATE")
            finally:
                release.set()
            self.assertEqual(download.result().content, self.raw)
        contender.close()
        self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 303)

    def test_file_purge_failure_is_reported_and_cleanup_retries(self):
        unlink = Path.unlink

        def fail_staged(path, *args, **kwargs):
            if path.name.endswith(".deleting"):
                raise PermissionError("injected purge failure")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_staged):
            response = self.client.post(self.book + "/delete", data=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertIn("残留源文件", response.text)
        self.assertEqual(self.db_count("books"), 0)
        self.assertEqual(self.db_count("imports"), 0)
        self.assertEqual(len(list(self.store.sources.glob("*.deleting"))), 1)
        self.store.cleanup()
        self.assertEqual(list(self.store.sources.iterdir()), [])
        self.assert_integrity()

    def test_startup_recovers_precommit_and_postcommit_staging(self):
        staged = self.source.with_name(self.source.name + ".deleting")
        self.source.rename(staged)
        Store(self.settings)
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.source.rename(staged)
        with self.store.connection() as db:
            db.execute("DELETE FROM notes")
            db.execute("DELETE FROM imports")
            db.execute("DELETE FROM books")
        Store(self.settings)
        self.assertFalse(staged.exists())
        self.assertEqual(list(self.store.sources.iterdir()), [])

    def test_note_database_failure_rolls_back(self):
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON notes BEGIN SELECT RAISE(ABORT, 'injected'); END")
        self.assertEqual(self.client.post(self.note_urls[0], data=self.payload).status_code, 503)
        self.assertEqual(self.db_count("notes"), 3)
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_invalid_source_path_refuses_deletion(self):
        outside = self.store.root / "keep.md"
        outside.write_bytes(b"keep")
        with self.store.connection() as db:
            db.execute("UPDATE imports SET original_file_path = '../keep.md'")
        self.assertEqual(self.client.post(self.book + "/delete", data=self.payload).status_code, 503)
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertEqual(self.db_count("notes"), 3)

    def test_concurrent_book_delete_and_note_delete_serialize(self):
        def delete_note():
            try:
                self.store.delete_note(self.book_id, self.notes[0]["id"])
            except LookupError:
                pass  # The book deletion may acquire the lock first.

        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = [executor.submit(delete_note), executor.submit(self.store.delete_book, self.book_id)]
            for task in tasks:
                task.result()
        self.assertEqual(self.db_count("books"), 0)
        self.assertEqual(self.db_count("notes"), 0)
        self.assertEqual(list(self.store.sources.iterdir()), [])
        self.assert_integrity()

    def test_backup_after_note_deletion_contains_survivors_and_exact_source(self):
        self.client.post(self.note_urls[1], data=self.payload)
        archive = io.BytesIO()
        backup(self.store.root, archive)
        destination = self.store.root / "restored"
        restore(destination, io.BytesIO(archive.getvalue()))
        restored = Store(type(self.settings)(destination, cookie_secure=False))
        with restored.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT 1 FROM notes WHERE id = ?", (self.notes[1]["id"],)).fetchone())
        self.assertEqual((restored.sources / self.source.name).read_bytes(), self.raw)
