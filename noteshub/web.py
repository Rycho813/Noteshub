"""Server-rendered single-user book and import workflow."""

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import hmac
import json
import logging
from pathlib import Path
from urllib.parse import quote
import secrets
import sqlite3
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from noteshub.auth import load_account, verify
from noteshub.config import Settings
from noteshub.db import ImportConflict, Store
from noteshub.exporter import export_filename, render_book
from noteshub.parser import correct_record, parse_markdown


PACKAGE = Path(__file__).parent
COOKIE = "noteshub_session"


class BodyLimitMiddleware:
    """Bound actual request bytes, even for chunked/false-length uploads."""

    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                response = HTMLResponse("上传或表单过大，请缩小后重试。", status_code=413)
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    account = load_account(settings.data_dir)
    store = Store(settings)
    templates = Jinja2Templates(directory=PACKAGE / "templates")

    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(store.cleanup)
            except sqlite3.Error:
                logging.getLogger(__name__).warning("临时预览清理失败，将在下一轮重试。")

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_upload_bytes + 64 * 1024)

    @app.middleware("http")
    async def request_context(request, call_next):
        request.state.session = None
        token = request.cookies.get(COOKIE, "")
        if token and len(token) <= 128:
            session_id = hashlib.sha256(token.encode()).hexdigest()
            with store.connection() as db:
                request.state.session = db.execute(
                    "SELECT * FROM sessions WHERE id = ? AND expires_at > ?",
                    (session_id, int(time.time())),
                ).fetchone()
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def render(request, name, *, status_code=200, **context):
        session = request.state.session
        return templates.TemplateResponse(
            request=request, name=name, status_code=status_code,
            context={"session": session, "csrf": session["csrf"] if session else "", **context},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)
        if exc.status_code in (302, 303):
            return RedirectResponse(exc.headers["Location"], status_code=exc.status_code)
        return render(request, "error.html", status_code=exc.status_code, message=exc.detail)

    def require_session(request):
        if request.state.session is None:
            if request.url.path.startswith("/api/"):
                raise HTTPException(401, "登录已失效。请先复制未保存文字，再重新登录。")
            raise HTTPException(303, headers={"Location": "/login"})
        return request.state.session

    @asynccontextmanager
    async def read_form(request, *, authenticated=True):
        session = require_session(request) if authenticated else None
        form = await request.form(max_files=1, max_fields=10, max_part_size=512 * 1024)
        token = form.get("csrf", "")
        expected = session["csrf"] if session else request.cookies.get("noteshub_login_csrf", "")
        if not isinstance(token, str) or not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            await form.close()
            raise HTTPException(403, "表单已失效，请刷新页面后重试。")
        try:
            yield form
        finally:
            await form.close()

    def field(form, name, *, max_length, required=False):
        value = form.get(name, "")
        if not isinstance(value, str) or len(value) > max_length:
            raise HTTPException(422, f"{name} 内容无效或过长。")
        if required and not value.strip():
            raise HTTPException(422, "必填内容不能为空。请返回后补充。")
        return value

    def get_book(db, book_id):
        book = db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if book is None:
            raise HTTPException(404, "书籍不存在。")
        return book

    def get_preview(db, book_id, preview_id, session):
        row = db.execute(
            "SELECT * FROM previews WHERE id = ? AND book_id = ? AND session_id = ? AND expires_at > ?",
            (preview_id, book_id, session["id"], int(time.time())),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "预览不存在、已取消或已过期，请重新上传。")
        return row

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if request.state.session:
            return RedirectResponse("/", status_code=303)
        csrf = secrets.token_urlsafe(32)
        response = render(request, "login.html", login_csrf=csrf, error=None)
        response.set_cookie("noteshub_login_csrf", csrf, httponly=True,
                            secure=settings.cookie_secure, samesite="strict", max_age=1800)
        return response

    @app.post("/login")
    async def login(request: Request):
        async with read_form(request, authenticated=False) as form:
            username = field(form, "username", max_length=100)
            password = field(form, "password", max_length=1024)
        now = int(time.time())
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            guard = db.execute("SELECT * FROM login_guard WHERE id = 1").fetchone()
            if guard["blocked_until"] > now:
                raise HTTPException(429, "登录尝试过多，请稍后一分钟再试。")
            if not verify(account, username, password):
                failures = guard["failures"] + 1 if guard["blocked_until"] == 0 else 1
                db.execute("UPDATE login_guard SET failures = ?, blocked_until = ? WHERE id = 1",
                           (failures, now + 60 if failures >= 5 else 0))
                return render(request, "login.html", status_code=401,
                              login_csrf=request.cookies["noteshub_login_csrf"], error="用户名或密码错误。")
            db.execute("UPDATE login_guard SET failures = 0, blocked_until = 0 WHERE id = 1")
            old_session = request.state.session
            if old_session:
                db.execute("DELETE FROM sessions WHERE id = ?", (old_session["id"],))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (hashlib.sha256(token.encode()).hexdigest(), csrf, now + settings.session_seconds))
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, token, httponly=True, secure=settings.cookie_secure,
                            samesite="strict", max_age=settings.session_seconds)
        response.delete_cookie("noteshub_login_csrf", secure=settings.cookie_secure,
                               httponly=True, samesite="strict")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        async with read_form(request):
            with store.connection() as db:
                db.execute("DELETE FROM sessions WHERE id = ?", (request.state.session["id"],))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, secure=settings.cookie_secure, httponly=True, samesite="strict")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def books_page(request: Request, q: str = ""):
        require_session(request)
        if len(q) > 200:
            raise HTTPException(422, "搜索词过长。")
        query = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with store.connection() as db:
            books = db.execute(
                """SELECT b.*, COUNT(n.id) AS total,
                COALESCE(SUM(n.status = 'done'), 0) AS done,
                EXISTS(SELECT 1 FROM imports i WHERE i.book_id = b.id) AS imported
                FROM books b LEFT JOIN notes n ON n.book_id = b.id
                WHERE b.title LIKE ? ESCAPE '\\' GROUP BY b.id ORDER BY b.created_at DESC, b.rowid DESC""",
                (f"%{query}%",),
            ).fetchall()
        return render(request, "books.html", books=books, q=q)

    @app.post("/books")
    async def create_book(request: Request):
        async with read_form(request) as form:
            title = field(form, "title", max_length=200, required=True).strip()
            author = field(form, "author", max_length=200).strip()
        book_id = uuid.uuid4().hex
        with store.connection() as db:
            db.execute("INSERT INTO books VALUES (?, ?, ?, ?)", (book_id, title, author, int(time.time())))
        return RedirectResponse(f"/books/{book_id}", status_code=303)

    @app.get("/books/{book_id}", response_class=HTMLResponse)
    async def book_page(request: Request, book_id: str, note: str | None = None):
        session = require_session(request)
        store.cleanup()
        with store.connection() as db:
            book = get_book(db, book_id)
            imported = db.execute("SELECT * FROM imports WHERE book_id = ?", (book_id,)).fetchone()
            notes = db.execute("SELECT id, status FROM notes WHERE book_id = ? ORDER BY sort_order", (book_id,)).fetchall()
            current = None
            index = 0
            if notes:
                if note is not None:
                    index = next((i for i, item in enumerate(notes) if item["id"] == note), -1)
                    if index == -1:
                        raise HTTPException(404, "这本书中没有这条笔记。")
                current = db.execute("SELECT * FROM notes WHERE id = ?", (notes[index]["id"],)).fetchone()
            elif note is not None:
                raise HTTPException(404, "这本书中没有这条笔记。")
            previews = db.execute(
                "SELECT id, filename FROM previews WHERE book_id = ? AND session_id = ? ORDER BY rowid DESC",
                (book_id, session["id"]),
            ).fetchall()
        def note_url(position):
            return f"/books/{book_id}?note={notes[position]['id']}"
        return render(
            request, "book.html", book=book, imported=imported, notes=notes, previews=previews,
            current=current, index=index, total=len(notes),
            done=sum(item["status"] == "done" for item in notes),
            previous_url=note_url(index - 1) if index > 0 else None,
            next_url=note_url(index + 1) if index + 1 < len(notes) else None,
            export_name=export_filename(book["title"]),
        )

    @app.post("/api/books/{book_id}/notes/{note_id}")
    async def save_note(request: Request, book_id: str, note_id: str):
        async with read_form(request) as form:
            if "second_comment" not in form:
                raise HTTPException(422, "缺少二读内容；如需清空，请明确提交空文本。")
            comment = field(form, "second_comment", max_length=50000)
            status = field(form, "status", max_length=20) if "status" in form else None
            if status is not None and status not in {"pending", "done"}:
                raise HTTPException(422, "处理状态只能是待处理或已完成。")
            if "\x00" in comment:
                raise HTTPException(422, "二读内容不能包含空字符。")
        try:
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT * FROM notes WHERE id = ? AND book_id = ?", (note_id, book_id)).fetchone()
                if existing is None:
                    raise HTTPException(404, "笔记不存在或不属于这本书。")
                actual_status = status if status is not None else existing["status"]
                db.execute(
                    "UPDATE notes SET second_comment = ?, status = ?, updated_at = ? WHERE id = ? AND book_id = ?",
                    (comment, actual_status, int(time.time()), note_id, book_id),
                )
                progress = db.execute(
                    "SELECT COUNT(*) AS total, COALESCE(SUM(status = 'done'), 0) AS done FROM notes WHERE book_id = ?",
                    (book_id,),
                ).fetchone()
                payload = {"note_id": note_id, "second_comment": comment, "status": actual_status,
                           "done": progress["done"], "total": progress["total"]}
        except sqlite3.Error:
            raise HTTPException(503, "保存失败，文字仍在输入框中，请重试。")
        return JSONResponse(payload)

    @app.get("/books/{book_id}/export")
    async def export_book(request: Request, book_id: str):
        require_session(request)
        with store.connection() as db:
            # Keep book metadata and all notes in one consistent read snapshot.
            db.execute("BEGIN")
            book = get_book(db, book_id)
            notes = db.execute("SELECT * FROM notes WHERE book_id = ? ORDER BY sort_order", (book_id,)).fetchall()
        if not notes:
            raise HTTPException(409, "这本书还没有已导入笔记。")
        filename = export_filename(book["title"])
        return Response(
            render_book(book, notes), media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=\"notes.md\"; filename*=UTF-8''{quote(filename, safe='')}",
                     "X-Noteshub-Pending": str(sum(note["status"] == "pending" for note in notes))},
        )

    @app.post("/books/{book_id}/imports")
    async def upload(request: Request, book_id: str):
        session = require_session(request)
        async with read_form(request) as form:
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise HTTPException(422, "请选择一个 Markdown 文件。")
            filename = Path((file.filename or "").replace("\\", "/")).name
            filename = "".join(c for c in filename if c.isprintable())[:200]
            if not filename.lower().endswith(".md"):
                raise HTTPException(422, "只支持 .md 文件。")
            raw = await file.read(settings.max_upload_bytes + 1)
        if len(raw) > settings.max_upload_bytes:
            raise HTTPException(413, "Markdown 文件不能超过 2 MiB。")
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "文件须使用 UTF-8 编码；本次上传未保留。")
        if "\x00" in source:
            raise HTTPException(422, "文件包含无效文本；本次上传未保留。")
        preview = parse_markdown(source)
        if not preview.records:
            raise HTTPException(422, "未发现笔记；本次上传未保留。")
        preview_id = uuid.uuid4().hex
        store.cleanup()
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            get_book(db, book_id)
            imported = db.execute("SELECT sha256 FROM imports WHERE book_id = ?", (book_id,)).fetchone()
            if imported:
                message = "这个文件已经导入过。" if imported["sha256"] == preview.sha256 else "这本书已成功导入过，不能导入其他文件。"
                raise HTTPException(409, message)
            # One active preview per book/session; a new upload replaces only
            # this browser's earlier temporary material, never imported notes.
            db.execute("DELETE FROM previews WHERE book_id = ? AND session_id = ?", (book_id, session["id"]))
            db.execute(
                "INSERT INTO previews (id, book_id, session_id, filename, source, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (preview_id, book_id, session["id"], filename, raw, int(time.time()) + settings.preview_seconds),
            )
        return RedirectResponse(f"/books/{book_id}/imports/{preview_id}", status_code=303)

    @app.get("/books/{book_id}/imports/{preview_id}", response_class=HTMLResponse)
    async def preview_page(request: Request, book_id: str, preview_id: str):
        session = require_session(request)
        store.cleanup()
        with store.connection() as db:
            book = get_book(db, book_id)
            row = get_preview(db, book_id, preview_id, session)
        preview = store.preview(row)
        unresolved = sum(record.needs_correction for record in preview.records)
        return render(request, "preview.html", book=book, preview=preview, upload=row, unresolved=unresolved)

    @app.post("/books/{book_id}/imports/{preview_id}/records/{record_id}")
    async def correction(request: Request, book_id: str, preview_id: str, record_id: int):
        session = require_session(request)
        async with read_form(request) as form:
            original = field(form, "original_text", max_length=50000, required=True)
            comment = field(form, "first_comment", max_length=50000)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = get_preview(db, book_id, preview_id, session)
            try:
                correct_record(store.preview(row), record_id, original_text=original, first_comment=comment)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            corrections = json.loads(row["corrections"])
            corrections[str(record_id)] = {"original_text": original, "first_comment": comment}
            db.execute("UPDATE previews SET corrections = ? WHERE id = ?",
                       (json.dumps(corrections, ensure_ascii=False), preview_id))
        return RedirectResponse(f"/books/{book_id}/imports/{preview_id}#record-{record_id}", status_code=303)

    @app.post("/books/{book_id}/imports/{preview_id}/confirm")
    async def confirm(request: Request, book_id: str, preview_id: str):
        async with read_form(request):
            try:
                store.confirm(book_id, preview_id, request.state.session["id"])
            except ImportConflict as exc:
                raise HTTPException(409, str(exc))
            except LookupError as exc:
                raise HTTPException(404, str(exc))
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            except (OSError, sqlite3.Error):
                raise HTTPException(503, "保存失败，未完成导入；预览仍保留，请稍后重试。")
        return RedirectResponse(f"/books/{book_id}", status_code=303)

    @app.post("/books/{book_id}/imports/{preview_id}/cancel")
    async def cancel(request: Request, book_id: str, preview_id: str):
        async with read_form(request):
            with store.connection() as db:
                get_preview(db, book_id, preview_id, request.state.session)
                db.execute("DELETE FROM previews WHERE id = ?", (preview_id,))
        return RedirectResponse(f"/books/{book_id}", status_code=303)

    @app.get("/books/{book_id}/source")
    async def source_file(request: Request, book_id: str):
        require_session(request)
        with store.connection() as db:
            row = db.execute("SELECT * FROM imports WHERE book_id = ?", (book_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "没有成功导入的源文件。")
        return FileResponse(store.sources / row["original_file_path"], filename=row["filename"],
                            media_type="application/octet-stream")

    return app
