"""Durable OCR attempts and revision-checked first-reading drafts."""
import asyncio
import hashlib
import logging
import re
import sqlite3
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from noteshub.ocr import ERRORS, OCRFailure, OCRResult


class DraftConflict(ValueError):
    pass


def find_draft(db, book_id, draft_id):
    row = db.execute(
        'SELECT * FROM image_drafts WHERE id = ? AND book_id = ? AND (editing_started = 1 OR expires_at > ?)',
        (draft_id, book_id, int(time.time())),
    ).fetchone()
    if row is None:
        raise LookupError('图片草稿不存在、已取消或已过期。')
    return row


def draft_state(db, book_id, draft_id):
    row = find_draft(db, book_id, draft_id)
    latest = db.execute('SELECT * FROM ocr_runs WHERE draft_id = ? ORDER BY rowid DESC LIMIT 1', (draft_id,)).fetchone()
    first = db.execute("SELECT text FROM ocr_runs WHERE draft_id = ? AND state = 'succeeded' ORDER BY rowid LIMIT 1", (draft_id,)).fetchone()
    last_success = db.execute("SELECT text FROM ocr_runs WHERE draft_id = ? AND state = 'succeeded' ORDER BY rowid DESC LIMIT 1", (draft_id,)).fetchone()
    return {
        'id': row['id'], 'original_text': row['original_text'], 'first_comment': row['first_comment'],
        'revision': row['revision'], 'editing_started': bool(row['editing_started']),
        'ocr_state': latest['state'] if latest else 'idle', 'run_id': latest['id'] if latest else '',
        'first_ocr_text': first['text'] if first else None,
        'latest_ocr_text': last_success['text'] if last_success else None,
        'request_id': latest['request_id'] if latest else '',
        'error': ERRORS.get(latest['error_code'], ERRORS['InternalError']) if latest and latest['state'] == 'failed' else '',
    }


