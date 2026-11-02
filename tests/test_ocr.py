"""All provider tests stub the HTTP transport; never call Tencent or incur fees."""
import base64
import io
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from requests import Response
from tencentcloud.common.retry import NoopRetryer
from tencentcloud.common.profile.client_profile import ClientProfile

from noteshub.auth import provision
from noteshub.config import Settings
from noteshub.db import Store, SCHEMA, IMAGE_SCHEMA
from noteshub.maintenance import backup, restore
from noteshub.ocr import OCRFailure, TencentOCR, normalize_response
from noteshub.web import create_app
from tests.test_images import picture
from tests.test_web import WebCase


def result(text='识别出的中文原文\n第二行', request_id='mock-request'):
    return normalize_response({'TextDetections': [{'DetectedText':line,'Confidence':99} for line in text.split('\n')], 'RequestId':request_id})


class FakeOCR:
    def __init__(self):
        self.calls = 0
        self.next = result()
        self.started = threading.Event()
        self.release = None

    def configured(self):
        return True

    def prepare(self):
        pass

    def recognize(self, raw):
        self.calls += 1
        self.started.set()
        if self.release is not None and not self.release.wait(5):
            raise OCRFailure('NetworkError')
        if isinstance(self.next, Exception):
            raise self.next
        return self.next


class SDKTests(unittest.TestCase):
    def test_real_sdk_serializes_signs_once_and_preserves_tls(self):
        response = Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps({'Response':{'TextDetections':[{'DetectedText':'中文书页','Confidence':98}], 'RequestId':'test-request'}}).encode()
        response.encoding = 'utf-8'
        bindings = {'NOTESHUB_OCR_ENABLED':'true','NOTESHUB_TENCENT_SECRET_ID':'unit-test-id',
                    'NOTESHUB_TENCENT_SECRET_KEY':'unit-test-key','NOTESHUB_TENCENT_CREDENTIALS_FILE':''}
        with patch.dict(os.environ,bindings), patch('noteshub.ocr.ClientProfile',wraps=ClientProfile) as profile, patch('requests.sessions.Session.request',return_value=response) as transport:
            parsed = TencentOCR().recognize(b'synthetic-image')
        self.assertEqual(parsed.text,'中文书页')
        self.assertIsInstance(profile.call_args.kwargs['retryer'],NoopRetryer)
        self.assertTrue(profile.call_args.kwargs['disable_region_breaker'])
        self.assertEqual(transport.call_count,1)
        args,kwargs = transport.call_args
        self.assertIn('https://ocr.tencentcloudapi.com',kwargs['url'])
        self.assertEqual(kwargs['headers']['X-TC-Action'],'GeneralAccurateOCR')
        self.assertTrue(kwargs['headers']['Authorization'].startswith('TC3-HMAC-SHA256'))
        self.assertEqual(json.loads(kwargs['data'])['ImageBase64'],base64.b64encode(b'synthetic-image').decode())
        self.assertNotIn('ImageUrl',json.loads(kwargs['data']))
        self.assertEqual(kwargs['timeout'],30)
        self.assertIsNot(kwargs['verify'],False)

    def test_sdk_error_is_not_retried_or_exposed(self):
        response = Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'application/json'
        response._content = json.dumps({'Response':{'Error':{'Code':'AuthFailure.SignatureFailure','Message':'DO-NOT-LOG-FAKE-SECRET'},'RequestId':'test'}}).encode()
        response.encoding = 'utf-8'
        bindings = {'NOTESHUB_OCR_ENABLED':'true','NOTESHUB_TENCENT_SECRET_ID':'unit-test-id',
                    'NOTESHUB_TENCENT_SECRET_KEY':'unit-test-key','NOTESHUB_TENCENT_CREDENTIALS_FILE':''}
        with patch.dict(os.environ,bindings), patch('requests.sessions.Session.request',return_value=response) as transport:
            with self.assertRaises(OCRFailure) as caught:
                TencentOCR().recognize(b'synthetic-image')
        self.assertEqual(transport.call_count,1)
        self.assertEqual(caught.exception.code,'AuthFailure')
        self.assertNotIn('DO-NOT-LOG',str(caught.exception))

    def test_transient_http_failure_is_not_automatically_retried(self):
        response = Response()
        response.status_code = 503
        response._content = b'synthetic gateway failure'
        bindings = {'NOTESHUB_OCR_ENABLED':'true','NOTESHUB_TENCENT_SECRET_ID':'unit-test-id',
                    'NOTESHUB_TENCENT_SECRET_KEY':'unit-test-key','NOTESHUB_TENCENT_CREDENTIALS_FILE':''}
        with patch.dict(os.environ,bindings), patch('requests.sessions.Session.request',return_value=response) as transport:
            with self.assertRaises(OCRFailure) as caught:
                TencentOCR().recognize(b'synthetic-image')
        self.assertEqual(caught.exception.code,'NetworkError')
        self.assertEqual(transport.call_count,1)

    def test_noop_retry_profile_and_disabled_credentials(self):
        bindings = {'NOTESHUB_OCR_ENABLED':'false','NOTESHUB_TENCENT_SECRET_ID':'unit-test-id',
                    'NOTESHUB_TENCENT_SECRET_KEY':'unit-test-key'}
        with patch.dict(os.environ,bindings), patch('requests.sessions.Session.request') as transport:
            with self.assertRaises(OCRFailure):
                TencentOCR().recognize(b'anything')
        transport.assert_not_called()
        self.assertIsInstance(NoopRetryer(),NoopRetryer)

    def test_credential_file_permissions_and_no_secret_in_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp)/'credentials.json'
            file.write_text(json.dumps({'SecretId':'unit-test-id','SecretKey':'unit-test-key'}))
            with patch.dict(os.environ,{'NOTESHUB_OCR_ENABLED':'true','NOTESHUB_TENCENT_CREDENTIALS_FILE':str(file)}):
                file.chmod(0o600)
                TencentOCR().prepare()
                file.chmod(0o644)
                with self.assertRaises(OCRFailure) as caught:
                    TencentOCR().prepare()
                self.assertNotIn('unit-test-key',str(caught.exception))

    def test_empty_malformed_or_excessive_results_fail_without_truncation(self):
        for payload in ({'TextDetections':[]}, {'TextDetections':[{'DetectedText':'a'*50001}]},
                        {'TextDetections':[{'DetectedText':None}]}, {'TextDetections':[{'DetectedText':'a\x00b'}]}, {}):
            with self.subTest(payload_type=type(payload)):
                with self.assertRaises(OCRFailure):
                    normalize_response(payload)


