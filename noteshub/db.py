"""Versioned SQLite storage for imports and private image drafts.

SQLite's standard-library driver is sufficient for the current three core
entities. Each connection enables foreign keys; writes serialize explicitly.
"""

from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from noteshub.paper_schema import PAPER_SCHEMA
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

IMAGE_SCHEMA = """
CREATE TABLE image_drafts (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id),
    file_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type IN ('image/png', 'image/jpeg')),
    byte_size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    UNIQUE(book_id, sha256)
);
CREATE INDEX image_drafts_expiry ON image_drafts(expires_at);
PRAGMA user_version = 2;
"""

OCR_SCHEMA = """
ALTER TABLE image_drafts ADD COLUMN editing_started INTEGER NOT NULL DEFAULT 0
    CHECK(editing_started IN (0, 1));
ALTER TABLE image_drafts ADD COLUMN original_text TEXT NOT NULL DEFAULT '';
ALTER TABLE image_drafts ADD COLUMN first_comment TEXT NOT NULL DEFAULT '';
ALTER TABLE image_drafts ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE image_drafts ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;
CREATE TABLE ocr_runs (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES image_drafts(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('running', 'succeeded', 'failed')),
    started_at INTEGER NOT NULL, finished_at INTEGER,
    text TEXT, response_json TEXT, request_id TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ocr_runs_draft ON ocr_runs(draft_id);
CREATE UNIQUE INDEX ocr_single_active ON ocr_runs((1)) WHERE state = 'running';
PRAGMA user_version = 3;
"""

ARCHIVE_SCHEMA = """
CREATE UNIQUE INDEX image_draft_identity ON image_drafts(id, book_id);
CREATE TABLE notes_v4 (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
    import_id TEXT REFERENCES imports(id),
    source_type TEXT NOT NULL DEFAULT 'zotero' CHECK(source_type IN ('zotero', 'image')),
    original_text TEXT NOT NULL, first_comment TEXT NOT NULL,
    second_comment TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'done')),
    raw_source TEXT NOT NULL, sort_order INTEGER NOT NULL,
    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    manually_corrected INTEGER NOT NULL CHECK(manually_corrected IN (0, 1)),
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    image_draft_id TEXT UNIQUE,
    UNIQUE(book_id, sort_order),
    FOREIGN KEY(image_draft_id, book_id) REFERENCES image_drafts(id, book_id) ON DELETE RESTRICT,
    CHECK((source_type = 'zotero' AND import_id IS NOT NULL AND image_draft_id IS NULL)
       OR (source_type = 'image' AND import_id IS NULL AND image_draft_id IS NOT NULL))
);
INSERT INTO notes_v4
    (id, book_id, import_id, source_type, original_text, first_comment, second_comment,
     status, raw_source, sort_order, start_line, end_line, manually_corrected, created_at, updated_at)
SELECT id, book_id, import_id, source_type, original_text, first_comment, second_comment,
       status, raw_source, sort_order, start_line, end_line, manually_corrected, created_at, updated_at
FROM notes;
DROP TABLE notes;
ALTER TABLE notes_v4 RENAME TO notes;
PRAGMA user_version = 4;
"""


class ImportConflict(ValueError):
    pass


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir
        self.sources = self.root / "sources"
        self.images = self.root / "images"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sources.mkdir(exist_ok=True, mode=0o700)
        self.images.mkdir(exist_ok=True, mode=0o700)
        (self.root / "paper-images").mkdir(exist_ok=True, mode=0o700)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
                version = 1
            if version == 1:
                db.executescript("BEGIN IMMEDIATE;" + IMAGE_SCHEMA + "COMMIT;")
                version = 2
            if version == 2:
                db.executescript("BEGIN IMMEDIATE;" + OCR_SCHEMA + "COMMIT;")
                version = 3
            if version == 3:
                db.executescript("BEGIN IMMEDIATE;" + ARCHIVE_SCHEMA + "COMMIT;")
                version = 4
            if version == 4:
                db.executescript("BEGIN IMMEDIATE;" + PAPER_SCHEMA + "COMMIT;")
            elif version != 5:
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
            # Never replay a possibly charged request after a crash/timeout.
            db.execute("UPDATE ocr_runs SET state = 'failed', error_code = 'Interrupted', finished_at = ? "
                       "WHERE state = 'running' AND started_at <= ?", (now, now - 120))
            db.execute("DELETE FROM image_drafts WHERE editing_started = 0 AND expires_at <= ? "
                       "AND NOT EXISTS (SELECT 1 FROM ocr_runs WHERE draft_id = image_drafts.id AND state = 'running') "
                       "AND NOT EXISTS (SELECT 1 FROM notes WHERE image_draft_id = image_drafts.id)", (now,))
        # Remove only files unreferenced by committed rows. A write lock also
        # prevents racing a draft writer between file creation and commit.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            retained = {row[0] for row in db.execute("SELECT file_path FROM image_drafts")}
            for path in self.images.iterdir():
                if re.fullmatch(r"[0-9a-f]{32}\.(png|jpg)", path.name) and f"images/{path.name}" not in retained:
                    path.unlink(missing_ok=True)

    def save_image_draft(self, book_id, raw, metadata):
        """Idempotent per book/content; never create notes or touch imports."""
        checksum = hashlib.sha256(raw).hexdigest()
        destination = None
        created = False
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone():
                    raise LookupError("书籍不存在。")
                existing = db.execute(
                    "SELECT id FROM image_drafts WHERE book_id = ? AND sha256 = ? AND (editing_started = 1 OR expires_at > ? "
                    "OR EXISTS (SELECT 1 FROM notes WHERE image_draft_id = image_drafts.id))",
                    (book_id, checksum, int(time.time())),
                ).fetchone()
                if existing:
                    return existing["id"]
                # A concurrently expired duplicate must not violate uniqueness.
                db.execute("DELETE FROM image_drafts WHERE book_id = ? AND sha256 = ?", (book_id, checksum))
                draft_id = uuid.uuid4().hex
                media_type, extension, width, height = metadata
                relative = f"images/{draft_id}.{extension}"
                destination = self.root / relative
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                with os.fdopen(fd, "wb") as target:
                    target.write(raw)
                    target.flush()
                    os.fsync(target.fileno())
                directory_fd = os.open(self.images, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                now = int(time.time())
                db.execute(
                    "INSERT INTO image_drafts (id, book_id, file_path, sha256, media_type, byte_size, width, height, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (draft_id, book_id, relative, checksum, media_type, len(raw), width, height,
                     now, now + self.settings.image_draft_seconds),
                )
            return draft_id
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise

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
                offset = db.execute("SELECT COALESCE(MAX(sort_order) + 1, 0) FROM notes WHERE book_id = ?", (book_id,)).fetchone()[0]
                db.executemany(
                    """INSERT INTO notes
                    (id, book_id, import_id, original_text, first_comment, raw_source,
                     sort_order, start_line, end_line, manually_corrected, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(uuid.uuid4().hex, book_id, import_id, note.original_text,
                      note.first_comment, note.raw_source, index + offset, note.start_line,
                      note.end_line, int(note.manually_corrected), now, now)
                     for index, note in enumerate(notes)],
                )
                db.execute("DELETE FROM previews WHERE book_id = ?", (book_id,))
            return len(notes)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
