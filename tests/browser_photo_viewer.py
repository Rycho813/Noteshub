"""Photo viewer and Markdown regression using synthetic data on loopback 18081.

Run with .venv/bin/python tests/browser_photo_viewer.py [--browser webkit].
"""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BOOK_SELECT_LINE = next(index for index,line in enumerate((ROOT/'noteshub/templates/quick.html').read_text().splitlines()) if '<select id="quick-book"' in line)


def ready(page):
    page.wait_for_function("() => !document.getElementById('quick-thought').disabled")
    page.wait_for_function("() => document.querySelectorAll('.quick-photo-open').length === NotesHubQuick.getCurrent().photos.length")


def login(page, base):
    page.goto(base+'/login')
    page.get_by_label('用户名', exact=True).fill('reader')
    page.get_by_label('密码', exact=True).fill('only-for-test-password')
    page.get_by_role('button', name='登录', exact=True).click()
    page.wait_for_url(base+'/')


def open_photo(page, index, mobile):
    thumb = page.locator('.quick-photo-open').nth(index)
    thumb.tap() if mobile else thumb.click()
    expect(page.get_by_role('dialog', name='图片查看器')).to_be_visible()
    page.wait_for_function("() => { const img=document.getElementById('quick-viewer-image'); return img.complete && img.naturalWidth > 0 && img.width > 0; }")
    assert page.locator('#quick-viewer-image').get_attribute('src').startswith('blob:')


