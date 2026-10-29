import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from noteshub.config import Settings
from noteshub.maintenance import backup, restore
from scripts.package_release import FILES, package
from tests.test_web import WebCase


class BackupTests(WebCase):
    def setUp(self):
        super().setUp()
        self.book = self.create_book()
        preview = self.upload(self.book).headers["location"]
        self.client.post(preview + "/confirm", data={"csrf": self.token()})
        with self.store.connection() as db:
            note = db.execute("SELECT id FROM notes").fetchone()[0]
        self.client.post(f"/api{self.book}/notes/{note}", data={
            "csrf": self.token(), "second_comment": "备份中的二读", "status": "done",
        })
        # An unfinished preview must not restore as an active login/upload.
        other = self.create_book("待录入")
        self.upload(other, "“异常".encode())

    def archive(self):
        target = io.BytesIO()
        backup(self.settings.data_dir, target)
        return target.getvalue()

    def test_backup_restore_preserves_content_and_expires_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "restored"
            restore(root, io.BytesIO(self.archive()))
            with sqlite3.connect(root / "noteshub.sqlite3") as db:
                self.assertEqual(db.execute("SELECT second_comment, status FROM notes").fetchone(), ("备份中的二读", "done"))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM books").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM previews").fetchone()[0], 0)
            self.assertEqual((root / "account.json").read_bytes(), (self.settings.data_dir / "account.json").read_bytes())
            self.assertEqual([p.read_bytes() for p in (root / "sources").iterdir()],
                             [p.read_bytes() for p in self.store.sources.iterdir()])

    def test_restore_does_not_overwrite_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "marker").write_text("keep")
            with self.assertRaises(ValueError):
                restore(root, io.BytesIO(self.archive()))
            self.assertEqual(list(root.iterdir()), [root / "marker"])
            self.assertEqual((root / "marker").read_text(), "keep")

    def test_corrupt_file_rejected_and_target_left_empty(self):
        changed = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(self.archive()), mode="r:gz") as source, tarfile.open(fileobj=changed, mode="w:gz") as target:
            for member in source:
                content = source.extractfile(member).read()
                if member.name.startswith("sources/"):
                    content += b"tampered"
                member.size = len(content)
                target.addfile(member, io.BytesIO(content))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                restore(Path(tmp), io.BytesIO(changed.getvalue()))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_unsafe_tar_members_rejected(self):
        for name, kind in (("../escaped", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("account.json", tarfile.SYMTYPE), ("account.json", tarfile.LNKTYPE)):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as tmp:
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w:gz") as target:
                    item = tarfile.TarInfo(name)
                    item.type, item.linkname = kind, "../../outside"
                    target.addfile(item)
                with self.assertRaises(ValueError):
                    restore(Path(tmp), io.BytesIO(archive.getvalue()))
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_capacity_limit_and_missing_source_fail_safely(self):
        data = self.archive()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                restore(Path(tmp), io.BytesIO(data), max_bytes=10)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        next(self.store.sources.iterdir()).unlink()
        target = io.BytesIO()
        with self.assertRaises(ValueError):
            backup(self.settings.data_dir, target)
        self.assertEqual(target.getvalue(), b"")

    def test_wal_updates_are_included_in_backup(self):
        db = sqlite3.connect(self.settings.data_dir / "noteshub.sqlite3")
        try:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("UPDATE notes SET second_comment = 'WAL 中的新评述'")
            db.commit()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                restore(root, io.BytesIO(self.archive()))
                with sqlite3.connect(root / "noteshub.sqlite3") as restored:
                    self.assertEqual(restored.execute("SELECT second_comment FROM notes").fetchone()[0], "WAL 中的新评述")
        finally:
            db.close()


class PackagingTests(unittest.TestCase):
    def test_release_has_no_runtime_data_or_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            for name in FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("deployment file")
            (root / "noteshub").mkdir()
            (root / "noteshub/__init__.py").write_text('"""app"""')
            for name in (".env", "data/account.json", "tests/fixtures/private.md", ".git/config"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("PRIVATE_TEST_MARKER")
            output = Path(tmp) / "release.tar.gz"
            package(root, output)
            with tarfile.open(output) as archive:
                names = archive.getnames()
                self.assertIn("Dockerfile", names)
                self.assertIn("noteshub/__init__.py", names)
                for name in names:
                    self.assertFalse(name.startswith(("data/", "tests/", ".git/")))
                    self.assertNotEqual(name, ".env")
                    self.assertNotIn(b"PRIVATE_TEST_MARKER", archive.extractfile(name).read())
                    self.assertEqual(archive.getmember(name).mode, 0o644)
            with self.assertRaises(FileExistsError):
                package(root, output)
