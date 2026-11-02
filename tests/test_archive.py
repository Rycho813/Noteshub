"""Formal archive invariants, genuine v3 upgrades, and portable restore."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from noteshub.auth import provision
from noteshub.config import Settings
from noteshub.db import Store, SCHEMA, IMAGE_SCHEMA, OCR_SCHEMA
from noteshub.drafts import DraftService
from noteshub.maintenance import backup, restore
from noteshub.web import create_app
from tests.test_images import picture
from tests.test_ocr import FakeOCR, result
from tests.test_web import WebCase, PASSWORD


def legacy_v3(root):
    """A real pre-archive database, including the user's observed text lengths."""
    provision(root,'reader',PASSWORD)
    (root/'images').mkdir()
    (root/'sources').mkdir()
    raw=picture()
    image='images/'+'a'*32+'.png'
    md='sources/'+'b'*32+'.md'
    source='“旧版原文”旧版一读'.encode()
    (root/image).write_bytes(raw)
    (root/md).write_bytes(source)
    with sqlite3.connect(root/'noteshub.sqlite3') as db:
        db.executescript(SCHEMA+IMAGE_SCHEMA+OCR_SCHEMA)
        db.execute("INSERT INTO books VALUES ('book','旧书','作者',10)")
        db.execute("INSERT INTO imports VALUES ('import','book','old.md',?,?,'curly-quotes-v1',11)",(hashlib.sha256(source).hexdigest(),Path(md).name))
        db.execute("INSERT INTO notes VALUES ('old-note','book','import','zotero','旧版原文','旧版一读','旧版二读','done','“旧版原文”旧版一读',0,1,1,0,12,13)")
        db.execute("INSERT INTO image_drafts VALUES ('draft','book',?,?,'image/png',?,80,40,14,1,1,?,?,2,15)",
                   (image,hashlib.sha256(raw).hexdigest(),len(raw),'原'*137,'评'*17))
        db.execute("INSERT INTO ocr_runs VALUES ('run','draft','succeeded',14,15,?,?,'synthetic-old-request','')",('识'*134,result('识'*134).response_json))
    return image,md


