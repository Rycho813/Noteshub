"""Unified recorder: idempotent books, moves, and narrowly scoped retirement."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from noteshub.config import Settings
from noteshub.db import Store
from noteshub.paper import PaperService
from noteshub.retire_drafts import retire
from tests.test_archive import legacy_v3
from tests.test_paper import uid
from tests.test_web import WebCase


class UnifiedTests(WebCase):
    def setUp(self):
        super().setUp()
        self.book=self.create_book().split('/')[-1]
        self.note=uid();self.base='/api/paper/notes/'+self.note
        self.headers={'X-CSRF-Token':self.token()}

    def payload(self, revision=0):
        return dict(mutation_id=uid(),book_id=self.book,revision=revision,original_text='',first_comment='先记录我的想法',second_comment='',photo_ids=[])

    def send(self,payload):
        return self.client.post(self.base+'/sync',json=payload,headers=self.headers)

    def test_book_creation_retry_collision_csrf_and_validation(self):
        book=uid();url='/api/paper/books/'+book;payload={'title':' 离线书 ','author':' 作者 '}
        self.assertEqual(self.client.post(url,json=payload).status_code,403)
        for _ in range(3):
            result=self.client.post(url,json=payload,headers=self.headers)
            self.assertEqual(result.status_code,200)
        self.assertEqual(result.json(),{'id':book,'title':'离线书','author':'作者'})
        self.assertEqual(self.db_count('books'),2)
        self.assertEqual(self.client.post(url,json={'title':'另一书'},headers=self.headers).status_code,409)
        for title in ('',' '*5,'x'*201,'bad\x00'):
            self.assertEqual(self.client.post('/api/paper/books/'+uid(),json={'title':title},headers=self.headers).status_code,422)
        with TestClient(self.app,follow_redirects=False) as anonymous:
            self.assertEqual(anonymous.post(url,json=payload,headers=self.headers).status_code,401)

    def test_move_same_record_with_retry_and_stale_revision_protection(self):
        new_book=self.create_book('新归属').split('/')[-1]
        self.send(self.payload())
        move=self.payload(1);move['book_id']=new_book
        first=self.send(move)
        self.assertEqual(first.status_code,200)
        self.assertEqual(self.send(move).json(),first.json())
        self.assertEqual(self.db_count('notes'),1)
        stale=self.payload(1);stale['first_comment']='旧设备不能搬回去'
        self.assertEqual(self.send(stale).status_code,409)
        self.assertEqual(self.client.get(self.base).json()['book_id'],new_book)
        self.assertIn('先记录我的想法',self.client.get('/books/'+new_book+'/export').text)

    def test_parallel_moves_keep_order_unique(self):
        target=self.create_book('并发归属').split('/')[-1]
        service=PaperService(self.store)
        ids=[uid() for _ in range(4)]
        for id in ids:service.sync(id,self.payload())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda id:service.sync(id,{**self.payload(1),'book_id':target}),ids))
        self.assertTrue(all(r['book_id']==target for r in results))
        with self.store.connection() as db:
            self.assertEqual(len({r[0] for r in db.execute('SELECT sort_order FROM notes WHERE book_id=?',(target,))}),4)

    def test_ocr_embed_policy_is_same_origin_and_scoped(self):
        self.assertIn("frame-ancestors 'self'",self.client.get('/quick?view=ocr').headers['content-security-policy'])
        for url in ('/quick','/quick?view=other','/'):
            self.assertIn("frame-ancestors 'none'",self.client.get(url).headers['content-security-policy'])

    def test_retirement_preserves_archived_markdown_paper_and_books(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'data';image,source=legacy_v3(root);store=Store(Settings(root))
            from noteshub.drafts import DraftService
            service=DraftService(store,None)
            service.archive('book','draft',2)
            from tests.test_images import picture
            from noteshub.images import validate_image
            raw=picture(size=(90,45))
            id=store.save_image_draft('book',raw,validate_image(raw,store.settings))
            with store.connection() as db:
                path=root/db.execute('SELECT file_path FROM image_drafts WHERE id=?',(id,)).fetchone()[0]
                db.execute("INSERT INTO ocr_runs(id,draft_id,state,started_at) VALUES (?,?,'failed',0)",(uid(),id))
            paper=uid();PaperService(store).sync(paper,{**self.payload(),'book_id':'book'})
            self.assertEqual(retire(root),{'drafts':1,'ocr_runs':1})
            self.assertTrue(path.exists())
            self.assertEqual(retire(root,apply=True),{'drafts':1,'ocr_runs':1})
            self.assertFalse(path.exists())
            self.assertTrue((root/image).exists());self.assertTrue((root/source).exists())
            with store.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM books').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM notes').fetchone()[0],3)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ocr_runs').fetchone()[0],1)
                self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(retire(root,apply=True),{'drafts':0,'ocr_runs':0})