class DraftService:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.tasks = set()

    def state(self, book_id, draft_id):
        with self.store.connection() as db:
            now = int(time.time())
            db.execute("UPDATE ocr_runs SET state = 'failed', error_code = 'Interrupted', finished_at = ? "
                       "WHERE state = 'running' AND started_at <= ?", (now, now - 120))
            return draft_state(db, book_id, draft_id)

    def retain(self, book_id, draft_id):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            find_draft(db, book_id, draft_id)
            db.execute('UPDATE image_drafts SET editing_started = 1 WHERE id = ?', (draft_id,))
            return draft_state(db, book_id, draft_id)

    def save(self, book_id, draft_id, original, comment, revision):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = find_draft(db, book_id, draft_id)
            same = row['original_text'] == original and row['first_comment'] == comment
            if row['revision'] != revision and not same:
                raise DraftConflict('草稿已在另一页面更新。当前输入仍保留，请复制后刷新核对，系统不会覆盖较新的内容。')
            if not same:
                db.execute('UPDATE image_drafts SET original_text = ?, first_comment = ?, revision = revision + 1, '
                           'editing_started = 1, updated_at = ? WHERE id = ?', (original, comment, int(time.time()), draft_id))
            else:
                db.execute('UPDATE image_drafts SET editing_started = 1 WHERE id = ?', (draft_id,))
            return draft_state(db, book_id, draft_id)

    def claim(self, book_id, draft_id, previous):
        # Check existence/ownership before reading any credentials.
        self.state(book_id, draft_id)
        self.client.prepare()
        now = int(time.time())
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            find_draft(db, book_id, draft_id)
            db.execute("UPDATE ocr_runs SET state = 'failed', error_code = 'Interrupted', finished_at = ? "
                       "WHERE state = 'running' AND started_at <= ?", (now, now - 120))
            latest = db.execute('SELECT id, state FROM ocr_runs WHERE draft_id = ? ORDER BY rowid DESC LIMIT 1', (draft_id,)).fetchone()
            if previous != (latest['id'] if latest else ''):
                raise DraftConflict('识别状态已更新，请刷新状态；没有重复提交 OCR。')
            if latest and latest['state'] == 'running':
                raise DraftConflict('当前图片正在识别，请等待结果。')
            if db.execute("SELECT 1 FROM ocr_runs WHERE state = 'running'").fetchone():
                raise DraftConflict('正在识别另一张图片，请完成后手动重试。')
            run_id = uuid.uuid4().hex
            db.execute("INSERT INTO ocr_runs (id, draft_id, state, started_at) VALUES (?, ?, 'running', ?)", (run_id, draft_id, now))
            db.execute('UPDATE image_drafts SET editing_started = 1 WHERE id = ?', (draft_id,))
        return run_id

    def image_bytes(self, draft_id):
        with self.store.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM image_drafts WHERE id = ?', (draft_id,)).fetchone()
            if row is None or not re.fullmatch(r'images/[0-9a-f]{32}\.(png|jpg)', row['file_path']):
                raise OCRFailure('ImageUnavailable')
            try:
                raw = (self.store.root / row['file_path']).read_bytes()
            except OSError:
                raise OCRFailure('ImageUnavailable') from None
            if len(raw) > self.store.settings.max_image_bytes or len(raw) != row['byte_size'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise OCRFailure('ImageUnavailable')
            return raw

    def finish(self, run_id, result=None, code='InternalError'):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute("SELECT * FROM ocr_runs WHERE id = ? AND state = 'running'", (run_id,)).fetchone()
            if run is None:
                return  # A stale/interrupted worker must not overwrite a later attempt.
            if result is None:
                db.execute("UPDATE ocr_runs SET state = 'failed', error_code = ?, finished_at = ? WHERE id = ?", (code if code in ERRORS else 'InternalError', int(time.time()), run_id))
                return
            first = not db.execute("SELECT 1 FROM ocr_runs WHERE draft_id = ? AND state = 'succeeded'", (run['draft_id'],)).fetchone()
            db.execute("UPDATE ocr_runs SET state = 'succeeded', text = ?, response_json = ?, request_id = ?, finished_at = ? WHERE id = ?",
                       (result.text, result.response_json, result.request_id, int(time.time()), run_id))
            # Only the first success can seed a never-edited original. Comments
            # and later manual corrections are never changed by an OCR job.
            if first:
                db.execute('UPDATE image_drafts SET original_text = ?, revision = revision + 1, updated_at = ? '
                           'WHERE id = ? AND revision = 0', (result.text, int(time.time()), run['draft_id']))

    async def run(self, run_id, draft_id):
        try:
            raw = await asyncio.to_thread(self.image_bytes, draft_id)
            result = await asyncio.to_thread(self.client.recognize, raw)
            if not isinstance(result, OCRResult):
                raise OCRFailure('InvalidResponse')
            await asyncio.to_thread(self.finish, run_id, result)
        except OCRFailure as exc:
            await self.fail(run_id, exc.code)
        except (OSError, sqlite3.Error):
            await self.fail(run_id, 'Interrupted')
        except Exception:
            await self.fail(run_id, 'InternalError')

    async def fail(self, run_id, code):
        try:
            await asyncio.to_thread(self.finish, run_id, None, code)
        except (OSError, sqlite3.Error):
            # Do not log raw provider/transport exceptions or replay a paid call.
            logging.getLogger(__name__).warning('OCR 状态保存失败，需等待状态超时后人工核对。')

    def launch(self, run_id, draft_id):
        task = asyncio.create_task(self.run(run_id, draft_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close(self):
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


def install_routes(app, service, *, render, require_session, read_form, field):
    async def operation(function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except DraftConflict as exc:
            raise HTTPException(409, str(exc))
        except OCRFailure as exc:
            raise HTTPException(503, str(exc))
        except (OSError, sqlite3.Error):
            raise HTTPException(503, '草稿保存或读取失败，请稍后重试。')

    @app.get('/books/{book_id}/image-drafts/{draft_id}/edit')
    async def edit(request: Request, book_id: str, draft_id: str):
        require_session(request)
        state = await operation(service.state, book_id, draft_id)
        with service.store.connection() as db:
            book = db.execute('SELECT * FROM books WHERE id = ?', (book_id,)).fetchone()
        return render(request, 'draft.html', book=book, draft=state, ocr_configured=service.client.configured())

    @app.get('/api/books/{book_id}/image-drafts/{draft_id}')
    async def get_state(request: Request, book_id: str, draft_id: str):
        require_session(request)
        return JSONResponse(await operation(service.state, book_id, draft_id))

    @app.post('/api/books/{book_id}/image-drafts/{draft_id}/open')
    async def open_editor(request: Request, book_id: str, draft_id: str):
        async with read_form(request):
            state = await operation(service.retain, book_id, draft_id)
        return JSONResponse(state)

    @app.post('/api/books/{book_id}/image-drafts/{draft_id}/save')
    async def save(request: Request, book_id: str, draft_id: str):
        async with read_form(request) as form:
            if 'original_text' not in form or 'first_comment' not in form:
                raise HTTPException(422, '缺少原文或一读内容；清空时也需明确提交。')
            original = field(form, 'original_text', max_length=50000)
            comment = field(form, 'first_comment', max_length=50000)
            revision = field(form, 'revision', max_length=20)
            if not revision.isascii() or not revision.isdecimal() or '\x00' in original or '\x00' in comment:
                raise HTTPException(422, '草稿内容或版本无效。')
            state = await operation(service.save, book_id, draft_id, original, comment, int(revision))
        return JSONResponse(state)

    @app.post('/api/books/{book_id}/image-drafts/{draft_id}/ocr')
    async def recognize(request: Request, book_id: str, draft_id: str):
        async with read_form(request) as form:
            previous = field(form, 'previous_run_id', max_length=64)
            run_id = await operation(service.claim, book_id, draft_id, previous)
        service.launch(run_id, draft_id)
        return JSONResponse({'run_id': run_id, 'ocr_state': 'running'}, status_code=202)
