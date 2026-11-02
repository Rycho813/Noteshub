"""Archive browser integration with a genuine v3 fixture; OCR remains disabled."""
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PASSWORD='only-for-test-password'


def run():
    with tempfile.TemporaryDirectory(prefix='noteshub-archive-browser-') as tmp:
        tmp=Path(tmp)
        data=tmp/'data'
        subprocess.run([str(ROOT/'.venv/bin/python'),'-c','from pathlib import Path; import sys; from tests.test_archive import legacy_v3; legacy_v3(Path(sys.argv[1]))',str(data)],cwd=ROOT,check=True,capture_output=True)
        image=tmp/'synthetic.png'
        Image.new('RGB',(240,80),(220,220,255)).save(image)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        with (tmp/'server.log').open('w+') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','noteshub.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env={**os.environ,'NOTESHUB_DATA_DIR':str(data),'NOTESHUB_COOKIE_SECURE':'false','NOTESHUB_OCR_ENABLED':'false'},stdout=log,stderr=log)
            try:
                with httpx.Client(trust_env=False) as c:
                    for _ in range(100):
                        try:
                            if c.get(base+'/login').status_code==200: break
                        except httpx.ConnectError: pass
                        if server.poll() is not None: raise RuntimeError('Test server exited')
                        time.sleep(.1)
                    else: raise RuntimeError('Readiness timeout')
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    desktop=browser.new_context(viewport={'width':1280,'height':1000})
                    page=desktop.new_page()
                    errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('dialog',lambda dialog:dialog.accept())
                    def login(page):
                        page.goto(base+'/login')
                        page.get_by_label('用户名',exact=True).fill('reader')
                        page.get_by_label('密码',exact=True).fill(PASSWORD)
                        page.get_by_role('button',name='登录',exact=True).click()
                        page.wait_for_url(base+'/')
                    def ready(page):
                        page.wait_for_function("() => !document.getElementById('draft-original').disabled")
                    def archived(page):
                        page.wait_for_function("() => document.getElementById('draft-archive-status').textContent.startsWith('已归档')")
                    login(page)
                    page.goto(base+'/books/book/image-drafts/draft/edit')
                    ready(page)
                    assert len(page.locator('#draft-original').input_value())==137
                    assert len(page.locator('#draft-comment').input_value())==17
                    assert page.locator('#draft-recognize').is_disabled()
                    events=[]
                    page.on('request',lambda request:events.append(request.url.rsplit('/',1)[-1]) if request.method=='POST' else None)
                    def lost_archive_reply(route):
                        response=route.fetch()
                        assert response.ok
                        route.abort()
                    page.route('**/image-drafts/*/archive',lost_archive_reply,times=1)
                    page.locator('#draft-original').fill('浏览器即时校对后的原文')
                    page.locator('#draft-comment').fill('点击归档前刚输入的一读')
                    page.locator('#draft-archive').click()
                    archived(page)
                    assert events.index('save') < events.index('archive')
                    assert page.locator('#draft-original').get_attribute('readonly') is not None
                    assert page.locator('#draft-archive').is_disabled()
                    state=page.request.get(base+'/api/books/book/image-drafts/draft').json()
                    assert state['archived'] and state['original_text']=='浏览器即时校对后的原文'
                    assert state['first_comment']=='点击归档前刚输入的一读'
                    assert state['first_ocr_text']=='识'*134
                    assert state['note_status']=='pending'
                    note_url=state['note_url']
                    page.reload()
                    ready(page); archived(page)
                    page.locator('#draft-note-link').click()
                    page.wait_for_url(base+note_url)
                    page.wait_for_function("() => !document.getElementById('export-book').disabled")
                    assert '浏览器即时校对后的原文' in page.locator('.reading-text').inner_text()
                    assert page.get_by_role('link',name='查看来源图片与识别记录').is_visible()
                    with page.expect_download() as download:
                        page.locator('#export-book').click()
                    exported=Path(download.value.path()).read_text()
                    assert '浏览器即时校对后的原文' in exported and '旧版二读' in exported and '点击归档前刚输入的一读' in exported
                    page.goto(base+'/books/book/images')
                    assert page.locator('.image-draft').count()==0
                    assert page.locator('.image-archive').count()==1
                    assert page.get_by_role('button',name='取消并删除这张草稿').count()==0
                    # Image-only book: empty-original validation, empty first comment allowed.
                    page.goto(base+'/')
                    page.get_by_role('textbox',name='书名 必填',exact=True).fill('只有图片的新书')
                    page.get_by_role('button',name='创建书籍').click()
                    new_book=page.url
                    page.get_by_role('link',name='图片摘录 · 上传与草稿').click()
                    page.locator('#image-file').set_input_files(image)
                    page.locator('#image-editor').wait_for(state='visible')
                    page.locator('#image-preview-button').click()
                    page.locator('#image-result').wait_for(state='visible')
                    page.locator('#image-upload').click()
                    page.get_by_role('link',name='识别与一读',exact=True).click()
                    ready(page)
                    page.locator('#draft-archive').click()
                    assert '原文不能为空' in page.locator('#draft-archive-status').inner_text()
                    page.locator('#draft-original').fill('无需 OCR 的手动原文')
                    page.locator('#draft-archive').click()
                    archived(page)
                    page.locator('#draft-note-link').click()
                    page.wait_for_function("() => !document.getElementById('export-book').disabled")
                    assert '无需 OCR 的手动原文' in page.locator('.reading-text').inner_text()
                    assert page.get_by_role('heading',name='导入 Zotero 笔记').is_visible()
                    # Failed draft save blocks archive until retry succeeds.
                    csrf=page.locator('input[name="csrf"]').first.get_attribute('value')
                    uploaded=page.request.post(new_book.replace(base,base+'/api')+'/image-drafts',multipart={'csrf':csrf,'file':{'name':'other.png','mimeType':'image/png','buffer':image.read_bytes()+b'\n'}})
                    assert uploaded.status==201
                    draft_id=uploaded.json()['id']
                    editor_url=new_book+'/image-drafts/'+draft_id+'/edit'
                    page.goto(editor_url); ready(page)
                    api=page.locator('#draft-editor').get_attribute('data-api')
                    page.route('**/image-drafts/*/save',lambda route:route.fulfill(status=503,json={'detail':'synthetic save failure'}),times=1)
                    page.locator('#draft-original').fill('失败重试的原文')
                    page.locator('#draft-comment').fill('保留输入再归档')
                    page.locator('#draft-archive').click()
                    page.wait_for_function("() => document.getElementById('draft-save-status').textContent.startsWith('保存失败')")
                    assert not page.request.get(base+api).json()['archived']
                    assert page.locator('#draft-comment').input_value()=='保留输入再归档'
                    page.locator('#draft-archive').click()
                    archived(page)
                    final=page.request.get(base+api).json()
                    assert final['archived'] and final['first_comment']=='保留输入再归档'
                    page.get_by_role('button',name='退出登录',exact=True).click()
                    page.wait_for_url(base+'/login')
                    login(page)
                    page.goto(editor_url); ready(page); archived(page)
                    artifacts=ROOT/'test-results'; artifacts.mkdir(exist_ok=True)
                    page.screenshot(path=artifacts/'v2-archive-desktop.png',full_page=True)
                    mobile=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                    phone=mobile.new_page()
                    phone.on('pageerror',lambda error:errors.append(str(error)))
                    login(phone); phone.goto(editor_url); ready(phone); archived(phone)
                    assert phone.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
                    assert phone.locator('#draft-comment').input_value()=='保留输入再归档'
                    phone.screenshot(path=artifacts/'v2-archive-mobile.png',full_page=True)
                    assert not errors,errors
                    browser.close()
                with __import__('sqlite3').connect(data/'noteshub.sqlite3') as db:
                    assert db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]==4
                    assert db.execute('SELECT COUNT(*) FROM ocr_runs').fetchone()[0]==1
                    assert db.execute("SELECT COUNT(*) FROM notes WHERE source_type='image'").fetchone()[0]==3
                print('PASS: genuine v3 upgrade, save-before-archive, lost archive reply, read-only history, mixed-book export, image-only book, empty-comment archive, save-failure retry, relogin and mobile layout. Zero OCR calls; existing synthetic OCR history unchanged.')
            finally:
                server.terminate(); server.wait(timeout=15)


if __name__=='__main__': run()