class ArchiveTests(WebCase):
    def setUp(self):
        self.fake=FakeOCR()
        with patch('noteshub.web.TencentOCR',return_value=self.fake):
            super().setUp()
        self.book=self.create_book('图片归档书')
        self.draft=self.make_draft()
        self.api='/api'+self.book+'/image-drafts/'+self.draft

    def make_draft(self,raw=None,book=None):
        response=self.client.post('/api'+(book or self.book)+'/image-drafts',data={'csrf':self.token()},files={'file':('x.png',raw or picture(),'image/png')})
        self.assertEqual(response.status_code,201)
        return response.json()['id']

    def state(self):
        return self.client.get(self.api).json()

    def save(self,original='校对原文',comment='一读内容'):
        return self.client.post(self.api+'/save',data={'csrf':self.token(),'revision':str(self.state()['revision']),'original_text':original,'first_comment':comment})

    def archive(self,revision=None):
        return self.client.post(self.api+'/archive',data={'csrf':self.token(),'revision':str(self.state()['revision'] if revision is None else revision)})

    def test_archive_creates_a_pending_note_with_empty_comment_allowed(self):
        self.save('  保留空白的原文\n','')
        response=self.archive()
        self.assertEqual(response.status_code,200)
        state=response.json()
        self.assertTrue(state['archived'])
        self.assertEqual(state['note_status'],'pending')
        with self.store.connection() as db:
            note=db.execute('SELECT * FROM notes WHERE id = ?',(state['note_id'],)).fetchone()
        self.assertEqual(note['original_text'],'  保留空白的原文\n')
        self.assertEqual(note['first_comment'],'')
        self.assertEqual(note['source_type'],'image')
        self.assertIsNone(note['import_id'])
        self.assertEqual(note['image_draft_id'],self.draft)
        self.assertEqual(note['second_comment'],'')
        self.assertEqual(self.db_count('imports'),0)
        self.assertEqual(self.fake.calls,0)
        self.assertIn('保留空白的原文',self.client.get(state['note_url']).text)
        self.assertIn('查看来源图片与识别记录',self.client.get(state['note_url']).text)
        self.assertIn('共 1 条笔记',self.client.get('/').text)
        self.assertIn('保留空白的原文',self.client.get(self.book+'/export').text)

    def test_repeated_archive_and_concurrent_double_click_are_idempotent(self):
        self.save()
        revision=self.state()['revision']
        service=self.app.state.drafts
        book_id=self.book.split('/')[-1]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:service.archive(book_id,self.draft,revision),range(4)))
        self.assertEqual(len({value['note_id'] for value in results}),1)
        self.assertEqual(self.db_count('notes'),1)
        self.assertEqual(self.archive(revision).json()['note_id'],results[0]['note_id'])
        self.assertEqual(self.state()['revision'],revision+1)

    def test_two_different_drafts_append_concurrently_without_order_collision(self):
        self.save()
        second=self.make_draft(picture(size=(81,40)))
        service=self.app.state.drafts
        book_id=self.book.split('/')[-1]
        service.save(book_id,second,'第二条原文','第二条一读',0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda d:service.archive(book_id,d,1),(self.draft,second)))
        with self.store.connection() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT sort_order FROM notes ORDER BY sort_order')],[0,1])
        self.assertEqual(self.db_count('notes'),2)

    def test_empty_original_stale_revision_and_csrf_do_not_archive(self):
        self.assertEqual(self.archive().status_code,422)
        self.save('\u3000\n','有评论也不能空原文')
        self.assertEqual(self.archive().status_code,422)
        self.save()
        old=self.state()['revision']
        self.save('另一页更新','更新评论')
        self.assertEqual(self.archive(old).status_code,409)
        self.assertEqual(self.client.post(self.api+'/archive',data={'csrf':'wrong','revision':'0'}).status_code,403)
        self.assertEqual(self.client.post(self.api+'/archive',data={'csrf':self.token(),'revision':'NaN'}).status_code,422)
        self.assertEqual(self.db_count('notes'),0)
        self.assertEqual(self.state()['original_text'],'另一页更新')

    def test_foreign_book_and_anonymous_archive_are_rejected(self):
        self.save()
        other=self.create_book('另一书')
        self.assertEqual(self.client.post(self.api.replace(self.book,other)+'/archive',data={'csrf':self.token(),'revision':'1'}).status_code,404)
        with TestClient(self.app,follow_redirects=False) as anonymous:
            self.assertEqual(anonymous.post(self.api+'/archive').status_code,401)
        self.assertEqual(self.db_count('notes'),0)

    def test_failure_after_insert_rolls_back_and_keeps_all_draft_data(self):
        self.save()
        before=self.state()
        with self.store.connection() as db:
            db.executescript("CREATE TRIGGER fail_archive BEFORE UPDATE OF revision ON image_drafts WHEN EXISTS (SELECT 1 FROM notes WHERE image_draft_id = OLD.id) BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END;")
        self.assertEqual(self.archive().status_code,503)
        self.assertEqual(self.db_count('notes'),0)
        self.assertEqual(self.state(),before)
        self.assertEqual(len(list(self.store.images.iterdir())),1)
        with self.store.connection() as db: db.execute('DROP TRIGGER fail_archive')
        self.assertEqual(self.archive().status_code,200)

    def test_archived_draft_is_read_only_and_cannot_be_deleted_or_recharged(self):
        self.save()
        archived=self.archive().json()
        self.assertEqual(self.save('不应覆盖','不应修改').status_code,409)
        self.assertEqual(self.save().status_code,200)
        with patch.object(self.fake,'prepare') as prepare:
            self.assertEqual(self.client.post(self.api+'/ocr',data={'csrf':self.token()}).status_code,409)
        prepare.assert_not_called()
        self.assertEqual(self.client.post(self.book+'/image-drafts/'+self.draft+'/cancel',data={'csrf':self.token()}).status_code,409)
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection() as db:
            db.execute('DELETE FROM image_drafts WHERE id = ?',(self.draft,))
        self.assertEqual(self.db_count('notes'),1)
        self.assertEqual(self.state()['original_text'],'校对原文')
        page=self.client.get(self.book+'/images').text
        self.assertIn('已归档来源（1）',page)
        self.assertIn('已保存的图片草稿（0）',page)
        self.assertNotIn('取消并删除这张草稿',page)
        self.assertIn(archived['note_id'],page)

    def test_ocr_history_image_and_retention_survive_archival_and_cleanup(self):
        self.save()
        service=self.app.state.drafts
        run_id=service.claim(self.book.split('/')[-1],self.draft,'')
        self.assertEqual(self.archive().status_code,409)
        service.finish(run_id,result('首次识别原文'))
        next_id=service.claim(self.book.split('/')[-1],self.draft,run_id)
        service.finish(next_id,result('后续识别原文'))
        archived=self.archive().json()
        with self.store.connection() as db:
            db.execute('UPDATE image_drafts SET editing_started = 0, expires_at = 1 WHERE id = ?',(self.draft,))
        self.store.cleanup()
        self.assertEqual(self.state()['first_ocr_text'],'首次识别原文')
        self.assertEqual(self.state()['latest_ocr_text'],'后续识别原文')
        self.assertEqual(self.db_count('ocr_runs'),2)
        self.assertEqual(self.client.get(self.book+'/image-drafts/'+self.draft+'/file').content,picture())
        self.assertEqual(self.client.get(archived['note_url']).status_code,200)
        self.assertEqual(self.fake.calls,0)

    def test_missing_image_does_not_create_a_formal_note(self):
        self.save()
        next(self.store.images.iterdir()).unlink()
        self.assertEqual(self.archive().status_code,503)
        self.assertEqual(self.db_count('notes'),0)
        self.assertEqual(self.state()['first_comment'],'一读内容')

    def test_markdown_first_then_images_keep_single_import_and_order(self):
        preview=self.upload(self.book,'“Markdown 原文”原有一读'.encode()).headers['location']
        self.assertEqual(self.client.post(preview+'/confirm',data={'csrf':self.token()}).status_code,303)
        self.save()
        self.archive()
        self.assertEqual(self.upload(self.book).status_code,409)
        with self.store.connection() as db:
            rows=db.execute('SELECT source_type,sort_order FROM notes ORDER BY sort_order').fetchall()
        self.assertEqual([tuple(row) for row in rows],[('zotero',0),('image',1)])
        self.assertEqual(self.db_count('imports'),1)
        exported=self.client.get(self.book+'/export').text
        self.assertLess(exported.index('Markdown 原文'),exported.index('校对原文'))

    def test_images_first_then_markdown_and_more_images_all_append(self):
        self.save()
        self.archive()
        preview=self.upload(self.book,'“Markdown 一”一读\n\n“Markdown 二”'.encode()).headers['location']
        self.assertEqual(self.client.post(preview+'/confirm',data={'csrf':self.token()}).status_code,303)
        second=self.make_draft(picture(size=(82,40)))
        service=self.app.state.drafts
        book_id=self.book.split('/')[-1]
        service.save(book_id,second,'追加图片','',0)
        service.archive(book_id,second,1)
        with self.store.connection() as db:
            rows=db.execute('SELECT source_type,sort_order FROM notes ORDER BY sort_order').fetchall()
        self.assertEqual([tuple(row) for row in rows],[('image',0),('zotero',1),('zotero',2),('image',3)])
        self.assertEqual(self.upload(self.book).status_code,409)
        self.assertIn('共 4 条笔记',self.client.get('/').text)

    def test_backup_restore_preserves_archival_links_history_and_export(self):
        self.save()
        run=self.app.state.drafts.claim(self.book.split('/')[-1],self.draft,'')
        self.app.state.drafts.finish(run,result('原始识别内容'))
        self.archive()
        exported=self.client.get(self.book+'/export').content
        output=io.BytesIO()
        backup(self.store.root,output)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'another-machine'
            restore(root,io.BytesIO(output.getvalue()))
            with TestClient(create_app(Settings(root,cookie_secure=False),ocr_client=self.fake),follow_redirects=False) as c:
                self.login(c)
                state=c.get(self.api).json()
                self.assertTrue(state['archived'])
                self.assertEqual(state['first_comment'],'一读内容')
                self.assertEqual(state['first_ocr_text'],'原始识别内容')
                self.assertEqual(c.get(self.book+'/export').content,exported)
                self.assertEqual(c.get(self.book+'/image-drafts/'+self.draft+'/file').content,picture())
                self.assertEqual(c.post(self.book+'/image-drafts/'+self.draft+'/cancel',data={'csrf':self.token(c)}).status_code,409)
        self.assertEqual(self.fake.calls,0)


