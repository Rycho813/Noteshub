"""Paper notes coexist with Markdown; CAS/retry/upload and migration safety."""
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import sqlite3
import tempfile
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient

from noteshub.config import Settings
from noteshub.db import Store, ARCHIVE_SCHEMA
from noteshub.maintenance import backup, restore
from noteshub.paper import PaperService
from noteshub.web import create_app
from tests.test_archive import legacy_v3
from tests.test_images import picture
from tests.test_web import WebCase


def uid():
    return uuid.uuid4().hex


class PaperTests(WebCase):
    def setUp(self):
        super().setUp()
        self.book = self.create_book().split('/')[-1]
        self.note = uid()
        self.base = '/api/paper/notes/'+self.note
        self.headers = {'X-CSRF-Token': self.token()}

    def payload(self, revision=0, **values):
        return dict(mutation_id=uid(),book_id=self.book,revision=revision,original_text='',
                    first_comment='先记录我的想法',second_comment='',photo_ids=[],**values)

    def send(self, payload):
        return self.client.post(self.base+'/sync',json=payload,headers=self.headers)

    def test_thought_only_and_photo_only_save_without_ocr(self):
        first = self.send(self.payload()).json()
        self.assertEqual(first['first_comment'],'先记录我的想法')
        self.assertEqual(first['original_text'],'')
        photo = uid()
        self.assertEqual(self.client.post(self.base+'/photos/'+photo,content=picture(),headers=self.headers).status_code,200)
        payload = self.payload(1); payload.update(first_comment='',photo_ids=[photo])
        result = self.send(payload).json()
        self.assertEqual(result['photos'][0]['id'],photo)
        self.assertEqual(self.db_count('notes'),1)
        self.assertEqual(self.db_count('ocr_runs'),0)
        self.assertEqual(self.client.get(self.base+'/photos/'+photo).content,picture())

    def test_exact_receipts_and_concurrent_retry_create_one_note(self):
        payload = self.payload()
        service = PaperService(self.store)
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _:service.sync(self.note,payload),range(4)))
        self.assertTrue(all(result==responses[0] for result in responses))
        self.assertEqual(self.db_count('notes'),1)
        newer=self.payload(1); newer['first_comment']='后续评论'
        self.assertEqual(self.send(newer).status_code,200)
        self.assertEqual(self.send(payload).json(),responses[0])
        self.assertEqual(self.client.get(self.base).json()['first_comment'],'后续评论')
        payload['first_comment']='不能重用编号覆盖'
        self.assertEqual(self.send(payload).status_code,409)

    def test_other_device_conflict_does_not_overwrite(self):
        self.send(self.payload())
        self.send(self.payload(1))
        stale = self.payload(1); stale['first_comment']='旧设备输入'
        self.assertEqual(self.send(stale).status_code,409)
        self.assertEqual(self.client.get(self.base).json()['first_comment'],'先记录我的想法')

    def test_upload_immutable_order_remove_and_wrong_note(self):
        self.send(self.payload())
        a,b=uid(),uid()
        for photo in (a,b):
            self.assertEqual(self.client.post(self.base+'/photos/'+photo,content=picture(),headers=self.headers).status_code,200)
            self.assertEqual(self.client.post(self.base+'/photos/'+photo,content=picture(),headers=self.headers).status_code,200)
        p=self.payload(1);p['photo_ids']=[b,a]
        self.assertEqual([v['id'] for v in self.send(p).json()['photos']],[b,a])
        p=self.payload(2);p['photo_ids']=[a]
        self.assertEqual([v['id'] for v in self.send(p).json()['photos']],[a])
        self.assertEqual(self.db_count('paper_photos'),2)  # removal is recoverable
        self.assertEqual(self.client.post(self.base+'/photos/'+a,content=picture(size=(81,40)),headers=self.headers).status_code,409)
        self.assertEqual(self.client.get('/api/paper/notes/'+uid()+'/photos/'+a).status_code,404)
        self.assertEqual(self.client.post(self.base+'/photos/'+uid(),content=b'bad',headers=self.headers).status_code,422)

    def test_retry_recovers_file_published_before_database_commit(self):
        self.send(self.payload())
        photo=uid();path=self.store.root/'paper-images'/f'{photo}.png'
        path.write_bytes(picture())  # Simulated process death after atomic file publication.
        self.assertEqual(self.client.post(self.base+'/photos/'+photo,content=picture(),headers=self.headers).status_code,200)
        self.assertEqual(self.db_count('paper_photos'),1)
        self.assertEqual(path.read_bytes(),picture())

    def test_metadata_failure_rolls_back_and_retry_preserves_content(self):
        self.send(self.payload())
        with self.store.connection() as db:
            db.executescript("CREATE TRIGGER synthetic_failure BEFORE INSERT ON paper_mutations BEGIN SELECT RAISE(ABORT,'disk'); END;")
        payload=self.payload(1);payload['first_comment']='应回滚'
        self.assertEqual(self.send(payload).status_code,503)
        self.assertEqual(self.client.get(self.base).json()['revision'],1)
        self.assertEqual(self.client.get(self.base).json()['first_comment'],'先记录我的想法')

    def test_markdown_rules_edit_export_and_backup_coexist(self):
        book='/books/'+self.book
        preview=self.upload(book).headers['location']
        self.assertEqual(self.client.post(preview+'/confirm',data={'csrf':self.token()}).status_code,303)
        self.send(self.payload())
        self.assertEqual(self.upload(book).status_code,409)
        photo=uid()
        self.client.post(self.base+'/photos/'+photo,content=picture(),headers=self.headers)
        p=self.payload(1);p['photo_ids']=[photo];p['original_text']='补充的纸质书原文'
        self.send(p)
        export=self.client.get(book+'/export').content
        self.assertIn('原文'.encode(),export)
        self.assertIn('先记录我的想法'.encode(),export)
        self.assertIn('补充的纸质书原文'.encode(),export)
        response=self.client.post('/api/books/'+self.book+'/notes/'+self.note,data={'csrf':self.token(),'revision':'2','second_comment':'旧阅读界面二读'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get(self.base).json()['revision'],3)
        out=io.BytesIO();backup(self.store.root,out)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'restored';restore(root,io.BytesIO(out.getvalue()))
            self.assertEqual((root/'paper-images'/f'{photo}.png').read_bytes(),picture())
            with TestClient(create_app(Settings(root,cookie_secure=False)),follow_redirects=False) as client:
                self.login(client)
                self.assertEqual(client.get(self.base).json()['second_comment'],'旧阅读界面二读')
                self.assertIn('旧阅读界面二读',client.get(book+'/export').text)

    def test_login_csrf_limits_and_public_shell_has_no_private_data(self):
        self.assertEqual(self.client.post(self.base+'/sync',json=self.payload()).status_code,403)
        with TestClient(self.app,follow_redirects=False) as anonymous:
            self.assertEqual(anonymous.get(self.base).status_code,401)
            self.assertEqual(anonymous.get('/api/paper/context').status_code,401)
            shell=anonymous.get('/quick')
            self.assertEqual(shell.status_code,200)
            self.assertNotIn('csrf',shell.text)
        p=self.payload();p['photo_ids']=[uid() for _ in range(21)]
        self.assertEqual(self.send(p).status_code,422)
        p=self.payload();p['first_comment']='x'*50001
        self.assertEqual(self.send(p).status_code,422)
        p=self.payload();p['photo_ids']=[uid()]
        self.assertEqual(self.send(p).status_code,422)
        self.assertEqual(self.db_count('notes'),0)

    def test_real_v4_migration_preserves_all_old_rows_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'old';image,md=legacy_v3(root)
            with sqlite3.connect(root/'noteshub.sqlite3') as db:
                db.executescript(ARCHIVE_SCHEMA)
                before={table:db.execute('SELECT * FROM '+table).fetchall() for table in ('books','notes','imports','image_drafts','ocr_runs')}
            files={name:(root/name).read_bytes() for name in (image,md,'account.json')}
            store=Store(Settings(root));Store(Settings(root))
            with store.connection() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],6)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                for table,rows in before.items(): self.assertEqual(rows,[tuple(r) for r in db.execute('SELECT * FROM '+table)])
            self.assertEqual(files,{name:(root/name).read_bytes() for name in files})