def exercise_viewer(page, mobile, artifacts, label):
    page._viewer_phase=label+' open'
    before = page.evaluate('() => NotesHubQuick.getCurrent()')
    address = page.url
    open_photo(page, 0, mobile)
    expect(page.locator('#quick-viewer-status')).to_have_text('图片 1 / 2 · 100%')
    expect(page.locator('#quick-viewer-out')).to_be_disabled()
    assert page.evaluate("() => {const i=document.getElementById('quick-viewer-image'),s=document.getElementById('quick-viewer-stage');return i.width<=s.clientWidth && i.height<=s.clientHeight;}")
    # Native dialogs keep background controls inert. Tabbing into browser chrome
    # is allowed and reports body as the active element in headless Chromium.
    for _ in range(6):
        page.keyboard.press('Tab')
        assert page.evaluate("() => document.getElementById('quick-viewer').matches(':modal') && (document.activeElement === document.body || document.getElementById('quick-viewer').contains(document.activeElement))")
    page._viewer_phase=label+' zoom'
    for _ in range(4):
        page.locator('#quick-viewer-in').click()
    expect(page.locator('#quick-viewer-in')).to_be_disabled()
    expect(page.locator('#quick-viewer-status')).to_have_text('图片 1 / 2 · 400%')
    stage = page.locator('#quick-viewer-stage')
    stage.hover()
    if not mobile:
        page.mouse.wheel(0,250)
        page.wait_for_function("() => document.getElementById('quick-viewer-stage').scrollTop > 0")
    elif label.startswith('chromium'):
        # Use real browser input for the swipe, rather than dispatching a JS
        # event that cannot trigger the browser's default touch scrolling.
        session = page.context.new_cdp_session(page)
        box = stage.bounding_box()
        x, y = box['x']+box['width']/2, box['y']+box['height']*.75
        session.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
        for distance in (30,70,110,150):
            session.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y-distance}]})
        session.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
        session.detach()
        page.wait_for_function("() => document.getElementById('quick-viewer-stage').scrollTop > 0")
    page.locator('#quick-viewer-stage').evaluate('(stage) => stage.scrollTo(150,300)')
    assert page.locator('#quick-viewer-stage').evaluate('(stage) => stage.scrollTop > 0 && stage.scrollLeft > 0')
    # WebKit's screenshot API injects an inline `body {}` stylesheet even with
    # caret='initial', conflicting with the app's CSP. Keep CSP fully enforced:
    # capture Chromium, and run the same interaction/layout assertions in WebKit.
    if label.startswith('chromium'):
        page.screenshot(path=artifacts/f'{label}-zoom.png',caret='initial')
    page.locator('#quick-viewer-out').click()
    expect(page.locator('#quick-viewer-status')).to_have_text('图片 1 / 2 · 267%')
    page.locator('#quick-viewer-fit').click()
    expect(page.locator('#quick-viewer-status')).to_have_text('图片 1 / 2 · 100%')
    if label.startswith('chromium'):
        page.screenshot(path=artifacts/f'{label}-fit.png',caret='initial')
    # A refresh of the thumbnails must not revoke the open viewer's URL.
    page._viewer_phase=label+' repaint'
    page.evaluate('() => NotesHubQuick.open()')
    page.wait_for_function("() => document.querySelectorAll('.quick-photo-open').length === 2")
    assert page.locator('#quick-viewer-image').evaluate('(image) => image.complete && image.naturalWidth > 0')
    assert page.evaluate("async () => {const copy=new Image();copy.src=document.getElementById('quick-viewer-image').src;await copy.decode();return copy.naturalWidth > 0;}")
    page.locator('#quick-viewer-close').click()
    expect(page.locator('#quick-viewer')).not_to_be_visible()
    expect(page.locator('.quick-photo-open').first).to_be_focused()
    assert page.locator('#quick-viewer-image').get_attribute('src') is None
    # Old queued native close events must not clear an immediately reopened
    # image. Keep all three actions in the same browser task to expose the race.
    page._viewer_phase=label+' rapid reopen'
    page.evaluate("() => {const buttons=document.querySelectorAll('.quick-photo-open');buttons[0].click();document.getElementById('quick-viewer-close').click();buttons[1].click();}")
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    expect(page.locator('#quick-viewer')).to_be_visible()
    page.wait_for_function("() => document.getElementById('quick-viewer-image').naturalWidth > 0")
    assert page.evaluate("() => document.documentElement.classList.contains('quick-viewer-open')")
    page.locator('#quick-viewer-close').click()
    # The landscape image is independent of the previous portrait image.
    open_photo(page, 1, mobile)
    expect(page.locator('#quick-viewer-status')).to_have_text('图片 2 / 2 · 100%')
    assert page.locator('#quick-viewer-image').evaluate('(image) => image.naturalWidth > image.naturalHeight')
    if mobile:
        page._viewer_phase=label+' rotation'
        page.set_viewport_size({'width':844, 'height':390})
        page.wait_for_timeout(100)
        expect(page.locator('#quick-viewer-close')).to_be_in_viewport()
        page.locator('#quick-viewer-close').tap()
        page.set_viewport_size({'width':390, 'height':844})
    else:
        page.keyboard.press('Escape')
    expect(page.locator('#quick-viewer')).not_to_be_visible()
    assert page.url == address
    after = page.evaluate('() => NotesHubQuick.getCurrent()')
    for name in ('id','book_id','first_comment','original_text','second_comment','photos','localVersion','revision','dirty'):
        assert after[name] == before[name], name
    assert not page.evaluate("() => document.documentElement.classList.contains('quick-viewer-open')")
    assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')


def markdown_roundtrip(page, base):
    page.goto(base+'/')
    page.get_by_role('textbox', name='书名 必填', exact=True).fill('合成 Markdown 回归')
    page.get_by_role('button', name='创建书籍', exact=True).click()
    book_url = page.url
    page.get_by_label('Markdown 文件').set_input_files({
        'name':'synthetic.md', 'mimeType':'text/markdown',
        'buffer':'“合成原文”合成一读\n\n“第二条原文”第二条一读'.encode(),
    })
    page.get_by_role('button', name='上传并预览', exact=True).click()
    page.get_by_role('button', name='确认导入 2 条笔记', exact=True).click()
    page.wait_for_url(book_url)
    page.wait_for_function("() => !document.getElementById('second-comment').disabled")
    page.locator('#second-comment').fill('保留的二读\n新一行')
    expect(page.locator('#save-status')).to_have_text('已保存')
    page.reload()
    expect(page.locator('#second-comment')).to_have_value('保留的二读\n新一行')
    with page.expect_download() as download:
        page.locator('#export-book').click()
    exported = Path(download.value.path()).read_text()
    for text in ('合成原文', '合成一读', '保留的二读', '新一行', '第二条原文', '第二条一读'):
        assert text in exported