class ArchiveMigrationTests(unittest.TestCase):
    def test_genuine_v3_backup_migrates_and_preserves_user_shaped_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'v3'
            image,md=legacy_v3(root)
            with sqlite3.connect(root/'noteshub.sqlite3') as db:
                db.row_factory=sqlite3.Row
                before={table:[dict(row) for row in db.execute('SELECT * FROM '+table)] for table in ('books','imports','notes','image_drafts','ocr_runs')}
            files={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ('account.json',image,md)}
            output=io.BytesIO()
            backup(root,output)  # v4 maintenance can back up v3 before migration.
            restored=Path(tmp)/'restored'
            restore(restored,io.BytesIO(output.getvalue()))
            store=Store(Settings(restored))
            Store(Settings(restored))
            with store.connection() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],6)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                for table,rows in before.items():
                    after=[dict(row) for row in db.execute('SELECT * FROM '+table)]
                    for old,new in zip(rows,after):
                        self.assertEqual(old,{key:new[key] for key in old},table)
                    self.assertEqual(len(rows),len(after))
            self.assertEqual(files,{name:hashlib.sha256((restored/name).read_bytes()).hexdigest() for name in files})
            service=DraftService(store,FakeOCR())
            archived=service.archive('book','draft',2)
            self.assertEqual(archived['original_text'],'原'*137)
            self.assertEqual(archived['first_comment'],'评'*17)
            self.assertEqual(archived['first_ocr_text'],'识'*134)
            with store.connection() as db:
                note=db.execute('SELECT * FROM notes WHERE id = ?', (archived['note_id'],)).fetchone()
                self.assertEqual(note['sort_order'],1)
                self.assertEqual(db.execute("SELECT second_comment,status FROM notes WHERE id = 'old-note'").fetchone()[:],('旧版二读','done'))
            self.assertEqual(service.client.calls,0)

    def test_failed_migration_rolls_back_without_erasing_legacy_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'v3'
            legacy_v3(root)
            with sqlite3.connect(root/'noteshub.sqlite3') as db:
                db.execute("UPDATE notes SET source_type = 'unsupported-legacy-source'")
            with self.assertRaises(sqlite3.IntegrityError):
                Store(Settings(root))
            with sqlite3.connect(root/'noteshub.sqlite3') as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
                self.assertEqual(db.execute('SELECT first_comment FROM image_drafts').fetchone()[0],'评'*17)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM notes').fetchone()[0],1)
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name IN ('notes_v4','image_draft_identity')").fetchall(),[])
