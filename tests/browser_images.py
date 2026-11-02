"""Isolated V2 browser checks; no private fixture, OCR or production access.

Run with a Python providing Playwright and Pillow; requires system Chromium and
project .venv dependencies. Uses synthetic images, random account, local server.
"""
import io
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from noteshub.auth import provision


def run():
    with tempfile.TemporaryDirectory(prefix='noteshub-image-browser-') as tmp:
        tmp = Path(tmp)
        data = tmp/'data'
        password = secrets.token_urlsafe(24)
        provision(data,'image-reader',password)
        png = tmp/'screenshot.png'
        jpg = tmp/'camera.jpg'
        source = Image.new('RGB',(400,300),'red')
        drawing = ImageDraw.Draw(source)
        drawing.rectangle((200,0,399,149),fill=(0,180,0))
        drawing.rectangle((0,150,199,299),fill='blue')
        drawing.rectangle((200,150,399,299),fill='black')
        source.save(png)
        exif = Image.Exif()
        exif[274] = 6
        source.save(jpg,quality=96,exif=exif)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port = s.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        log = (tmp/'server.log').open('w+')
        server = subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','noteshub.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env={**os.environ,'NOTESHUB_DATA_DIR':str(data),'NOTESHUB_COOKIE_SECURE':'false'},stdout=log,stderr=log)
        try:
            with httpx.Client(trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get(base+'/login').status_code == 200: break
                    except httpx.ConnectError: pass
                    if server.poll() is not None: raise RuntimeError('server exited')
                    time.sleep(.1)
                else: raise RuntimeError('server timeout')
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                errors=[]
                desktop=browser.new_context(viewport={'width':1280,'height':1000},permissions=['clipboard-read','clipboard-write'])
                page=desktop.new_page()
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('console',lambda msg:errors.append(msg.text) if 'Content Security Policy' in msg.text else None)
                page.on('dialog',lambda dialog:dialog.accept())
                def login(page):
                    page.goto(base+'/login')
                    page.get_by_label('用户名',exact=True).fill('image-reader')
                    page.get_by_label('密码',exact=True).fill(password)
                    page.get_by_role('button',name='登录',exact=True).click()
                    page.wait_for_url(base+'/')
                login(page)
                page.get_by_role('textbox',name='书名 必填',exact=True).fill('V2 browser book')
                page.get_by_role('button',name='创建书籍').click()
                book_url=page.url
                page.get_by_role('link',name='图片摘录 · 上传与草稿').click()
                workspace=page.url
                page.locator('#image-file').set_input_files(png)
                page.locator('#image-editor').wait_for(state='visible')
                # Free selection, movement and resizing via real mouse events.
                canvas=page.locator('#crop-canvas')
                canvas.scroll_into_view_if_needed()
                box=canvas.bounding_box()
                def drag(x1,y1,x2,y2):
                    page.mouse.move(box['x']+x1,box['y']+y1)
                    page.mouse.down()
                    page.mouse.move(box['x']+x2,box['y']+y2,steps=8)
                    page.mouse.up()
                drag(40,30,160,100)
                assert page.locator('#crop-w').input_value() == '120'
                drag(100,60,110,70)
                assert page.locator('#crop-x').input_value() == '50'
                drag(170,110,210,140)
                assert page.locator('#crop-w').input_value() == '160'
                # Zoom affects display only.
                page.locator('#image-zoom').fill('2')
                assert page.locator('#crop-canvas').bounding_box()['width'] == box['width'] * 2
                page.locator('#image-pan').click()
                assert 'crop-pan' in page.locator('#crop-canvas').get_attribute('class')
                page.locator('#image-pan').click()
                assert page.locator('#crop-w').input_value() == '160'
                page.locator('#image-zoom').fill('1')
                for field,value in [('x',200),('y',0),('w',200),('h',150)]:
                    page.locator('#crop-'+field).fill(str(value))
                    page.locator('#crop-'+field).dispatch_event('change')
                page.locator('#image-preview-button').click()
                page.locator('#image-result').wait_for(state='visible')
                # Nothing leaves the browser until save is clicked.
                with sqlite3.connect(data/'noteshub.sqlite3') as db:
                    assert db.execute('SELECT COUNT(*) FROM image_drafts').fetchone()[0] == 0
                # Failed upload preserves preview and can retry.
                page.route('**/api/books/*/image-drafts',lambda route:route.abort())
                page.locator('#image-upload').click()
                page.wait_for_function("() => !document.getElementById('image-upload').disabled")
                assert page.locator('#image-result').is_visible()
                page.unroute('**/api/books/*/image-drafts')
                page.locator('#image-upload').click()
                page.wait_for_url('**/images#draft-*')
                page.locator('.image-draft').wait_for(state='visible')
                with sqlite3.connect(data/'noteshub.sqlite3') as db:
                    row=db.execute('SELECT file_path,width,height FROM image_drafts').fetchone()
                assert row[1:] == (200,150),row
                with Image.open(data/row[0]) as saved:
                    assert saved.format == 'PNG'
                    assert saved.getpixel((100,75))[:3] == (0,180,0), saved.getpixel((100,75))
                page.reload()
                assert page.locator('.image-draft').count() == 1
                artifacts=ROOT/'test-results'
                artifacts.mkdir(exist_ok=True)
                page.screenshot(path=artifacts/'v2-desktop-draft.png',full_page=True)
                page.get_by_role('button',name='取消并删除这张草稿').click()
                page.wait_for_url(workspace)
                assert page.locator('.image-draft').count() == 0
                assert not list((data/'images').iterdir())
                # Real browser clipboard image paste, then reset local image.
                png_url=__import__('base64').b64encode(png.read_bytes()).decode()
                page.evaluate("""async (b64) => { const bytes=Uint8Array.from(atob(b64),c=>c.charCodeAt(0)); const blob=new Blob([bytes],{type:'image/png'}); await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]); }""",png_url)
                page.locator('#image-drop').focus()
                page.keyboard.press('Control+v')
                page.locator('#image-editor').wait_for(state='visible')
                assert page.locator('#crop-w').input_value() == '400'
                # A paste targeted at a textarea must not be intercepted.
                assert page.evaluate("""() => { const t=document.createElement('textarea'); document.getElementById('image-input').append(t); const dt=new DataTransfer(); dt.items.add(new File(['x'],'x.png',{type:'image/png'})); const e=new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}); t.dispatchEvent(e); t.remove(); return !e.defaultPrevented; }""")
                page.locator('#image-discard').click()
                # Drag/drop uses the same preparation path.
                encoded=__import__('base64').b64encode(png.read_bytes()).decode()
                page.locator('#image-drop').evaluate("""(node,b64) => { const bytes=Uint8Array.from(atob(b64),c=>c.charCodeAt(0)); const dt=new DataTransfer(); dt.items.add(new File([bytes],'drop.png',{type:'image/png'})); node.dispatchEvent(new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true})); }""",encoded)
                page.locator('#image-editor').wait_for(state='visible')
                assert page.locator('#crop-w').input_value() == '400'
                page.locator('#image-discard').click()
                # Mobile viewport + touch: JPEG EXIF orientation, rotate, no crop.
                mobile=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,device_scale_factor=2)
                phone=mobile.new_page()
                phone.on('pageerror',lambda error:errors.append(str(error)))
                phone.on('dialog',lambda dialog:dialog.accept())
                login(phone)
                phone.goto(workspace)
                assert phone.locator('#image-camera').get_attribute('capture') == 'environment'
                phone.locator('#image-camera').set_input_files(jpg)
                phone.locator('#image-editor').wait_for(state='visible')
                assert phone.locator('#crop-w').input_value() == '300'
                assert phone.locator('#crop-h').input_value() == '400'
                cdp=mobile.new_cdp_session(phone)
                def tap(selector):
                    node=phone.locator(selector)
                    node.scroll_into_view_if_needed()
                    bounds=node.bounding_box()
                    cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':bounds['x']+bounds['width']/2,'y':bounds['y']+bounds['height']/2}]})
                    cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
                tap('#image-rotate')
                phone.wait_for_function("() => document.getElementById('crop-w').value === '400'")
                assert phone.locator('#crop-w').input_value() == '400'
                assert phone.locator('#crop-h').input_value() == '300'
                phone.locator('#crop-canvas').scroll_into_view_if_needed()
                box=phone.locator('#crop-canvas').bounding_box()
                cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':box['x']+box['width']*.15,'y':box['y']+box['height']*.15}]})
                phone.wait_for_timeout(100)
                cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':box['x']+box['width']*.75,'y':box['y']+box['height']*.7}]})
                phone.wait_for_timeout(100)
                cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
                # Give the synthetic gesture realistic duration and settle time.
                # Zero-duration CDP swipes suppress the next tap even on an
                # independent blank page; no application behavior is bypassed.
                phone.wait_for_timeout(500)
                assert 1 < int(phone.locator('#crop-w').input_value()) < 400
                tap('#image-whole')
                phone.wait_for_function("() => document.getElementById('crop-w').value === '400' && document.getElementById('crop-h').value === '300'")
                tap('#image-preview-button')
                phone.locator('#image-result').wait_for(state='visible')
                phone.screenshot(path=artifacts/'v2-mobile-preview.png',full_page=True)
                tap('#image-upload')
                phone.wait_for_url('**/images#draft-*')
                phone.locator('.image-draft').wait_for(state='visible')
                assert phone.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                with sqlite3.connect(data/'noteshub.sqlite3') as db:
                    row=db.execute('SELECT file_path,width,height FROM image_drafts').fetchone()
                    assert db.execute('SELECT COUNT(*) FROM notes').fetchone()[0] == 0
                assert row[1:] == (400,300), row
                with Image.open(data/row[0]) as saved:
                    assert saved.format == 'JPEG'
                    assert max(saved.getpixel((20,20))) < 10 # EXIF 90° + user 90° = 180°.
                    assert not saved.getexif()
                # Existing Markdown -> review flow and normal text clipboard paste.
                page.goto(workspace)
                csrf=page.locator('#image-input').get_attribute('data-csrf')
                uploaded=page.request.post(book_url+'/imports',multipart={'csrf':csrf,'file':{'name':'notes.md','mimeType':'text/markdown','buffer':'“旧流程原文”一读评论'.encode()}},max_redirects=0)
                assert uploaded.status == 303
                assert page.request.post(base+uploaded.headers['location']+'/confirm',form={'csrf':csrf},max_redirects=0).status == 303
                page.goto(book_url)
                page.wait_for_function("() => !document.getElementById('second-comment').disabled")
                page.evaluate("() => navigator.clipboard.writeText('浏览器粘贴的二读评论')")
                page.locator('#second-comment').focus()
                page.keyboard.press('Control+v')
                page.get_by_text('已保存',exact=True).wait_for()
                assert page.locator('#second-comment').input_value() == '浏览器粘贴的二读评论'
                assert '浏览器粘贴的二读评论' in page.request.get(book_url+'/export').text()
                assert not errors,errors
                browser.close()
            print('PASS: desktop file/crop/move/resize/zoom, real clipboard, textarea paste isolation, drop, failed-upload retry, PNG pixel accuracy, refresh/cancel; mobile touch/EXIF/rotate/whole-image/JPEG metadata removal and responsive layout; existing Markdown import/review text paste/export. No OCR or real device camera used.')
        finally:
            server.terminate()
            server.wait(timeout=10)
            log.close()


if __name__ == '__main__':
    run()
