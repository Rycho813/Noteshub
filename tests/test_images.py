"""Image draft boundaries, migration and portable backup restoration."""
import asyncio
import io
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from noteshub.config import Settings
from noteshub.db import Store
from noteshub.maintenance import backup, restore
from noteshub.web import BodyLimitMiddleware, create_app
from tests.test_web import WebCase


def picture(fmt='PNG', size=(80, 40)):
    out = io.BytesIO()
    Image.new('RGB', size, 'white').save(out, format=fmt)
    return out.getvalue()


class ImageTests(WebCase):
    def setUp(self):
        super().setUp()
        self.book = self.create_book()
        self.raw = picture()

    def upload_image(self, raw=None, book=None, **kwargs):
        return self.client.post('/api' + (book or self.book) + '/image-drafts',
                                data={'csrf': self.token()},
                                files={'file': ('../../ignored.jpg', self.raw if raw is None else raw, 'text/plain')}, **kwargs)

    def row(self):
        with self.store.connection() as db:
            return db.execute('SELECT * FROM image_drafts').fetchone()

    def image_url(self, draft_id, book=None):
        return (book or self.book) + '/image-drafts/' + draft_id + '/file'

    def test_png_and_jpeg_are_private_and_not_notes(self):
        for raw, media in ((self.raw, 'image/png'), (picture('JPEG'), 'image/jpeg')):
            response = self.upload_image(raw)
            self.assertEqual(response.status_code, 201)
            url = self.image_url(response.json()['id'])
            image = self.client.get(url)
            self.assertEqual(image.content, raw)
            self.assertEqual(image.headers['content-type'], media)
            self.assertEqual(image.headers['cache-control'], 'no-store')
            self.assertEqual(image.headers['x-content-type-options'], 'nosniff')
            with TestClient(self.app, follow_redirects=False) as anonymous:
                self.assertEqual(anonymous.get(url).status_code, 303)
                self.assertEqual(anonymous.get(self.book + '/images').status_code, 303)
                self.assertEqual(anonymous.post('/api'+self.book+'/image-drafts', files={'file': ('x.png',raw)}).status_code, 401)
            self.assertEqual(self.db_count('notes'), 0)
            self.assertEqual(self.db_count('imports'), 0)
        self.assertEqual(self.db_count('image_drafts'), 2)
        self.assertEqual(self.client.get('/images/'+Path(self.row()['file_path']).name).status_code, 404)
        self.assertEqual(self.client.get('/static/'+self.row()['file_path']).status_code, 404)

    def test_refresh_restart_and_relogin_preserve_draft(self):
        response = self.upload_image()
        draft_id = response.json()['id']
        for _ in range(2):
            self.assertIn(draft_id, self.client.get(self.book+'/images').text)
        self.client.post('/logout', data={'csrf': self.token()})
        self.login()
        self.assertEqual(self.client.get(self.image_url(draft_id)).content, self.raw)
        with TestClient(create_app(self.settings), follow_redirects=False) as client:
            client.cookies.update(self.client.cookies)
            self.assertEqual(client.get(self.image_url(draft_id)).content, self.raw)

    def test_upload_context_refreshes_after_relogin_and_requires_auth(self):
        url = '/api'+self.book+'/image-drafts/context'
        previous = self.client.get(url).json()['csrf']
        self.client.post('/logout', data={'csrf': previous})
        self.assertEqual(self.client.get(url).status_code, 401)
        self.login()
        current = self.client.get(url).json()['csrf']
        self.assertNotEqual(current, previous)
        self.assertEqual(self.client.get('/api/books/missing/image-drafts/context').status_code, 404)
        response = self.client.post('/api'+self.book+'/image-drafts',data={'csrf':current},files={'file':('x.png',self.raw)})
        self.assertEqual(response.status_code, 201)

    def test_retry_is_idempotent_and_other_books_are_independent(self):
        first = self.upload_image().json()['id']
        self.assertEqual(self.upload_image().json()['id'], first)
        self.assertEqual(self.db_count('image_drafts'), 1)
        self.assertEqual(len(list(self.store.images.iterdir())), 1)
        other = self.create_book('其他书')
        self.assertNotEqual(self.upload_image(book=other).json()['id'], first)
        self.assertEqual(self.client.get(self.image_url(first,other)).status_code, 404)

    def test_csrf_and_wrong_book_cannot_remove_draft(self):
        draft = self.upload_image().json()['id']
        cancel = self.book+'/image-drafts/'+draft+'/cancel'
        self.assertEqual(self.client.post(cancel, data={'csrf':'wrong'}).status_code,403)
        self.assertEqual(self.client.post('/api'+self.book+'/image-drafts', data={'csrf':'wrong'}, files={'file':('x.png',self.raw)}).status_code,403)
        other = self.create_book('其他书')
        self.client.post(other+'/image-drafts/'+draft+'/cancel', data={'csrf':self.token()})
        self.assertEqual(self.client.get(self.image_url(draft)).content,self.raw)
        self.assertEqual(self.client.post(cancel,data={'csrf':self.token()}).status_code,303)
        self.assertEqual(self.client.get(self.image_url(draft)).status_code,404)
        self.assertEqual(list(self.store.images.iterdir()),[])

    def test_expiry_and_orphan_cleanup_leave_live_drafts(self):
        first = self.upload_image().json()['id']
        second = self.upload_image(picture(size=(40,80))).json()['id']
        with self.store.connection() as db:
            db.execute('UPDATE image_drafts SET expires_at = ? WHERE id = ?', (int(time.time())-1, first))
        self.assertEqual(self.client.get(self.image_url(first)).status_code,404)
        orphan = self.store.images / ('f'*32+'.png')
        orphan.write_bytes(self.raw)
        self.store.cleanup()
        self.assertFalse(orphan.exists())
        self.assertEqual(self.db_count('image_drafts'),1)
        self.assertEqual(self.client.get(self.image_url(second)).content,picture(size=(40,80)))

    def test_bad_images_do_not_write_files_or_rows(self):
        for raw in (b'', b'<svg>not an image</svg>', self.raw[:40], picture('GIF'), picture(size=(6001,1)), picture(size=(4000,3001)), picture('JPEG')[:-20]):
            with self.subTest(size=len(raw)):
                self.assertEqual(self.upload_image(raw).status_code,422)
        self.assertEqual(self.upload_image(b'x'*(self.settings.max_image_bytes+1)).status_code,413)
        self.assertEqual(self.db_count('image_drafts'),0)
        self.assertEqual(list(self.store.images.iterdir()),[])

    def test_chunked_image_body_is_bounded_before_parsing(self):
        def chunks():
            yield b'x'*self.settings.max_image_bytes
            yield b'x'*(64*1024+1)
        response = self.client.post('/api'+self.book+'/image-drafts',content=chunks(),headers={'content-type':'application/octet-stream'})
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.db_count('image_drafts'),0)

    def test_missing_book_does_not_write(self):
        self.assertEqual(self.upload_image(book='/books/missing').status_code,404)
        self.assertEqual(list(self.store.images.iterdir()),[])

    def test_file_write_failure_rolls_back_and_can_retry(self):
        with patch('noteshub.db.os.fsync',side_effect=OSError('disk full')):
            self.assertEqual(self.upload_image().status_code,503)
        self.assertEqual(self.db_count('image_drafts'),0)
        self.assertEqual(list(self.store.images.iterdir()),[])
        self.assertEqual(self.upload_image().status_code,201)

    def test_v1_migration_preserves_notes_and_exports(self):
        preview = self.upload(self.book).headers['location']
        self.client.post(preview+'/confirm',data={'csrf':self.token()})
        export = self.client.get(self.book+'/export').content
        with self.store.connection() as db:
            before = list(db.execute('SELECT * FROM notes').fetchall())
            for table in ('paper_mutations','paper_pages','paper_photos','paper_entries','paper_workspace'):
                db.execute('DROP TABLE '+table)
            db.execute('DROP TABLE ocr_runs')
            db.execute('DROP TABLE image_drafts')
            db.execute('PRAGMA user_version = 1')
        Store(self.settings)
        Store(self.settings)  # Startup migration is repeatable.
        with self.store.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM notes')],[tuple(row) for row in before])
        self.assertEqual(self.client.get(self.book+'/export').content,export)
        self.assertEqual(self.upload_image().status_code,201)

    def test_backup_restore_to_new_location_keeps_images_and_old_content(self):
        preview = self.upload(self.book).headers['location']
        self.client.post(preview+'/confirm',data={'csrf':self.token()})
        export = self.client.get(self.book+'/export').content
        draft_id = self.upload_image().json()['id']
        output = io.BytesIO()
        backup(self.settings.data_dir,output)
        with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
            self.assertIn(self.row()['file_path'],archive.getnames())
            self.assertTrue(all(not name.startswith('/') for name in archive.getnames()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'moved-to-another-machine'
            restore(root,io.BytesIO(output.getvalue()))
            self.assertEqual((root/self.row()['file_path']).read_bytes(),self.raw)
            with TestClient(create_app(Settings(root,cookie_secure=False)),follow_redirects=False) as c:
                self.assertEqual(c.get(self.image_url(draft_id)).status_code,303)
                self.login(c)
                self.assertEqual(c.get(self.image_url(draft_id)).content,self.raw)
                self.assertEqual(c.get(self.book+'/export').content,export)
                self.assertIn(draft_id,c.get(self.book+'/images').text)

    def test_missing_or_tampered_image_blocks_backup(self):
        self.upload_image()
        path = self.store.root/self.row()['file_path']
        path.write_bytes(b'wrong')
        with self.assertRaises(ValueError):
            backup(self.settings.data_dir,io.BytesIO())
        path.unlink()
        with self.assertRaises(ValueError):
            backup(self.settings.data_dir,io.BytesIO())

    def test_restore_does_not_extend_expired_drafts(self):
        draft_id = self.upload_image().json()['id']
        with self.store.connection() as db:
            db.execute('UPDATE image_drafts SET expires_at = ? WHERE id = ?', (int(time.time())-1,draft_id))
        out = io.BytesIO()
        backup(self.settings.data_dir,out)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore(root,io.BytesIO(out.getvalue()))
            restored = Store(Settings(root))
            with restored.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM image_drafts').fetchone()[0],0)
            self.assertEqual(list((root/'images').iterdir()),[])

    def test_v1_archive_remains_restorable_and_upgrades_on_start(self):
        with self.store.connection() as db:
            for table in ('paper_mutations','paper_pages','paper_photos','paper_entries','paper_workspace'):
                db.execute('DROP TABLE '+table)
            db.execute('DROP TABLE ocr_runs')
            db.execute('DROP TABLE image_drafts')
            db.execute('PRAGMA user_version = 1')
        archive = io.BytesIO()
        backup(self.settings.data_dir,archive)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore(root,io.BytesIO(archive.getvalue()))
            migrated = Store(Settings(root))
            with migrated.connection() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],5)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM books').fetchone()[0],1)


class UploadConcurrencyTests(WebCase):
    def test_only_one_image_body_can_be_in_flight(self):
        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()
            async def app(scope,receive,send):
                await receive()
                started.set()
                await release.wait()
            middleware = BodyLimitMiddleware(app,max_bytes=20,max_image_bytes=30)
            async def receive():
                return {'type':'http.request','body':b'x','more_body':False}
            sent=[]
            async def send(message):
                sent.append(message)
            scope={'type':'http','method':'POST','path':'/api/books/a/image-drafts'}
            first=asyncio.create_task(middleware(scope,receive,send))
            await started.wait()
            await middleware(scope,receive,send)
            self.assertEqual(sent[0]['status'],503)
            release.set()
            await first
        asyncio.run(scenario())
