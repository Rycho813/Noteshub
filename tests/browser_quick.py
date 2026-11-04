"""Real service-worker/IndexedDB offline and synchronization integration."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run():
    with tempfile.TemporaryDirectory(prefix='noteshub-v3-browser-') as tmp:
        tmp = Path(tmp); data = tmp/'data'
        subprocess.run([str(ROOT/'.venv/bin/python'),'-c',
                        'from pathlib import Path;import sys;from tests.test_archive import legacy_v3;legacy_v3(Path(sys.argv[1]))',str(data)],cwd=ROOT,check=True,capture_output=True)
        images=[]
        for i in range(2):
            image=tmp/f'page-{i}.png';Image.new('RGB',(320,100),(200+i*20,220,240)).save(image);images.append(str(image))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        port=int(os.environ.get('NOTESHUB_BROWSER_PORT',port))
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            listener.bind(('127.0.0.1',port))
        base=f'http://127.0.0.1:{port}'
        with (tmp/'server.log').open('w+') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','noteshub.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,
                                    env={**os.environ,'NOTESHUB_DATA_DIR':str(data),'NOTESHUB_COOKIE_SECURE':'false','NOTESHUB_OCR_ENABLED':'false'},stdout=log,stderr=log)
            try:
                with httpx.Client(trust_env=False) as c:
                    for _ in range(100):
                        try:
                            if c.get(base+'/login').status_code==200:break
                        except httpx.ConnectError:pass
                        if server.poll() is not None:raise RuntimeError('Test app exited')
                        time.sleep(.1)
                    else:raise RuntimeError('App readiness timeout')
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    ctx=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                    page=ctx.new_page();errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    def login(page):
                        page.goto(base+'/login');page.get_by_label('用户名',exact=True).fill('reader')
                        page.get_by_label('密码',exact=True).fill('only-for-test-password')
                        page.get_by_role('button',name='登录',exact=True).click();page.wait_for_url(base+'/')
                    def ready(page):page.wait_for_function("() => !document.getElementById('quick-thought').disabled")
                    def synced(page):
                        try:page.wait_for_function("() => document.getElementById('quick-status').textContent === '已同步'")
                        except Exception as exc:
                            raise AssertionError(page.evaluate("() => ({status:document.getElementById('quick-status').textContent,error:document.getElementById('quick-error').textContent,note:NotesHubQuick.getCurrent()})")) from exc
                    login(page);page.goto(base+'/quick');ready(page)
                    page.wait_for_function('() => Boolean(navigator.serviceWorker.controller)')
                    page.wait_for_timeout(900)
                    ctx.set_offline(True)
                    page.locator('#quick-thought').fill('离线语音输入的一读想法')
                    page.locator('#quick-files').set_input_files(images)
                    page.wait_for_function("() => document.querySelectorAll('#quick-photos li').length === 2 && document.getElementById('quick-photo-status').textContent === '图片已保存在本机'")
                    original_ids=page.evaluate('() => NotesHubQuick.getCurrent().photos.slice()')
                    page.locator('#quick-photos li').nth(1).get_by_role('button',name='前移',exact=True).click()
                    page.wait_for_function('(ids) => NotesHubQuick.getCurrent().photos[0] === ids[1]',arg=original_ids)
                    page.wait_for_timeout(200)
                    page.get_by_text('离线使用与本机备份',exact=True).click()
                    with page.expect_download() as d:page.locator('#quick-backup').click()
                    local_backup=tmp/'local-backup.json';d.value.save_as(local_backup)
                    page.reload();ready(page)
                    assert page.locator('#quick-thought').input_value()=='离线语音输入的一读想法'
                    assert page.locator('#quick-photos li').count()==2
                    assert page.evaluate('() => NotesHubQuick.getCurrent().photos')==list(reversed(original_ids))
                    note_id=page.evaluate('() => NotesHubQuick.getCurrent().id')
                    # Lose one committed sync response. Exact retry must not add a note.
                    def lost(route):
                        response=route.fetch();assert response.ok;route.abort()
                    page.route('**/api/paper/notes/*/sync',lost,times=1)
                    ctx.set_offline(False);page.evaluate("window.dispatchEvent(new Event('online'))")
                    page.wait_for_function("() => document.getElementById('quick-status').textContent.startsWith('同步失败')")
                    page.locator('#quick-sync').click();synced(page)
                    remote=page.request.get(base+'/api/paper/notes/'+note_id).json()
                    assert remote['first_comment']=='离线语音输入的一读想法'
                    assert [v['id'] for v in remote['photos']]==list(reversed(original_ids))
                    # A second device updates the cloud while this one is offline.
                    other=browser.new_context();pc=other.new_page();login(pc)
                    pc.goto(base+'/quick?note='+note_id);ready(pc)
                    ctx.set_offline(True)
                    page.locator('#quick-thought').fill('手机离线的新想法不能丢')
                    pc.get_by_text('补充原文与二读',exact=True).click()
                    pc.locator('#quick-original').fill('电脑补充的原文')
                    pc.locator('#quick-thought').fill('电脑端的新一读')
                    pc.locator('#quick-sync').click();synced(pc)
                    ctx.set_offline(False);page.evaluate("window.dispatchEvent(new Event('online'))")
                    page.wait_for_function("() => !document.getElementById('quick-copy').hidden")
                    assert page.locator('#quick-thought').input_value()=='手机离线的新想法不能丢'
                    assert '电脑端的新一读' in page.locator('#quick-remote').inner_text()
                    page.locator('#quick-copy').click();synced(page)
                    copied=page.evaluate('() => NotesHubQuick.getCurrent().id');assert copied!=note_id
                    assert page.request.get(base+'/api/paper/notes/'+note_id).json()['first_comment']=='电脑端的新一读'
                    assert page.request.get(base+'/api/paper/notes/'+copied).json()['first_comment']=='手机离线的新想法不能丢'
                    # Offline copied photos survive restart, and a photo can be removed.
                    ctx.set_offline(True);page.reload();ready(page)
                    page.locator('#quick-photos li').first.get_by_role('button',name='移除',exact=True).click()
                    page.wait_for_function("() => NotesHubQuick.getCurrent().photos.length === 1")
                    page.wait_for_timeout(200)
                    ctx.set_offline(False);page.locator('#quick-sync').click();synced(page)
                    assert len(page.request.get(base+'/api/paper/notes/'+copied).json()['photos'])==1
                    # Session expiry keeps text locally; logging in elsewhere permits retry.
                    pc.goto(base+'/');pc.get_by_role('button',name='退出登录',exact=True).click()
                    # Delete this browser's session too, without clearing IndexedDB.
                    ctx.clear_cookies()
                    page.locator('#quick-thought').fill('重新登录后再同步')
                    page.locator('#quick-sync').click()
                    page.wait_for_function("() => document.getElementById('quick-status').textContent.startsWith('同步失败')")
                    signin=ctx.new_page();login(signin)
                    page.locator('#quick-sync').click();synced(page)
                    assert page.request.get(base+'/api/paper/notes/'+copied).json()['first_comment']=='重新登录后再同步'
                    assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
                    # Cache contains only public shell assets, never private API data.
                    cached=page.evaluate('async () => { const keys=await caches.keys();const out=[];for(const k of keys) for(const r of await (await caches.open(k)).keys())out.push(new URL(r.url).pathname);return out; }')
                    assert all(v=='/quick' or v.startswith('/static/') for v in cached),cached
                    # Same-device tabs cannot silently overwrite each other's local edits.
                    ctx.set_offline(True)
                    tab=ctx.new_page();tab.goto(base+'/quick');ready(tab)
                    page.locator('#quick-thought').fill('第一个标签页的新内容')
                    page.wait_for_timeout(200)
                    tab.locator('#quick-thought').fill('另一个标签页不能被覆盖的内容')
                    tab.wait_for_function("() => document.getElementById('quick-error').textContent.includes('另一标签页')")
                    assert tab.locator('#quick-thought').input_value()=='另一个标签页不能被覆盖的内容'
                    tab.locator('#quick-copy').click()
                    tab.wait_for_function("() => document.getElementById('quick-error').textContent.includes('已另存')")
                    assert tab.evaluate('() => NotesHubQuick.getCurrent().id')!=copied
                    # Quota failure must retain the text and not claim durability.
                    tab.evaluate("() => { const original=IDBObjectStore.prototype.put;IDBObjectStore.prototype.put=function(value,...rest) { if(this.name==='notes') { IDBObjectStore.prototype.put=original;throw new DOMException('fixture quota','QuotaExceededError'); }return original.call(this,value,...rest); }; }")
                    tab.locator('#quick-thought').fill('磁盘不足时输入也不能消失')
                    tab.wait_for_function("() => document.getElementById('quick-status').textContent.startsWith('本机保存失败')")
                    assert tab.locator('#quick-thought').input_value()=='磁盘不足时输入也不能消失'
                    tab.locator('#quick-sync').click()
                    tab.wait_for_function("() => document.getElementById('quick-status').textContent.includes('仅本地保存')")
                    # A photo that cannot yet fit in IDB stays in page memory,
                    # is included in recovery export, and can be retried.
                    tab.evaluate("() => { const original=IDBObjectStore.prototype.put;IDBObjectStore.prototype.put=function(value,...rest) { if(this.name==='photos') { IDBObjectStore.prototype.put=original;throw new DOMException('fixture photo quota','QuotaExceededError'); }return original.call(this,value,...rest); }; }")
                    tab.locator('#quick-camera').set_input_files(images[0])
                    tab.wait_for_function("() => document.getElementById('quick-status').textContent.startsWith('本机保存失败')")
                    assert tab.locator('#quick-photos li').count()==2
                    # Even a photo retained only in memory can be viewed while
                    # the quota failure stays visible and all edits stay intact.
                    tab.locator('.quick-photo-open').last.click()
                    tab.wait_for_function("() => document.getElementById('quick-viewer-image').naturalWidth > 0")
                    tab.locator('#quick-viewer-in').click()
                    tab.locator('#quick-viewer-close').click()
                    assert tab.locator('#quick-thought').input_value()=='磁盘不足时输入也不能消失'
                    assert tab.locator('#quick-status').inner_text().startswith('本机保存失败')
                    tab.get_by_text('离线使用与本机备份',exact=True).click()
                    with tab.expect_download() as photo_recovery:tab.locator('#quick-backup').click()
                    import json
                    recovery=json.loads(Path(photo_recovery.value.path()).read_text())
                    assert len(recovery['unsaved']['photos'])==2
                    assert all(any(p['id']==photo_id for p in recovery['photos']) for photo_id in recovery['unsaved']['photos'])
                    tab.locator('#quick-sync').click()
                    tab.wait_for_function("() => document.getElementById('quick-status').textContent.includes('仅本地保存')")
                    tab.close(run_before_unload=False)
                    ctx.set_offline(False)
                    # Restore in a fresh local profile as new records, preserving originals.
                    restored=browser.new_context();rp=restored.new_page();login(rp);rp.goto(base+'/quick');ready(rp)
                    rp.locator('#quick-restore').set_input_files(local_backup)
                    rp.wait_for_function("() => document.getElementById('quick-error').textContent.includes('已作为新记录恢复')")
                    rp.locator('#quick-sync').click();rp.wait_for_timeout(1000)
                    assert not errors,errors
                    artifacts=ROOT/'test-results';artifacts.mkdir(exist_ok=True)
                    page.screenshot(path=artifacts/'v3-quick-mobile.png',full_page=True)
                    browser.close()
                print('PASS P0: real SW offline reload, durable text/multi-photo ordering, exact retry after lost response, two-device and two-tab conflicts with preserved copies, quota failure, removal, reauthentication, local export/restore and mobile layout. Zero OCR calls.')
            finally:
                server.terminate();server.wait(timeout=15)
                if server.returncode not in (0,-15):
                    log.seek(0);print(log.read()[-3000:])


if __name__=='__main__':run()