class OCRTests(WebCase):
    def setUp(self):
        self.fake = FakeOCR()
        with patch('noteshub.web.TencentOCR',return_value=self.fake):
            super().setUp()
        self.book = self.create_book('OCR 书籍')
        response = self.client.post('/api'+self.book+'/image-drafts',data={'csrf':self.token()},files={'file':('crop.png',picture(),'image/png')})
        self.draft_id = response.json()['id']
        self.api = '/api'+self.book+'/image-drafts/'+self.draft_id

    def state(self):
        response = self.client.get(self.api)
        self.assertEqual(response.status_code,200)
        return response.json()

    def start(self, previous=''):
        return self.client.post(self.api+'/ocr',data={'csrf':self.token(),'previous_run_id':previous})

    def wait(self, status='succeeded'):
        deadline = time.monotonic()+4
        while time.monotonic() < deadline:
            current = self.state()
            if current['ocr_state'] == status:
                return current
            time.sleep(.01)
        self.fail('OCR mock did not complete: '+str(current['ocr_state']))

    def save(self, original='人工校对', comment='我的一读', revision=None):
        if revision is None:
            revision = self.state()['revision']
        return self.client.post(self.api+'/save',data={'csrf':self.token(),'revision':str(revision),'original_text':original,'first_comment':comment})

    def test_first_result_and_explicit_rerecognition_preserve_manual_content(self):
        self.assertEqual(self.start().status_code,202)
        first = self.wait()
        self.assertEqual(first['original_text'],'识别出的中文原文\n第二行')
        self.assertEqual(first['first_ocr_text'],first['original_text'])
        self.assertEqual(self.save().status_code,200)
        self.fake.next = result('第二次识别的新结果','mock-second')
        self.assertEqual(self.start(first['run_id']).status_code,202)
        second = self.wait()
        self.assertEqual(second['original_text'],'人工校对')
        self.assertEqual(second['first_comment'],'我的一读')
        self.assertEqual(second['first_ocr_text'],first['first_ocr_text'])
        self.assertEqual(second['latest_ocr_text'],'第二次识别的新结果')
        self.assertEqual(self.db_count('ocr_runs'),2)
        self.assertEqual(self.db_count('notes'),0)
        self.assertEqual(self.db_count('imports'),0)
        with self.store.connection() as db:
            stored = json.loads(db.execute('SELECT response_json FROM ocr_runs ORDER BY rowid LIMIT 1').fetchone()[0])
        self.assertEqual(stored['RequestId'],'mock-request')

    def test_double_click_and_other_draft_are_not_charged_twice(self):
        self.fake.release = threading.Event()
        try:
            first = self.start()
            self.assertEqual(first.status_code,202)
            self.assertTrue(self.fake.started.wait(2))
            self.assertEqual(self.start().status_code,409)
            self.assertEqual(self.start(first.json()['run_id']).status_code,409)
            second = self.client.post('/api'+self.book+'/image-drafts',data={'csrf':self.token()},files={'file':('x.png',picture(size=(20,10)))})
            other_api = '/api'+self.book+'/image-drafts/'+second.json()['id']
            self.assertEqual(self.client.post(other_api+'/ocr',data={'csrf':self.token()}).status_code,409)
            self.assertEqual(self.client.post(self.book+'/image-drafts/'+self.draft_id+'/cancel',data={'csrf':self.token()}).status_code,409)
            for _ in range(3):
                self.assertEqual(self.state()['ocr_state'],'running')
            self.assertEqual(self.fake.calls,1)
        finally:
            self.fake.release.set()
        self.wait()

    def test_refresh_new_app_and_status_queries_never_replay_running_job(self):
        self.fake.release = threading.Event()
        try:
            self.start()
            self.assertTrue(self.fake.started.wait(2))
            with TestClient(create_app(self.settings,ocr_client=self.fake),follow_redirects=False) as restarted:
                restarted.cookies.update(self.client.cookies)
                self.assertEqual(restarted.get(self.api).json()['ocr_state'],'running')
                self.assertEqual(restarted.get(self.book+'/image-drafts/'+self.draft_id+'/edit').status_code,200)
                self.assertEqual(self.fake.calls,1)
        finally:
            self.fake.release.set()
        self.wait()

    def test_failure_polling_and_manual_retry(self):
        self.fake.next = OCRFailure('NetworkError')
        self.start()
        failed = self.wait('failed')
        self.assertIn('可能再次计费',failed['error'])
        for _ in range(3):
            self.state()
        self.assertEqual(self.fake.calls,1)
        self.fake.next = result('重试成功')
        self.assertEqual(self.start(failed['run_id']).status_code,202)
        succeeded = self.wait()
        self.assertEqual(succeeded['first_ocr_text'],'重试成功')
        self.assertEqual(self.fake.calls,2)
        self.assertEqual(self.start(failed['run_id']).status_code,409)
        self.assertEqual(self.fake.calls,2)

    def test_failed_rerecognition_keeps_last_success_and_manual_fields(self):
        self.start()
        first = self.wait()
        self.save()
        self.fake.next = OCRFailure('NoText')
        self.assertEqual(self.start(first['run_id']).status_code,202)
        state = self.wait('failed')
        self.assertEqual(state['first_ocr_text'],first['first_ocr_text'])
        self.assertEqual(state['latest_ocr_text'],first['latest_ocr_text'])
        self.assertEqual(state['original_text'],'人工校对')
        self.assertEqual(state['first_comment'],'我的一读')

    def test_manual_content_saved_while_ocr_runs_is_not_overwritten(self):
        self.fake.release = threading.Event()
        try:
            self.start()
            self.assertTrue(self.fake.started.wait(2))
            self.assertEqual(self.save('先行校对','等待时记录一读',0).status_code,200)
        finally:
            self.fake.release.set()
        state = self.wait()
        self.assertEqual(state['original_text'],'先行校对')
        self.assertEqual(state['first_comment'],'等待时记录一读')
        self.assertEqual(state['latest_ocr_text'],'识别出的中文原文\n第二行')

    def test_open_and_edit_survive_expiry_relogin_restart_and_reupload(self):
        self.assertEqual(self.client.post(self.api+'/open',data={'csrf':self.token()}).status_code,200)
        self.save('长期原文','长期一读')
        with self.store.connection() as db:
            db.execute('UPDATE image_drafts SET expires_at = 1 WHERE id = ?',(self.draft_id,))
        self.store.cleanup()
        self.client.post('/logout',data={'csrf':self.token()})
        self.login()
        with TestClient(create_app(self.settings,ocr_client=self.fake),follow_redirects=False) as c:
            c.cookies.update(self.client.cookies)
            self.assertEqual(c.get(self.api).json()['first_comment'],'长期一读')
            self.assertEqual(c.get(self.book+'/image-drafts/'+self.draft_id+'/file').status_code,200)
        uploaded = self.client.post('/api'+self.book+'/image-drafts',data={'csrf':self.token()},files={'file':('same.png',picture())})
        self.assertEqual(uploaded.json()['id'],self.draft_id)
        self.assertEqual(self.db_count('image_drafts'),1)

    def test_revision_conflict_and_lost_response_retry_are_safe(self):
        first = self.save('A','一读 A',0)
        self.assertEqual(first.status_code,200)
        self.assertEqual(self.save('A','一读 A',0).json()['revision'],first.json()['revision'])
        stale = self.save('B','另一页的一读',0)
        self.assertEqual(stale.status_code,409)
        self.assertEqual(self.state()['original_text'],'A')
        self.assertEqual(self.state()['first_comment'],'一读 A')

    def test_csrf_auth_ownership_and_validation(self):
        for suffix in ('/open','/save','/ocr'):
            self.assertEqual(self.client.post(self.api+suffix,data={'csrf':'wrong'}).status_code,403)
        other = self.create_book('其他书')
        wrong = self.api.replace(self.book,other)
        self.assertEqual(self.client.get(wrong).status_code,404)
        self.assertEqual(self.client.post(wrong+'/ocr',data={'csrf':self.token()}).status_code,404)
        self.assertEqual(self.client.post(self.api+'/save',data={'csrf':self.token()}).status_code,422)
        self.assertEqual(self.save('x'*50001).status_code,422)
        self.assertEqual(self.save('a\x00b').status_code,422)
        self.assertEqual(self.save(revision='NaN').status_code,422)
        with TestClient(self.app,follow_redirects=False) as anonymous:
            self.assertEqual(anonymous.get(self.api).status_code,401)
            self.assertEqual(anonymous.post(self.api+'/ocr').status_code,401)
            self.assertEqual(anonymous.get(self.book+'/image-drafts/'+self.draft_id+'/edit').status_code,303)
        self.assertEqual(self.fake.calls,0)

    def test_missing_configuration_does_not_create_or_call_a_job(self):
        with patch.object(self.fake,'prepare',side_effect=OCRFailure('NotConfigured')):
            response = self.start()
        self.assertEqual(response.status_code,503)
        self.assertEqual(self.db_count('ocr_runs'),0)
        self.assertEqual(self.fake.calls,0)

    def test_committed_provider_call_with_failed_result_storage_is_uncertain_not_replayed(self):
        service = self.app.state.drafts
        actual = service.finish
        attempts = []
        def fail_once(*args,**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError('synthetic disk failure')
            return actual(*args,**kwargs)
        with patch.object(service,'finish',side_effect=fail_once):
            self.start()
            state = self.wait('failed')
        self.assertIn('可能再次计费',state['error'])
        self.assertEqual(self.fake.calls,1)
        self.assertIsNone(state['first_ocr_text'])

    def test_expired_lease_and_late_result_never_replay_or_overwrite(self):
        service = self.app.state.drafts
        run_id = service.claim(self.book.split('/')[-1],self.draft_id,'')
        with self.store.connection() as db:
            db.execute('UPDATE ocr_runs SET started_at = 1 WHERE id = ?',(run_id,))
        state = self.state()
        self.assertEqual(state['ocr_state'],'failed')
        self.assertEqual(self.fake.calls,0)
        service.finish(run_id,result('过期工作进程的结果'))
        self.assertEqual(self.state()['original_text'],'')
        self.assertIsNone(self.state()['first_ocr_text'])

    def test_changed_source_image_is_not_sent(self):
        with self.store.connection() as db:
            path = db.execute('SELECT file_path FROM image_drafts WHERE id = ?',(self.draft_id,)).fetchone()[0]
        (self.store.root/path).write_bytes(b'tampered')
        self.start()
        self.assertIn('来源图片',self.wait('failed')['error'])
        self.assertEqual(self.fake.calls,0)

    def test_backup_restore_keeps_ocr_results_and_permanent_draft(self):
        self.start()
        self.wait()
        self.save('迁移原文','迁移一读')
        with self.store.connection() as db:
            db.execute('UPDATE image_drafts SET expires_at = 1')
        out = io.BytesIO()
        backup(self.store.root,out)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'new-machine'
            restore(root,io.BytesIO(out.getvalue()))
            fake = FakeOCR()
            with TestClient(create_app(Settings(root,cookie_secure=False),ocr_client=fake),follow_redirects=False) as c:
                self.assertEqual(c.get(self.api).status_code,401)
                self.login(c)
                state = c.get(self.api).json()
                self.assertEqual(state['original_text'],'迁移原文')
                self.assertEqual(state['first_comment'],'迁移一读')
                self.assertEqual(state['first_ocr_text'],'识别出的中文原文\n第二行')
                self.assertEqual(c.get(self.book+'/image-drafts/'+self.draft_id+'/file').content,picture())
                self.assertEqual(fake.calls,0)

    def test_restore_marks_in_progress_job_uncertain_without_calling_provider(self):
        self.app.state.drafts.claim(self.book.split('/')[-1],self.draft_id,'')
        out=io.BytesIO()
        backup(self.store.root,out)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            restore(root,io.BytesIO(out.getvalue()))
            with sqlite3.connect(root/'noteshub.sqlite3') as db:
                self.assertEqual(db.execute('SELECT state,error_code FROM ocr_runs').fetchone(),('failed','Interrupted'))
        self.assertEqual(self.fake.calls,0)


class MigrationTests(unittest.TestCase):
    def test_v2_image_drafts_migrate_without_losing_files_or_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            provision(root,'legacy-reader','legacy-test-password')
            filename='images/'+'a'*32+'.png'
            raw=picture()
            with sqlite3.connect(root/'noteshub.sqlite3') as db:
                db.executescript(SCHEMA+IMAGE_SCHEMA)
                db.execute("INSERT INTO books VALUES ('book','legacy','',1)")
                db.execute("INSERT INTO image_drafts VALUES ('draft','book',?,?,'image/png',?,80,40,1,9999999999)",(filename,hashlib.sha256(raw).hexdigest(),len(raw)))
            (root/'images').mkdir()
            (root/filename).write_bytes(raw)
            archive=io.BytesIO()
            backup(root,archive)
            new_root=root/'restored'
            restore(new_root,io.BytesIO(archive.getvalue()))
            migrated=Store(Settings(new_root))
            with migrated.connection() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
                row=db.execute('SELECT * FROM image_drafts').fetchone()
                self.assertEqual(row['file_path'],filename)
                self.assertEqual(row['editing_started'],0)
                self.assertEqual(row['revision'],0)
            self.assertEqual((new_root/filename).read_bytes(),raw)
