"""SQLite schema v1 and transactional import storage.

SQLite's standard-library driver is sufficient for the current three core
entities. Each connection enables foreign keys; writes serialize explicitly.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from noteshub.config import Settings
from noteshub.parser import confirm_preview, correct_record, parse_markdown


SCHEMA = """
CREATE TABLE books (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE previews (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL, source BLOB NOT NULL, corrections TEXT NOT NULL DEFAULT '{}',
    expires_at INTEGER NOT NULL
);
CREATE INDEX previews_expiry ON previews(expires_at);
CREATE TABLE imports (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL UNIQUE REFERENCES books(id),
    filename TEXT NOT NULL, sha256 TEXT NOT NULL, original_file_path TEXT NOT NULL UNIQUE,
    parser_version TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE notes (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
    import_id TEXT NOT NULL REFERENCES imports(id), source_type TEXT NOT NULL DEFAULT 'zotero',
    original_text TEXT NOT NULL, first_comment TEXT NOT NULL,
    second_comment TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'done')),
    raw_source TEXT NOT NULL, sort_order INTEGER NOT NULL,
    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    manually_corrected INTEGER NOT NULL CHECK(manually_corrected IN (0, 1)),
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE(book_id, sort_order)
);
CREATE TABLE login_guard (
    id INTEGER PRIMARY KEY CHECK(id = 1), failures INTEGER NOT NULL,
    blocked_until INTEGER NOT NULL
);
INSERT INTO login_guard VALUES (1, 0, 0);
PRAGMA user_version = 1;
"""


class ImportConflict(ValueError):
    pass


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir
        self.sources = self.root / "sources"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sources.mkdir(exist_ok=True, mode=0o700)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
            elif version != 1:
                raise RuntimeError(f"不支持的数据库版本：{version}")
        self.cleanup()
        self.recover_sources()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root / "noteshub.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA secure_delete = ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def cleanup(self):
        now = int(time.time())
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            db.execute("DELETE FROM previews WHERE expires_at <= ?", (now,))

    def recover_sources(self):
        # A process crash between file creation and commit can leave an orphan.
        # The write lock prevents racing a different worker's active import.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            retained = {row[0] for row in db.execute("SELECT original_file_path FROM imports")}
            for path in self.sources.iterdir():
                if re.fullmatch(r"[0-9a-f]{32}\.md", path.name) and path.name not in retained:
                    path.unlink()

    @staticmethod
    def preview(row):
        preview = parse_markdown(bytes(row["source"]).decode("utf-8"))
        for record_id, fields in json.loads(row["corrections"]).items():
            preview = correct_record(preview, int(record_id), **fields)
        return preview

    def confirm(self, book_id: str, preview_id: str, session_id: str) -> int:
        destination: Path | None = None
        created = False
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM imports WHERE book_id = ?", (book_id,)).fetchone():
                    raise ImportConflict("这本书已经成功导入过，不能再次导入。")
                row = db.execute(
                    "SELECT * FROM previews WHERE id = ? AND book_id = ? AND session_id = ? AND expires_at > ?",
                    (preview_id, book_id, session_id, int(time.time())),
                ).fetchone()
                if row is None:
                    raise LookupError("预览不存在或已过期，请重新上传。")
                preview = self.preview(row)
                notes = confirm_preview(preview)
                import_id = uuid.uuid4().hex
                destination = self.sources / f"{import_id}.md"
                # Names never incorporate the uploaded filename.
                with destination.open("xb") as target:
                    created = True
                    target.write(row["source"])
                    target.flush()
                    os.fsync(target.fileno())
                directory_fd = os.open(self.sources, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                now = int(time.time())
                db.execute(
                    "INSERT INTO imports VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (import_id, book_id, row["filename"], preview.sha256,
                     destination.name, "curly-quotes-v1", now),
                )
                db.executemany(
                    """INSERT INTO notes
                    (id, book_id, import_id, original_text, first_comment, raw_source,
                     sort_order, start_line, end_line, manually_corrected, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(uuid.uuid4().hex, book_id, import_id, note.original_text,
                      note.first_comment, note.raw_source, index, note.start_line,
                      note.end_line, int(note.manually_corrected), now, now)
                     for index, note in enumerate(notes)],
                )
                db.execute("DELETE FROM previews WHERE book_id = ?", (book_id,))
            return len(notes)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