def cached_reader(create_context, base, note_id, artifacts, label):
    """A different device downloads server photos, then views them offline."""
    context = create_context(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
    page = context.new_page()
    login(page,base)
    page.goto(base+'/quick?note='+note_id)
    ready(page)
    page.wait_for_function("() => document.querySelectorAll('.quick-photo-open:not(:disabled)').length === 2")
    context.set_offline(True)
    exercise_viewer(page,True,artifacts,label+'-downloaded-offline')
    context.close()


def run(browser_name):
    artifacts = ROOT/'test-results'
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='noteshub-photo-viewer-') as folder:
        tmp = Path(folder)
        data = tmp/'data'
        subprocess.run([str(ROOT/'.venv/bin/python'), '-c',
                        'from pathlib import Path;import sys;from tests.test_archive import legacy_v3;legacy_v3(Path(sys.argv[1]))', str(data)], cwd=ROOT, check=True, capture_output=True)
        old_files = {p.relative_to(data):hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in data.rglob('*') if p.is_file() and p.suffix != '.sqlite3'}
        images = []
        for index, size in enumerate(((1200,2000), (2000,1000))):
            image = Image.new('RGB', size, '#fffefb')
            drawing = ImageDraw.Draw(image)
            for y in range(60, size[1], 90):
                drawing.text((60,y), f'NotesHub synthetic page {index+1}, line {y//90+1}', fill='#23342f', font_size=30)
                drawing.line((60,y+45,size[0]-60,y+45), fill='#b5c6bc', width=3)
            path = tmp/f'photo-{index}.png'
            image.save(path)
            images.append(path)
        base = 'http://127.0.0.1:'+os.environ.get('NOTESHUB_BROWSER_PORT','18081')
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            listener.bind(('127.0.0.1',int(base.rsplit(':',1)[1])))
        with (tmp/'server.log').open('w+') as log:
            server = subprocess.Popen([str(ROOT/'.venv/bin/python'), '-m', 'uvicorn', 'noteshub.web:create_app', '--factory', '--host', '127.0.0.1', '--port', base.rsplit(':',1)[1]], cwd=ROOT,
                                      env={**os.environ, 'NOTESHUB_DATA_DIR':str(data), 'NOTESHUB_COOKIE_SECURE':'false', 'NOTESHUB_OCR_ENABLED':'false'}, stdout=log, stderr=log)
            try:
                with httpx.Client(trust_env=False) as client:
                    for _ in range(100):
                        if server.poll() is not None:
                            raise RuntimeError('Temporary test server exited')
                        try:
                            if client.get(base+'/login').status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        time.sleep(.1)
                    else:
                        raise RuntimeError('Temporary test server did not start')
                with sync_playwright() as playwright:
                    options = {'headless':True}
                    if browser_name == 'chromium':
                        options.update(executable_path=shutil.which('chromium'), args=['--no-sandbox'])
                    engine = getattr(playwright,browser_name)
                    browser = engine.launch(**options) if browser_name == 'chromium' else None
                    profile_index = 0
                    errors, native_select_warnings = [], []
                    def watch(page):
                        page.on('pageerror',lambda error: errors.append(str(error)))
                        def console(message):
                            if 'Content Security Policy' not in message.text:
                                return
                            # WebKit's native select inserts a UA stylesheet at
                            # parse time. This baseline warning reproduces with
                            # all app scripts removed and vanishes without the
                            # select. Report only that exact source separately;
                            # viewer and all other app CSP errors still fail.
                            location = message.location
                            if browser_name == 'webkit' and message.text.startswith('Refused to apply a stylesheet') and urlsplit(location.get('url','')).path == '/quick' and location.get('lineNumber',location.get('line')) == BOOK_SELECT_LINE:
                                native_select_warnings.append(message.text)
                                return
                            errors.append((getattr(page,'_viewer_phase','navigation'),message.text,message.location))
                        page.on('console',console)
                    def create_context(**context_options):
                        nonlocal profile_index
                        if browser:
                            context = browser.new_context(**context_options)
                        else:
                            # WebKit's private contexts cannot store photo Blobs
                            # in IndexedDB. Use isolated ordinary PWA profiles.
                            profile_index += 1
                            context = engine.launch_persistent_context(str(tmp/f'webkit-profile-{profile_index}'), **options, **context_options)
                        context.on('page',watch)
                        return context
                    for mobile in (False, True):
                        context = create_context(viewport={'width':390,'height':844} if mobile else {'width':1280,'height':1000}, is_mobile=mobile, has_touch=mobile)
                        page = context.new_page()
                        page._viewer_phase='login'
                        login(page,base)
                        page._viewer_phase='quick load'
                        page.goto(base+'/quick?book=book')
                        ready(page)
                        page.wait_for_function('() => Boolean(navigator.serviceWorker.controller)')
                        context.set_offline(True)
                        page.locator('#quick-thought').fill('离线记录的一读想法')
                        page.get_by_text('补充原文与二读',exact=True).click()
                        page.locator('#quick-original').fill('查看大图前的原文')
                        page.locator('#quick-second').fill('查看大图前的二读')
                        page._viewer_phase='upload'
                        page.locator('#quick-files').set_input_files(images)
                        try:
                            expect(page.locator('#quick-photo-status')).to_have_text('图片已保存在本机',timeout=30000)
                        except AssertionError as error:
                            raise AssertionError(page.locator('#quick-error').inner_text()) from error
                        # Open immediately after typing; an outstanding local
                        # save must keep the latest edit when the viewer closes.
                        page.locator('#quick-thought').fill('查看大图前的一读想法')
                        label = browser_name+('-mobile' if mobile else '-desktop')
                        exercise_viewer(page,mobile,artifacts,label)
                        note_id = page.evaluate('() => NotesHubQuick.getCurrent().id')
                        # Close the actual page and open a new offline PWA shell.
                        page.close()
                        page = context.new_page()
                        page.goto(base+'/quick')
                        ready(page)
                        expect(page.locator('#quick-thought')).to_have_value('查看大图前的一读想法')
                        exercise_viewer(page,mobile,artifacts,label+'-reopened-offline')
                        page.get_by_text('补充原文与二读',exact=True).click()
                        expect(page.locator('#quick-original')).to_have_value('查看大图前的原文')
                        expect(page.locator('#quick-second')).to_have_value('查看大图前的二读')
                        context.set_offline(False)
                        page.locator('#quick-sync').click()
                        expect(page.locator('#quick-status')).to_have_text('已同步',timeout=30000)
                        remote = page.request.get(base+'/api/paper/notes/'+note_id).json()
                        assert remote['first_comment'] == '查看大图前的一读想法'
                        assert remote['original_text'] == '查看大图前的原文'
                        assert remote['second_comment'] == '查看大图前的二读'
                        assert len(remote['photos']) == 2
                        if not mobile:
                            cached_reader(create_context,base,note_id,artifacts,label)
                            page._viewer_phase='markdown'
                            markdown_roundtrip(page,base)
                        context.close()
                    assert not errors, errors
                    if native_select_warnings:
                        print(f'Baseline WebKit native-select CSP warnings: {len(native_select_warnings)}; app CSP unchanged, no viewer CSP errors.')
                    if browser:
                        browser.close()
                for path, digest in old_files.items():
                    assert hashlib.sha256((data/path).read_bytes()).hexdigest() == digest
                with sqlite3.connect(data/'noteshub.sqlite3') as db:
                    assert db.execute('SELECT COUNT(*) FROM ocr_runs').fetchone()[0] == 1
                    assert db.execute("SELECT original_text,first_comment,second_comment FROM notes WHERE id='old-note'").fetchone() == ('旧版原文','旧版一读','旧版二读')
                print(f'PASS {browser_name}: desktop/mobile photo fit/zoom/scroll/close, focus, orientation, offline page reopen, all edits retained and synced, synthetic Markdown import/edit/export, legacy files/history unchanged. Zero paid OCR calls.')
            finally:
                server.terminate()
                server.wait(timeout=15)
                if server.returncode not in (0,-15):
                    log.seek(0)
                    print(log.read()[-3000:])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',choices=('chromium','webkit'),default='chromium')
    run(parser.parse_args().browser)
