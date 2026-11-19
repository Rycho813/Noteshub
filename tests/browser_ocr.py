"""Real browser + local fake OCR. No external API or private fixture required."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from noteshub.auth import provision


def run():
    with tempfile.TemporaryDirectory(prefix='noteshub-ocr-browser-') as tmp:
        tmp=Path(tmp)
        password=secrets.token_urlsafe(24)
        provision(tmp/'data','ocr-reader',password)
        image=tmp/'synthetic.png'
        Image.new('RGB',(400,200),'white').save(image)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        with (tmp/'server.log').open('w+') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','tests.ocr_fixture_app:application','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env={**os.environ,'NOTESHUB_DATA_DIR':str(tmp/'data'),'NOTESHUB_COOKIE_SECURE':'false','NOTESHUB_OCR_ENABLED':'false'},stdout=log,stderr=log)
            try:
                with httpx.Client(trust_env=False) as c:
                    for _ in range(100):
                        try:
                            if c.get(base+'/login').status_code == 200: break
                        except httpx.ConnectError: pass
                        if server.poll() is not None: raise RuntimeError('Test server exited')
                        time.sleep(.1)
                    else: raise RuntimeError('Test server readiness timeout')
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    desktop=browser.new_context(viewport={'width':1280,'height':1000})
                    page=desktop.new_page()
                    errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('dialog',lambda dialog:dialog.accept())
                    def login(page):
                        page.goto(base+'/login')
                        page.get_by_label('用户名',exact=True).fill('ocr-reader')
                        page.get_by_label('密码',exact=True).fill(password)
                        page.get_by_role('button',name='登录',exact=True).click()
                        page.wait_for_url(base+'/')
                    def saved(page):
                        page.locator('#draft-save-status').get_by_text('已保存',exact=True).wait_for()
                    def calls():
                        return page.request.get(base+'/__test__/ocr-count').json()['mock_ocr_calls']
                    login(page)
                    page.get_by_role('textbox',name='书名 必填',exact=True).fill('OCR 浏览器验收书')
                    page.get_by_role('button',name='创建书籍').click()
                    page.get_by_role('link',name='图片摘录 · 上传与草稿').click()
                    page.locator('#image-file').set_input_files(image)
                    page.locator('#image-editor').wait_for(state='visible')
                    page.locator('#image-preview-button').click()
                    page.locator('#image-result').wait_for(state='visible')
                    page.locator('#image-upload').click()
                    page.wait_for_url('**/images#draft-*')
                    page.get_by_role('link',name='识别与一读',exact=True).click()
                    saved(page)
                    edit_url=page.url
                    api=page.locator('#draft-editor').get_attribute('data-api')
                    page.locator('#draft-recognize').click()
                    page.wait_for_function("() => document.getElementById('draft-ocr-status').textContent.startsWith('正在识别')")
                    assert page.locator('#draft-recognize').is_disabled()
                    page.reload()
                    page.wait_for_function("() => document.getElementById('draft-ocr-status').textContent.startsWith('识别失败')")
                    assert calls() == 1
                    for _ in range(2):
                        page.locator('#draft-refresh').click()
                    assert calls() == 1
                    page.locator('#draft-recognize').click()
                    page.wait_for_function("() => document.getElementById('draft-ocr-status').textContent.startsWith('识别成功')")
                    assert calls() == 2
                    assert '模拟识别的中文原文' in page.locator('#draft-original').input_value()
                    assert not page.evaluate('() => Boolean(window.ocrInjected)')
                    page.locator('#draft-original').fill('我校对过的原文')
                    page.locator('#draft-comment').fill('初次一读笔记')
                    saved(page)
                    assert page.request.get(base+api).json()['first_comment'] == '初次一读笔记'
                    # Lose a reply after the server committed, then keep typing.
                    def lose_reply(route):
                        response=route.fetch()
                        assert response.ok
                        route.abort()
                    page.route('**/image-drafts/*/save',lose_reply,times=1)
                    page.locator('#draft-comment').fill('回执丢失但服务端已保存')
                    page.wait_for_function("() => document.getElementById('draft-save-status').textContent.startsWith('保存失败')")
                    assert page.locator('#draft-comment').input_value() == '回执丢失但服务端已保存'
                    assert page.request.get(base+api).json()['first_comment'] == '回执丢失但服务端已保存'
                    page.locator('#draft-comment').fill('重试时的新一读内容')
                    page.locator('#draft-save').click()
                    saved(page)
                    assert page.request.get(base+api).json()['first_comment'] == '重试时的新一读内容'
                    # An ordinary failure also preserves input until explicit retry.
                    page.route('**/image-drafts/*/save',lambda route:route.fulfill(status=503,json={'detail':'测试保存失败'}),times=1)
                    page.locator('#draft-comment').fill('保存失败也不会丢失的一读')
                    page.wait_for_function("() => document.getElementById('draft-save-status').textContent.startsWith('保存失败')")
                    page.locator('#draft-save').click()
                    saved(page)
                    page.locator('#draft-recognize').click()
                    page.wait_for_function("() => document.getElementById('draft-ocr-status').textContent.startsWith('识别成功') && document.getElementById('draft-latest-text').textContent.includes('重新识别')")
                    assert calls() == 3
                    assert page.locator('#draft-original').input_value() == '我校对过的原文'
                    assert page.locator('#draft-comment').input_value() == '保存失败也不会丢失的一读'
                    assert '模拟识别的中文原文' in page.locator('#draft-first-text').text_content()
                    page.locator('#draft-latest').evaluate('(node) => { node.open=true; }')
                    page.locator('#draft-adopt').click()
                    saved(page)
                    assert '模拟重新识别的候选原文' in page.locator('#draft-original').input_value()
                    assert page.locator('#draft-comment').input_value() == '保存失败也不会丢失的一读'
                    page.reload()
                    saved(page)
                    assert calls() == 3
                    page.get_by_role('button',name='退出登录',exact=True).click()
                    page.wait_for_url(base+'/login')
                    login(page)
                    page.goto(edit_url)
                    saved(page)
                    assert page.locator('#draft-comment').input_value() == '保存失败也不会丢失的一读'
                    artifacts=ROOT/'test-results'
                    artifacts.mkdir(exist_ok=True)
                    page.screenshot(path=artifacts/'v2-ocr-desktop.png',full_page=True)
                    mobile=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                    phone=mobile.new_page()
                    phone.on('pageerror',lambda error:errors.append(str(error)))
                    phone.on('dialog',lambda dialog:dialog.accept())
                    login(phone)
                    phone.goto(edit_url)
                    saved(phone)
                    assert phone.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth')
                    phone.locator('#draft-comment').fill('手机上保存的一读')
                    saved(phone)
                    phone.screenshot(path=artifacts/'v2-ocr-mobile.png',full_page=True)
                    # Stale desktop page must keep its local input and reject overwrite.
                    page.locator('#draft-comment').fill('另一页面的未保存修改')
                    page.wait_for_function("() => document.getElementById('draft-save-status').textContent.includes('另一页面')")
                    assert page.locator('#draft-comment').input_value() == '另一页面的未保存修改'
                    assert page.request.get(base+api).json()['first_comment'] == '手机上保存的一读'
                    assert calls() == 3
                    assert not errors,errors
                    browser.close()
                print('PASS (mock OCR only): upload/crop -> OCR failure/manual retry, running-state reload without replay, original/first-comment autosave, lost-reply recovery, explicit adoption, first-result preservation, logout/relogin, mobile layout and cross-page conflict protection. Three simulated provider calls, zero Tencent calls.')
            except BaseException:
                log.flush(); log.seek(0); print(log.read()[-5000:])
                raise
            finally:
                server.terminate()
                server.wait(timeout=15)


if __name__ == '__main__':
    run()
