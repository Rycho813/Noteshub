"""Synthetic mixed v6 production-shaped data; no production access."""
from contextlib import closing
import io
import sqlite3
from unittest.mock import patch
from pathlib import Path

from fastapi import HTTPException
from noteshub.db import Store
from noteshub.drafts import DraftService
from noteshub.images import validate_image
from noteshub.paper import PaperService
from noteshub.local_jobs import LocalJobs
from noteshub.maintenance import backup, restore
from tests.test_web import WebCase
from tests.test_images import picture
from tests.test_paper import uid


class IntegrationTests(WebCase):
    def setUp(self):
        super().setUp()
        self.paper=PaperService(self.store)
        self.books=[self.mixed('目标书'),self.mixed('保留书')]

    def mixed(self,title):
        url=self.create_book(title);book=url.split('/')[-1]
        preview=self.upload(url).headers['location']
        self.client.post(preview+'/confirm',data={'csrf':self.token()})
        raw=picture();draft=self.store.save_image_draft(book,raw,validate_image(raw,self.settings))
        service=DraftService(self.store,None)
        service.save(book,draft,'图片原文','图片评述',0)
        archived=service.archive(book,draft,1)
        note=uid();payload=dict(mutation_id=uid(),book_id=book,revision=0,first_comment='纸书想法',photo_ids=[])
        self.paper.sync(note,payload)
        photos=[uid(),uid()]
        for photo in photos:self.paper.upload(note,photo,raw)
        # Include a detached photo, which still belongs to the paper original.
        self.paper.sync(note,{**payload,'mutation_id':uid(),'revision':1,'photo_ids':photos[:1]})
        job=uid();LocalJobs(self.store).queue(note,{'job_id':job,'revision':2})
        with self.store.connection() as db:
            worker=uid()
            db.execute('INSERT INTO paper_worker_tokens(id,token_hash,label,created_at) VALUES (?,?,?,1)',(worker,uid(),'合成工人'))
            db.execute('INSERT INTO paper_worker_claims VALUES (?,?,?,?,1)',(uid(),worker,job,uid()))
            db.execute("INSERT INTO ocr_runs(id,draft_id,state,started_at,text) VALUES (?,?,'succeeded',1,'识别历史')",(uid(),draft))
            paths=[r[0] for r in db.execute('SELECT file_path FROM image_drafts WHERE book_id=?',(book,))]
            paths += [r[0] for r in db.execute('SELECT file_path FROM paper_photos WHERE note_id=?',(note,))]
        return dict(url=url,id=book,note=note,photos=photos,paths=paths,draft=draft,payload=payload)

    def snapshot(self):
        with self.store.connection() as db:
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('deleted_entities','retained_files') ORDER BY name")]
            return {t:[tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in tables}

    def test_v6_upgrade_preserves_every_existing_row_schema_and_file(self):
        with self.store.connection() as db:
            db.execute('DROP TABLE retained_files');db.execute('DROP TABLE deleted_entities')
            db.execute('PRAGMA user_version=6')
            schema=[tuple(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")]
        before=self.snapshot()
        files={p:p.read_bytes() for directory in ('sources','images','paper-images') for p in (self.store.root/directory).iterdir()}
        Store(self.settings);Store(self.settings)
        self.assertEqual(self.snapshot(),before)
        with self.store.connection() as db:
            after=[tuple(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT IN ('deleted_entities','retained_files','sqlite_autoindex_deleted_entities_1','sqlite_autoindex_retained_files_1') ORDER BY name")]
            self.assertEqual(schema,after)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],7)
        self.assertEqual(files,{p:p.read_bytes() for p in files})

    def test_single_paper_delete_removes_attached_detached_photos_and_backup(self):
        book=self.books[0]
        self.store.delete_note(book['id'],book['note'])
        for path in book['paths'][1:]:self.assertFalse((self.store.root/path).exists())
        self.assertTrue((self.store.root/book['paths'][0]).exists())
        self.store.cleanup()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM retained_files').fetchone()[0],0)
            self.assertFalse(db.execute('SELECT 1 FROM paper_ocr_jobs WHERE note_id=?',(book['note'],)).fetchone())
        with self.assertRaises(HTTPException) as error:self.paper.sync(book['note'],book['payload'])
        self.assertEqual(error.exception.status_code,410)
        archive=io.BytesIO();backup(self.store.root,archive)
        root=self.store.root/'restore';restore(root,io.BytesIO(archive.getvalue()))
        for path in book['paths'][1:]:self.assertFalse((root/path).exists())
        self.store.delete_book(book['id'])
        for path in book['paths']:self.assertFalse((self.store.root/path).exists())

    def test_whole_book_deletion_removes_only_its_mixed_assets(self):
        target,other=self.books
        before={p:(self.store.root/p).read_bytes() for p in other['paths']}
        self.store.delete_book(target['id'])
        for path in target['paths']:self.assertFalse((self.store.root/path).exists())
        self.assertEqual(before,{p:(self.store.root/p).read_bytes() for p in before})
        with self.store.connection() as db:
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notes').fetchone()[0],3)
        self.assertEqual(self.client.post('/api/paper/books/'+target['id'],json={'title':'复活'},headers={'x-csrf-token':self.token()}).status_code,410)

    def test_mixed_rollback_restores_images_sources_and_rows(self):
        target=self.books[0];before=self.snapshot()
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER injected BEFORE DELETE ON books BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.Error):self.store.delete_book(target['id'])
        self.assertEqual(self.snapshot(),before)
        for path in target['paths']:self.assertTrue((self.store.root/path).exists())
        self.assertFalse(list(self.store.root.rglob('*.deleting')))

    def test_single_image_delete_removes_source_and_ocr_history(self):
        book=self.books[0]
        with self.store.connection() as db:
            note=db.execute('SELECT id FROM notes WHERE image_draft_id=?',(book['draft'],)).fetchone()[0]
        self.store.delete_note(book['id'],note);self.store.cleanup()
        self.assertFalse((self.store.root/book['paths'][0]).exists())
        with self.store.connection() as db:
            self.assertFalse(db.execute('SELECT 1 FROM ocr_runs WHERE draft_id=?',(book['draft'],)).fetchone())
            self.assertFalse(db.execute('SELECT 1 FROM image_drafts WHERE id=?',(book['draft'],)).fetchone())
        self.assertEqual(self.client.get('/api/books/'+book['id']+'/image-drafts/'+book['draft']).status_code,404)
        from noteshub.retire_drafts import retire
        self.assertEqual(retire(self.store.root,apply=True)['drafts'],0)
        self.assertFalse((self.store.root/book['paths'][0]).exists())

    def test_purged_residual_is_removed_by_note_and_book_delete(self):
        for index,book in enumerate(self.books):
            with self.store.connection() as db:
                db.execute('UPDATE paper_photos SET purged_at=1 WHERE note_id=?',(book['note'],))
            if index:self.store.delete_book(book['id'])
            else:self.store.delete_note(book['id'],book['note'])
            for path in book['paths'][1:]:self.assertFalse((self.store.root/path).exists())

    def test_single_photo_delete_rollback_restores_files_and_rows(self):
        book=self.books[0];before=self.snapshot()
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_note BEFORE DELETE ON notes BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.Error):self.store.delete_note(book['id'],book['note'])
        self.assertEqual(self.snapshot(),before)
        for path in book['paths']:self.assertTrue((self.store.root/path).exists())
        self.assertFalse(list(self.store.root.rglob('*.deleting')))

    def test_single_photo_postcommit_failure_reports_pending_and_recovers(self):
        book=self.books[0];unlink=Path.unlink
        def fail(path,*a,**kw):
            if path.name.endswith('.deleting'):raise PermissionError('injected')
            return unlink(path,*a,**kw)
        with patch.object(Path,'unlink',fail):
            r=self.client.post(book['url']+'/notes/'+book['note']+'/delete',data={'csrf':self.token(),'confirm':'delete'})
        self.assertEqual(r.status_code,503)
        self.assertIn('未能全部完成',r.text)
        Store(self.settings)
        self.assertFalse(list(self.store.root.rglob('*.deleting')))
        for path in book['paths'][1:]:self.assertFalse((self.store.root/path).exists())

    def test_deleted_photo_id_and_legacy_retained_path_cannot_be_reused(self):
        a,b=self.books
        self.store.delete_note(a['id'],a['note'])
        with self.assertRaises(HTTPException) as e:self.paper.upload(b['note'],a['photos'][0],picture())
        self.assertEqual(e.exception.status_code,409)
        with self.store.connection() as db:
            p=db.execute('SELECT file_path,sha256,byte_size FROM paper_photos WHERE id=?',(b['photos'][0],)).fetchone()
            db.execute('INSERT INTO retained_files VALUES (?,?,?,?)',(p['file_path'],b['id'],p['sha256'],p['byte_size']))
        with self.assertRaises(HTTPException):self.paper.upload(b['note'],b['photos'][0],picture())

    def test_deletion_feed_is_authenticated_and_only_exposes_ids(self):
        from fastapi.testclient import TestClient
        with TestClient(self.app,follow_redirects=False) as client:
            self.assertEqual(client.get('/api/paper/deletions').status_code,401)
        a,b=self.books
        self.store.delete_note(a['id'],a['note']);self.store.delete_book(b['id'])
        rows=self.client.get('/api/paper/deletions').json()['deleted']
        self.assertIn({'kind':'note','id':a['note']},rows)
        self.assertIn({'kind':'book','id':b['id']},rows)
        self.assertIn({'kind':'photo','id':a['photos'][0]},rows)
        self.assertTrue(all(set(r)=={'kind','id'} and r['kind'] in ('note','book','photo') for r in rows))

    def test_v6_upgrade_preserves_eleven_paper_notes_and_thirty_photos(self):
        book=self.create_book('置身天下（合成保留验证）').split('/')[-1]
        raw=picture()
        for i in range(11):
            note=uid()
            self.paper.sync(note,dict(mutation_id=uid(),book_id=book,revision=0,first_comment=f'合成想法 {i}'))
            for _ in range(3 if i<8 else 2):self.paper.upload(note,uid(),raw)
        with self.store.connection() as db:
            before=[tuple(r) for r in db.execute('SELECT * FROM notes WHERE book_id=? ORDER BY sort_order',(book,))]
            paths=[r[0] for r in db.execute('SELECT file_path FROM paper_photos WHERE note_id IN (SELECT id FROM notes WHERE book_id=?)',(book,))]
            db.execute('DROP TABLE retained_files');db.execute('DROP TABLE deleted_entities');db.execute('PRAGMA user_version=6')
        files={p:(self.store.root/p).read_bytes() for p in paths}
        Store(self.settings);Store(self.settings)
        with self.store.connection() as db:
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM notes WHERE book_id=? ORDER BY sort_order',(book,))])
        self.assertEqual(len(before),11);self.assertEqual(len(files),30)
        self.assertEqual(files,{p:(self.store.root/p).read_bytes() for p in paths})

    def test_upgrade_failure_rolls_back_both_new_tables_and_version(self):
        with self.store.connection() as db:
            db.execute('DROP TABLE retained_files');db.execute('DROP TABLE deleted_entities');db.execute('PRAGMA user_version=6')
        before=self.snapshot()
        from noteshub import deletion
        with patch.object(deletion,'SCHEMA',deletion.SCHEMA+'INVALID SQL;'):
            with self.assertRaises(sqlite3.Error):Store(self.settings)
        self.assertEqual(self.snapshot(),before)
        with self.store.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],6)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='deleted_entities'").fetchone())
        Store(self.settings)

    def test_corrupt_cross_book_asset_reference_refuses_deletion(self):
        target,other=self.books
        with self.store.connection() as db:
            row=db.execute('SELECT sha256,byte_size FROM paper_photos WHERE file_path=?',(other['paths'][1],)).fetchone()
            db.execute('INSERT INTO retained_files VALUES (?,?,?,?)',(other['paths'][1],target['id'],*row))
        with self.assertRaises(OSError):self.store.delete_book(target['id'])
        for path in other['paths']:self.assertTrue((self.store.root/path).exists())

    def test_paper_staged_files_recover_from_committed_database(self):
        target=self.books[0];path=self.store.root/target['paths'][1]
        stage=path.with_name(path.name+'.deleting');path.rename(stage)
        Store(self.settings);self.assertTrue(path.exists());self.assertFalse(stage.exists())
        unlink=Path.unlink
        def fail(path,*a,**kw):
            if path.name.endswith('.deleting'):raise OSError('injected purge')
            return unlink(path,*a,**kw)
        with patch.object(Path,'unlink',fail):
            with self.assertRaises(OSError):self.store.delete_book(target['id'])
        Store(self.settings)
        self.assertFalse(list(self.store.root.rglob('*.deleting')))
        for relative in target['paths']:self.assertFalse((self.store.root/relative).exists())
