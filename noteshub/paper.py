"""Multi-photo paper notes: immutable uploads, explicit versions, retry receipts."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import time

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from noteshub.images import validate_image


ID = re.compile(r'[0-9a-f]{32}')
MAX_PHOTOS = 20


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise HTTPException(422, '记录或图片编号无效。')
    return value


def text(value):
    if not isinstance(value, str) or len(value) > 50000 or '\x00' in value:
        raise HTTPException(422, '文字最多 50000 字符，不能包含空字符。')
    return value


def state(db, note_id):
    row = db.execute('SELECT n.*, p.revision,p.confirmed_revision,p.confirmed_at FROM notes n '
                     'JOIN paper_entries p ON p.note_id=n.id WHERE n.id=?', (note_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '快速记录不存在。')
    photos = db.execute('SELECT a.* FROM paper_pages p JOIN paper_photos a ON a.id=p.photo_id '
                        'WHERE p.note_id=? ORDER BY p.position', (note_id,)).fetchall()
    return {**{key: row[key] for key in ('id','book_id','original_text','first_comment','second_comment',
                                        'status','revision','updated_at','confirmed_revision','confirmed_at')},
            'photos': [{key: photo[key] for key in ('id','sha256','media_type','byte_size','purge_after','purged_at')}
                       for photo in photos]}


def changed(db, note_id):
    """All edit paths invalidate prior approval, including the existing reader."""
    db.execute('UPDATE paper_entries SET revision=revision+1,confirmed_revision=NULL,confirmed_at=NULL WHERE note_id=?', (note_id,))
    db.execute('UPDATE paper_photos SET purge_after=NULL WHERE note_id=? AND purged_at IS NULL', (note_id,))


class PaperService:
    def __init__(self, store):
        self.store = store

    def sync(self, note_id, payload):
        identifier(note_id)
        mutation = identifier(payload.get('mutation_id'))
        book_id = payload.get('book_id')
        if not isinstance(book_id, str):
            raise HTTPException(422, '请选择书籍。')
        revision = payload.get('revision')
        if type(revision) is not int or revision < 0:
            raise HTTPException(422, '版本无效。')
        fields = [text(payload.get(key, '')) for key in ('original_text','first_comment','second_comment')]
        photos = payload.get('photo_ids', [])
        if not isinstance(photos, list) or len(photos) > MAX_PHOTOS:
            raise HTTPException(422, '每条记录最多 20 张图片。')
        photos = [identifier(photo) for photo in photos]
        if len(set(photos)) != len(photos):
            raise HTTPException(422, '图片不能重复。')
        digest = hashlib.sha256(json.dumps([note_id, book_id, revision, fields, photos], ensure_ascii=False).encode()).hexdigest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            receipt = db.execute('SELECT * FROM paper_mutations WHERE id=?', (mutation,)).fetchone()
            if receipt:
                if receipt['note_id'] != note_id or receipt['payload_sha'] != digest:
                    raise HTTPException(409, '同一个同步编号不能用于不同内容。')
                return json.loads(receipt['response'])
            row = db.execute('SELECT n.book_id,p.revision FROM notes n LEFT JOIN paper_entries p ON p.note_id=n.id WHERE n.id=?', (note_id,)).fetchone()
            if row and row['revision'] is None:
                raise HTTPException(409, '这条记录属于其他书籍或录入方式。')
            if row and row['revision'] != revision:
                raise HTTPException(409, '云端内容已在其他设备更新。本机内容仍保留，请核对或另存一条。')
            if not row and revision != 0:
                raise HTTPException(409, '云端版本不存在，本机内容仍保留。')
            if not db.execute('SELECT 1 FROM books WHERE id=?', (book_id,)).fetchone():
                raise HTTPException(404, '书籍不存在，请联网更新书籍列表。')
            for photo in photos:
                if not db.execute('SELECT 1 FROM paper_photos WHERE id=? AND note_id=?', (photo,note_id)).fetchone():
                    raise HTTPException(422, '还有图片未上传，记录仍保留在本机。')
            now = int(time.time())
            if row:
                if row['book_id'] != book_id:
                    order = db.execute('SELECT COALESCE(MAX(sort_order)+1,0) FROM notes WHERE book_id=?', (book_id,)).fetchone()[0]
                    db.execute('UPDATE notes SET book_id=?,sort_order=? WHERE id=?', (book_id,order,note_id))
                db.execute('UPDATE notes SET original_text=?,first_comment=?,second_comment=?,updated_at=?,manually_corrected=1 WHERE id=?', (*fields,now,note_id))
                changed(db, note_id)
            else:
                order = db.execute('SELECT COALESCE(MAX(sort_order)+1,0) FROM notes WHERE book_id=?', (book_id,)).fetchone()[0]
                db.execute("INSERT INTO notes (id,book_id,source_type,original_text,first_comment,second_comment,raw_source,sort_order,start_line,end_line,manually_corrected,created_at,updated_at) VALUES (?,?,'paper',?,?,?,'',?,0,0,1,?,?)",
                           (note_id,book_id,*fields,order,now,now))
                db.execute('INSERT INTO paper_entries(note_id,revision) VALUES (?,1)', (note_id,))
            db.execute('DELETE FROM paper_pages WHERE note_id=?', (note_id,))
            db.executemany('INSERT INTO paper_pages(note_id,photo_id,position) VALUES (?,?,?)',
                           [(note_id,photo,position) for position,photo in enumerate(photos)])
            result = state(db, note_id)
            db.execute('INSERT INTO paper_mutations VALUES (?,?,?,?)', (mutation,note_id,digest,json.dumps(result,ensure_ascii=False)))
            return result

    def upload(self, note_id, photo_id, raw):
        identifier(note_id); identifier(photo_id)
        metadata = validate_image(raw, self.store.settings)
        checksum = hashlib.sha256(raw).hexdigest()
        media_type, extension, width, height = metadata
        relative = f'paper-images/{photo_id}.{extension}'
        path = self.store.root / relative
        created = False
        try:
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                state(db, note_id)
                existing = db.execute('SELECT * FROM paper_photos WHERE id=?', (photo_id,)).fetchone()
                if existing:
                    if existing['note_id'] != note_id or existing['sha256'] != checksum or existing['purged_at'] is not None:
                        raise HTTPException(409, '图片编号已被使用，原图未覆盖。')
                    return {'id':photo_id,'sha256':checksum}
                if path.exists():
                    # A crash after file publication and before SQLite commit
                    # leaves a complete unreferenced file. Exact retry reuses it.
                    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:
                        raise OSError('Unrecognized image file')
                else:
                    fd,temporary=tempfile.mkstemp(prefix='.upload-',dir=path.parent)
                    try:
                        with os.fdopen(fd,'wb') as target:
                            target.write(raw);target.flush();os.fsync(target.fileno())
                        os.link(temporary,path);created=True
                    finally:
                        os.unlink(temporary)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                db.execute('INSERT INTO paper_photos(id,note_id,file_path,sha256,media_type,byte_size,width,height,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                           (photo_id,note_id,relative,checksum,media_type,len(raw),width,height,int(time.time())))
            return {'id':photo_id,'sha256':checksum}
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise


def install_paper(app, service, *, require_session, render, account_id):
    store = service.store

    async def body(request):
        session = require_session(request)
        token = request.headers.get('x-csrf-token', '')
        if not token or not hmac.compare_digest(token.encode(), session['csrf'].encode()):
            raise HTTPException(403, '登录已更新，请重新登录后同步，本机内容仍保留。')
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, '同步内容无效。')
        if not isinstance(payload, dict):
            raise HTTPException(422, '同步内容无效。')
        return payload

    async def operation(method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except (sqlite3.Error, OSError):
            raise HTTPException(503, '服务器暂时不能保存，本机内容仍保留，可稍后重试。')

    @app.get('/quick')
    async def quick(request: Request):
        # This is a public, empty shell. Never cache rendered private pages.
        return render(request, 'quick.html')

    @app.get('/quick-sw.js')
    async def worker():
        return FileResponse(__import__('pathlib').Path(__file__).parent/'static'/'quick-sw.js',
                            media_type='application/javascript', headers={'Service-Worker-Allowed':'/quick'})

    @app.get('/api/paper/context')
    async def context(request: Request):
        session = require_session(request)
        with store.connection() as db:
            workspace = db.execute('SELECT id FROM paper_workspace').fetchone()[0]
            books = [dict(row) for row in db.execute('SELECT id,title,author FROM books ORDER BY created_at DESC,rowid DESC')]
        return {'workspace':hashlib.sha256((workspace+account_id).encode()).hexdigest(), 'csrf':session['csrf'], 'books':books}

    @app.post('/api/paper/books/{book_id}')
    async def create_book(request: Request, book_id: str):
        payload = await body(request)
        identifier(book_id)
        title, author = payload.get('title'), payload.get('author', '')
        if (not isinstance(title, str) or not title.strip() or len(title) > 200
                or not isinstance(author, str) or len(author) > 200 or '\x00' in title + author):
            raise HTTPException(422, '书名必填，书名与作者最多各 200 字符。')
        title, author = title.strip(), author.strip()
        def save():
            with store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT id,title,author FROM books WHERE id=?', (book_id,)).fetchone()
                if old and (old['title'],old['author']) != (title,author):
                    raise HTTPException(409, '书籍编号已存在，请保留本机内容后重试。')
                if not old:
                    db.execute('INSERT INTO books VALUES (?,?,?,?)', (book_id,title,author,int(time.time())))
                return {'id':book_id,'title':title,'author':author}
        return await operation(save)

    @app.get('/api/paper/notes')
    async def listing(request: Request):
        require_session(request)
        with store.connection() as db:
            rows = db.execute('SELECT note_id FROM paper_entries p JOIN notes n ON n.id=p.note_id ORDER BY n.updated_at DESC,n.rowid DESC LIMIT 200').fetchall()
            return {'notes':[state(db,row[0]) for row in rows]}

    @app.get('/api/paper/notes/{note_id}')
    async def get(request: Request, note_id: str):
        require_session(request)
        with store.connection() as db:
            return state(db, identifier(note_id))

    @app.post('/api/paper/notes/{note_id}/sync')
    async def sync(request: Request, note_id: str):
        payload = await body(request)
        return JSONResponse(await operation(service.sync,note_id,payload))

    @app.post('/api/paper/notes/{note_id}/photos/{photo_id}')
    async def upload(request: Request, note_id: str, photo_id: str):
        session = require_session(request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token','').encode(),session['csrf'].encode()):
            raise HTTPException(403, '请重新登录后同步，图片仍在本机。')
        raw = await request.body()
        return JSONResponse(await operation(service.upload,note_id,photo_id,raw))

    @app.get('/api/paper/notes/{note_id}/photos/{photo_id}')
    async def photo(request: Request, note_id: str, photo_id: str):
        require_session(request)
        def read():
            with store.connection() as db:
                db.execute('BEGIN')
                row = db.execute('SELECT * FROM paper_photos WHERE id=? AND note_id=? AND purged_at IS NULL', (identifier(photo_id),identifier(note_id))).fetchone()
                if row is None:
                    raise HTTPException(404, '图片不存在或已按确认计划清理。')
                raw = (store.root/row['file_path']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != row['sha256']:
                    raise HTTPException(503, '图片校验失败，请检查完整备份。')
                return raw,row['media_type']
        raw,media = await operation(read)
        return Response(raw,media_type=media)

    return body, operation
