"""No cloud inference: leased worker protocol, unchanged comments, safe cleanup."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
import time
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from noteshub.local_jobs import LocalJobs, cleanup_photos, RETENTION_SECONDS
from noteshub.maintenance import backup, restore
from noteshub.paper import PaperService
from tests.test_images import picture
from tests.test_paper import uid
from tests.test_web import WebCase

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('noteshub_test_worker',ROOT/'tools/local_ocr/worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)


class LocalJobsTests(WebCase):
    def setUp(self):
        super().setUp()
        self.book=self.create_book().split('/')[-1];self.note=uid()
        self.service=PaperService(self.store);self.jobs=LocalJobs(self.store)
        self.payload={'mutation_id':uid(),'book_id':self.book,'revision':0,'original_text':'人工校对原文',
                      'first_comment':'一读不能覆盖','second_comment':'二读也保留','photo_ids':[]}
        self.service.sync(self.note,self.payload)
        self.photos=[uid(),uid()]
        for photo in self.photos:self.service.upload(self.note,photo,picture())
        self.payload.update(mutation_id=uid(),revision=1,photo_ids=self.photos)
        self.saved=self.service.sync(self.note,self.payload)
        self.session_headers={'X-CSRF-Token':self.token()}
        token=self.client.post('/api/paper/worker-tokens',json={'label':'fixture'},headers=self.session_headers).json()
        self.token=token['token'];self.worker_id=token['id']
        self.worker_headers={'Authorization':'Bearer '+self.token}
        self.base='/api/paper/notes/'+self.note

    def queue(self):
        return self.jobs.queue(self.note,{'job_id':uid(),'revision':self.saved['revision']})

    def claim(self):return self.jobs.claim(self.worker_id,self.token,uid())

    def completed(self):
        job=self.queue();claim=self.claim()
        payload={'lease':claim['lease'],'engine':'synthetic engine','failed':False,
                 'results':[{'photo_id':p,'text':'第 '+str(i+1)+' 页原文'} for i,p in enumerate(self.photos)]}
        self.assertEqual(self.jobs.finish(self.worker_id,job['id'],payload),{'state':'succeeded'})
        return job,payload

    def test_queue_batch_and_computer_off_do_not_block_notes(self):
        job=self.queue()
        self.assertEqual(job['state'],'pending')
        self.assertEqual(self.queue()['id'],job['id'])
        self.assertEqual(self.jobs.batch(),{'queued':0,'skipped':1,'limit':200})
        self.assertEqual(self.client.get(self.base).json()['first_comment'],'一读不能覆盖')
        self.assertEqual(self.db_count('ocr_runs'),0)
        self.assertEqual(self.db_count('paper_ocr_jobs'),1)

    def test_claim_receipt_concurrency_and_expired_lease(self):
        job=self.queue();claim_id=uid()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.jobs.claim(self.worker_id,self.token,claim_id),range(4)))
        self.assertTrue(all(r==results[0] for r in results))
        old=results[0]
        self.assertIsNone(self.claim()['job'])
        with self.store.connection() as db:db.execute('UPDATE paper_ocr_jobs SET lease_until=1')
        with self.assertRaises(HTTPException):self.jobs.heartbeat(self.worker_id,job['id'],old['lease'])
        new=self.claim();self.assertNotEqual(old['lease'],new['lease'])
        with self.assertRaises(HTTPException):self.jobs.image(self.worker_id,job['id'],self.photos[0],old['lease'])
        self.assertTrue(self.jobs.heartbeat(self.worker_id,job['id'],new['lease'])['renewed'])
        self.assertEqual(self.jobs.image(self.worker_id,job['id'],self.photos[1],new['lease'])[0],picture())

    def test_results_order_idempotence_and_comments_never_changed(self):
        job,payload=self.completed()
        self.assertEqual(self.jobs.finish(self.worker_id,job['id'],payload),{'state':'succeeded'})
        note=self.client.get(self.base).json()
        for name in ('first_comment','second_comment','original_text','revision'):
            self.assertEqual(note[name],self.saved[name])
        payload['results'][0]['text']='不可改写历史'
        with self.assertRaises(HTTPException):self.jobs.finish(self.worker_id,job['id'],payload)
        self.assertEqual(len(self.client.get(self.base+'/ocr').json()['jobs']),1)
        self.queue();claim=self.claim()
        invalid={'lease':claim['lease'],'engine':'fake','results':[{'photo_id':p,'text':'x'} for p in reversed(self.photos)]}
        self.assertEqual(self.client.post('/api/local-ocr/jobs/'+claim['job']['id']+'/result',json=invalid,headers=self.worker_headers).status_code,422)

    def test_scoped_auth_csrf_revoke_and_unrelated_image(self):
        job=self.queue();claim=self.claim()
        with TestClient(self.app,follow_redirects=False) as anonymous:
            self.assertEqual(anonymous.get('/api/paper/notes',headers=self.worker_headers).status_code,401)
            self.assertEqual(anonymous.post('/api/local-ocr/claim',json={'claim_id':uid()}).status_code,401)
            response=anonymous.get('/api/local-ocr/jobs/'+job['id']+'/photos/'+uid(),headers={**self.worker_headers,'X-OCR-Lease':claim['lease']})
            self.assertEqual(response.status_code,404)
        self.assertEqual(self.client.post(self.base+'/ocr',json={'job_id':uid(),'revision':2}).status_code,403)
        self.assertEqual(self.client.post('/api/paper/worker-tokens/'+self.worker_id+'/revoke',json={},headers=self.session_headers).status_code,200)
        with TestClient(self.app) as anonymous:
            self.assertEqual(anonymous.post('/api/local-ocr/claim',json={'claim_id':uid()},headers=self.worker_headers).status_code,401)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM paper_ocr_jobs').fetchone()[0],'pending')
            self.assertNotIn(self.token,db.execute('SELECT token_hash FROM paper_worker_tokens').fetchone()[0])

    def test_unconfirmed_images_never_purge_and_confirmation_can_be_undone(self):
        with self.store.connection() as db:db.execute('UPDATE paper_photos SET purge_after=1')
        cleanup_photos(self.store)
        self.assertEqual(len(list((self.store.root/'paper-images').iterdir())),2)
        with self.assertRaises(HTTPException):self.jobs.confirm(self.note,{'revision':2})
        self.completed()
        confirmed=self.jobs.confirm(self.note,{'revision':2})
        self.assertEqual(confirmed['confirmed_revision'],2)
        self.assertTrue(all(abs(p['purge_after']-time.time()-RETENTION_SECONDS)<3 for p in confirmed['photos']))
        cleanup_photos(self.store)
        self.assertEqual(len(list((self.store.root/'paper-images').iterdir())),2)
        self.jobs.confirm(self.note,{'revision':2},True)
        with self.store.connection() as db:db.execute('UPDATE paper_photos SET purge_after=1')
        cleanup_photos(self.store)
        self.assertEqual(len(list((self.store.root/'paper-images').iterdir())),2)

    def test_queued_retry_requires_fresh_confirmation_after_completion(self):
        self.completed()
        self.queue()
        with self.assertRaises(HTTPException):self.jobs.confirm(self.note,{'revision':2})
        self.assertIsNone(self.client.get(self.base).json()['confirmed_revision'])

    def test_model_failure_marks_task_failed_and_keeps_note_and_images(self):
        self.queue();test=self
        class Adapter:
            def call(self,path,payload=None,lease=None,binary=False):
                headers={**test.worker_headers}
                if lease:headers['X-OCR-Lease']=lease
                r=test.client.post(path,json=payload,headers=headers) if payload is not None else test.client.get(path,headers=headers)
                if r.status_code!=200:raise worker.WorkerError(f'HTTP {r.status_code}')
                return r.content if binary else r.json()
        def broken(_):raise RuntimeError('synthetic inference failure')
        with tempfile.TemporaryDirectory() as tmp:worker.run_batch(Adapter(),Path(tmp),3,broken)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT state FROM paper_ocr_jobs').fetchone()[0],'failed')
        self.assertEqual(self.client.get(self.base).json()['first_comment'],'一读不能覆盖')
        self.assertEqual(len(list((self.store.root/'paper-images').iterdir())),2)

    def test_any_text_edit_including_old_reader_cancels_confirmation(self):
        self.completed();self.jobs.confirm(self.note,{'revision':2})
        response=self.client.post('/api/books/'+self.book+'/notes/'+self.note,data={'csrf':super().token(),'revision':'2','second_comment':'再次二读'})
        self.assertEqual(response.status_code,200)
        current=self.client.get(self.base).json()
        self.assertEqual(current['revision'],3)
        self.assertIsNone(current['confirmed_revision'])
        self.assertTrue(all(p['purge_after'] is None for p in current['photos']))
        self.assertEqual(self.client.post('/api/books/'+self.book+'/notes/'+self.note,data={'csrf':super().token(),'revision':'2','second_comment':'旧标签页不能覆盖'}).status_code,409)
        with self.assertRaises(HTTPException):self.jobs.confirm(self.note,{'revision':2})

    def test_deadline_purge_keeps_text_history_and_backups_restorable(self):
        self.completed();self.jobs.confirm(self.note,{'revision':2})
        before=io.BytesIO();backup(self.store.root,before)
        with self.store.connection() as db:db.execute('UPDATE paper_photos SET purge_after=1')
        cleanup_photos(self.store)
        self.assertEqual(list((self.store.root/'paper-images').iterdir()),[])
        self.assertIn('一读不能覆盖',self.client.get('/books/'+self.book+'/export').text)
        self.assertEqual(self.client.get(self.base+'/ocr').json()['jobs'][0]['state'],'succeeded')
        self.assertEqual(self.client.get(self.base+'/photos/'+self.photos[0]).status_code,404)
        after=io.BytesIO();backup(self.store.root,after)
        for data,expected in ((before.getvalue(),2),(after.getvalue(),0)):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'restore';restore(root,io.BytesIO(data))
                self.assertEqual(len(list((root/'paper-images').glob('*'))),expected)
                import sqlite3
                with sqlite3.connect(root/'noteshub.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM paper_ocr_jobs').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM paper_worker_tokens WHERE revoked_at IS NULL').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM paper_photos WHERE purge_after IS NOT NULL AND purged_at IS NULL').fetchone()[0],0)

    def test_running_task_protects_files_and_restores_to_pending(self):
        self.completed();self.jobs.confirm(self.note,{'revision':2})
        self.queue();claim=self.claim()
        with self.store.connection() as db:
            db.execute('UPDATE paper_photos SET purge_after=1')
            db.execute('UPDATE paper_entries SET confirmed_revision=revision')
        cleanup_photos(self.store)
        self.assertEqual(len(list((self.store.root/'paper-images').iterdir())),2)
        out=io.BytesIO();backup(self.store.root,out)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'restore';restore(root,io.BytesIO(out.getvalue()))
            import sqlite3
            with sqlite3.connect(root/'noteshub.sqlite3') as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM paper_ocr_jobs WHERE state='pending'").fetchone()[0],1)

    def test_real_worker_outbox_replays_result_without_inference_or_duplicate(self):
        self.queue();calls=[];test=self
        class Adapter:
            lose=True
            def call(self,path,payload=None,lease=None,binary=False):
                headers={**test.worker_headers}
                if lease:headers['X-OCR-Lease']=lease
                r=test.client.post(path,json=payload,headers=headers) if payload is not None else test.client.get(path,headers=headers)
                if r.status_code!=200:raise worker.WorkerError(f'HTTP {r.status_code}')
                if path.endswith('/result') and self.lose:
                    self.lose=False;raise worker.WorkerError('synthetic lost result response')
                return r.content if binary else r.json()
        client=Adapter()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def recognize(raw):calls.append(raw);return '本地模型原文'
            with self.assertRaises(worker.WorkerError):worker.run_batch(client,root,5,recognize)
            self.assertEqual(len(calls),2)
            self.assertEqual(len(list((root/'outbox').glob('*.json'))),1)
            worker.run_batch(client,root,5,recognize)
            self.assertEqual(len(calls),2)
            self.assertEqual(list((root/'outbox').glob('*.json')),[])
            self.assertEqual(self.db_count('paper_ocr_jobs'),1)
            self.assertEqual(self.client.get(self.base).json()['first_comment'],'一读不能覆盖')

    def test_https_required_credentials_not_redirected_and_config_exclusive(self):
        for url in ('http://example.org','https://user:pass@example.org','https://example.org/path','https://example.org?q=x'):
            with self.assertRaises(worker.WorkerError):worker.validate_url(url)
        self.assertEqual(worker.validate_url('https://example.org/'),'https://example.org')
        with self.assertRaises(worker.WorkerError):worker.NoRedirect().redirect_request(None,None,None,None,None,None)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json';worker.private_write(path,{'url':'fixture'},exclusive=True)
            with self.assertRaises(FileExistsError):worker.private_write(path,{'url':'overwrite'},exclusive=True)
            self.assertEqual(json.loads(path.read_text()),{'url':'fixture'})
            with worker.worker_lock(Path(tmp)):
                with self.assertRaises(worker.WorkerError):
                    with worker.worker_lock(Path(tmp)):pass


class ClipboardConfigureTests(unittest.TestCase):
    def test_clipboard_config_never_echoes_secret_and_keeps_exclusive_write(self):
        token='nh_'+'fixture_'*8
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'config.json'
            output=io.StringIO();tls=worker.ssl.create_default_context()
            with patch.object(worker.ssl,'create_default_context',return_value=tls), patch.object(worker.sys,'platform','win32'), patch.object(worker.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=token+'\r\n')) as call, patch.object(worker.getpass,'getpass',side_effect=AssertionError('interactive input used')), redirect_stdout(output):
                worker.main(['--config',str(target),'configure','--url','https://example.org','--clipboard'])
            self.assertEqual(json.loads(target.read_text())['token'],token)
            self.assertNotIn(token,output.getvalue())
            self.assertNotIn(token,' '.join(call.call_args.args[0]))
            self.assertIn('Get-Clipboard -Raw',call.call_args.args[0][-1])
            with patch.object(worker,'clipboard_token',return_value=token),redirect_stderr(output),self.assertRaises(SystemExit):
                worker.main(['--config',str(target),'configure','--url','https://example.org','--clipboard'])
            self.assertEqual(json.loads(target.read_text())['token'],token)
            self.assertNotIn(token,output.getvalue())

    def test_bad_clipboard_is_rejected_without_echo_or_config(self):
        for value in ('','copied command text','nh_'+'x'*45+'\x16'):
            with tempfile.TemporaryDirectory() as folder:
                target=Path(folder)/'config.json';output=io.StringIO()
                with patch.object(worker,'clipboard_token',return_value=value),redirect_stderr(output),self.assertRaises(SystemExit):
                    worker.main(['--config',str(target),'configure','--url','https://example.org','--clipboard'])
                self.assertFalse(target.exists())
                if value:self.assertNotIn(value,output.getvalue())

    def test_clipboard_errors_are_private_and_windows_only(self):
        secret='fixture sensitive clipboard error'
        for result in (types.SimpleNamespace(returncode=1,stdout=secret),):
            with patch.object(worker.sys,'platform','win32'),patch.object(worker.subprocess,'run',return_value=result),self.assertRaises(worker.WorkerError) as error:
                worker.clipboard_token()
            self.assertNotIn(secret,str(error.exception))
        with patch.object(worker.sys,'platform','linux'),patch.object(worker.subprocess,'run') as call,self.assertRaises(worker.WorkerError):
            worker.clipboard_token()
        call.assert_not_called()
        with patch.object(worker.sys,'platform','win32'),patch.object(worker.subprocess,'run',side_effect=worker.subprocess.TimeoutExpired(secret,10)),self.assertRaises(worker.WorkerError) as error:
            worker.clipboard_token()
        self.assertNotIn(secret,str(error.exception))
