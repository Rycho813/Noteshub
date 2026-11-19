"""Additive V3 storage; v4 Markdown/image rows retain every existing value."""

PAPER_SCHEMA = """
CREATE TABLE notes_v5 (
 id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
 import_id TEXT REFERENCES imports(id),
 source_type TEXT NOT NULL DEFAULT 'zotero' CHECK(source_type IN ('zotero','image','paper')),
 original_text TEXT NOT NULL, first_comment TEXT NOT NULL,
 second_comment TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done')),
 raw_source TEXT NOT NULL, sort_order INTEGER NOT NULL,
 start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 manually_corrected INTEGER NOT NULL CHECK(manually_corrected IN (0,1)),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, image_draft_id TEXT UNIQUE,
 UNIQUE(book_id,sort_order),
 FOREIGN KEY(image_draft_id,book_id) REFERENCES image_drafts(id,book_id) ON DELETE RESTRICT,
 CHECK((source_type='zotero' AND import_id IS NOT NULL AND image_draft_id IS NULL)
 OR (source_type='image' AND import_id IS NULL AND image_draft_id IS NOT NULL)
 OR (source_type='paper' AND import_id IS NULL AND image_draft_id IS NULL))
);
INSERT INTO notes_v5 SELECT * FROM notes;
DROP TABLE notes;
ALTER TABLE notes_v5 RENAME TO notes;
CREATE TABLE paper_workspace (id TEXT PRIMARY KEY);
INSERT INTO paper_workspace VALUES (lower(hex(randomblob(16))));
CREATE TABLE paper_entries (
 note_id TEXT PRIMARY KEY REFERENCES notes(id), revision INTEGER NOT NULL,
 confirmed_revision INTEGER, confirmed_at INTEGER
);
CREATE TABLE paper_photos (
 id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES paper_entries(note_id),
 file_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, media_type TEXT NOT NULL,
 byte_size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
 created_at INTEGER NOT NULL, purge_after INTEGER, purged_at INTEGER,
 UNIQUE(id,note_id)
);
CREATE TABLE paper_pages (
 note_id TEXT NOT NULL REFERENCES paper_entries(note_id), photo_id TEXT NOT NULL UNIQUE,
 position INTEGER NOT NULL, PRIMARY KEY(note_id,position),
 FOREIGN KEY(photo_id,note_id) REFERENCES paper_photos(id,note_id)
);
CREATE TABLE paper_mutations (
 id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES paper_entries(note_id),
 payload_sha TEXT NOT NULL, response TEXT NOT NULL
);
PRAGMA user_version = 5;
"""

LOCAL_OCR_SCHEMA = """
CREATE TABLE paper_worker_tokens (
 id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
 created_at INTEGER NOT NULL, last_used_at INTEGER, revoked_at INTEGER
);
CREATE TABLE paper_ocr_jobs (
 id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES paper_entries(note_id),
 revision INTEGER NOT NULL, photos_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed')),
 created_at INTEGER NOT NULL, finished_at INTEGER,
 worker_id TEXT REFERENCES paper_worker_tokens(id), lease_hash TEXT, lease_until INTEGER,
 engine TEXT NOT NULL DEFAULT '', results_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX paper_ocr_active ON paper_ocr_jobs(note_id) WHERE state IN ('pending','running');
CREATE TABLE paper_worker_claims (
 id TEXT PRIMARY KEY, worker_id TEXT NOT NULL REFERENCES paper_worker_tokens(id),
 job_id TEXT REFERENCES paper_ocr_jobs(id), lease_hash TEXT, created_at INTEGER NOT NULL
);
CREATE INDEX paper_photos_note ON paper_photos(note_id);
CREATE INDEX paper_ocr_queue ON paper_ocr_jobs(state,created_at);
CREATE INDEX paper_ocr_history ON paper_ocr_jobs(note_id,created_at);
PRAGMA user_version = 6;
"""
