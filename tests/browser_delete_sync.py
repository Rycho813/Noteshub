"""Synthetic multi-device deletion/offline/photo regression on random loopback."""
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from noteshub.auth import provision
from noteshub.config import Settings
from noteshub.db import Store
from tests.browser_photo_viewer import login, ready
from tests.test_web import csrf


def local_rows(page, name):
    return page.evaluate("""name => new Promise((resolve,reject) => {
      const q=indexedDB.open('noteshub-quick',1);q.onsuccess=()=>{
        const db=q.result,r=db.transaction(name).objectStore(name).getAll();
        r.onsuccess=()=>{resolve(r.result.map(v=>({id:v.id,key:v.key,dirty:v.dirty,note_id:v.note_id})));db.close();};r.onerror=()=>reject(r.error);
      };
    })""", name)


def exercise(browser, mobile):
    with tempfile.TemporaryDirectory(prefix='noteshub-delete-sync-') as folder:
        data=Path(folder);provision(data,'reader','only-for-test-password')
        store=Store(Settings(data,cookie_secure=False))
        book,other=uuid.uuid4().hex,uuid.uuid4().hex
        with store.connection() as db:
            db.executemany('INSERT INTO books VALUES (?,?,?,1)',[(book,'目标书',''),(other,'保留书','')])
        image=io.BytesIO();Image.new('RGB',(80,60),'orange').save(image,format='PNG')
        upload={'name':'synthetic.png','mimeType':'image/png','buffer':image.getvalue()}
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        with (data/'server.log').open('w') as log:
            server=subprocess.Popen([sys.executable,'-m','uvicorn','noteshub.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,
                env={**os.environ,'NOTESHUB_DATA_DIR':str(data),'NOTESHUB_COOKIE_SECURE':'false','NOTESHUB_OCR_ENABLED':'false'},stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                with httpx.Client(base_url=base,trust_env=False,follow_redirects=False) as client:
                    for _ in range(100):
                        try:
                            if client.get('/login').status_code==200:break
                        except httpx.ConnectError:pass
                        time.sleep(.1)
                    else:raise RuntimeError('Temporary server readiness failed')
                    assert client.post('/login',data={'csrf':csrf(client.get('/login')),'username':'reader','password':'only-for-test-password'}).status_code==303
                    def delete(path):
                        assert client.post(path,data={'csrf':csrf(client.get('/')),'confirm':'delete'}).status_code==303
                    context=browser.new_context(viewport={'width':390,'height':844} if mobile else {'width':1280,'height':900},is_mobile=mobile,has_touch=mobile)
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    try:
                        login(page,base);page.goto(base+'/quick?book='+book);ready(page)
                        page.locator('#quick-thought').fill('A 已同步记录')
                        page.locator('#quick-files').set_input_files([upload,upload])
                        expect(page.locator('#quick-status')).to_have_text('已同步',timeout=30000)
                        a=page.evaluate('() => NotesHubQuick.getCurrent().id')
                        context.set_offline(True)
                        # Legacy downloaded blobs may lack note_id ownership.
                        page.evaluate("""() => new Promise(resolve => {
                          const q=indexedDB.open('noteshub-quick',1);q.onsuccess=()=>{const db=q.result,tx=db.transaction('photos','readwrite'),s=tx.objectStore('photos'),r=s.getAll();
                            r.onsuccess=()=>{for(const p of r.result){delete p.note_id;s.put(p);}};tx.oncomplete=()=>{db.close();resolve();};};
                        })""")
                        page.locator('#quick-photos li').first.get_by_role('button',name='移除',exact=True).click()
                        page.locator('#quick-thought').fill('A 离线修改')
                        page.locator('#quick-new').click();page.locator('#quick-thought').fill('B 其他离线记录')
                        page.locator('#quick-files').set_input_files(upload)
                        expect(page.locator('#quick-photo-status')).to_have_text('图片已保存在本机')
                        b=page.evaluate('() => NotesHubQuick.getCurrent().id')
                        delete('/books/'+book+'/notes/'+a+'/delete')
                        # Server/network errors do not authorize deleting any local data.
                        page.route('**/api/paper/deletions',lambda route:route.fulfill(status=503,json={'detail':'synthetic outage'}))
                        context.set_offline(False)
                        expect(page.locator('#quick-error')).to_contain_text('synthetic outage',timeout=30000)
                        assert {r['id'] for r in local_rows(page,'notes')}=={a,b}
                        assert len(local_rows(page,'photos'))==3
                        page.unroute('**/api/paper/deletions')
                        page.evaluate('() => NotesHubQuick.sync()')
                        expect(page.locator('#quick-status')).to_have_text('已同步',timeout=30000)
                        assert {r['id'] for r in local_rows(page,'notes')}=={b}
                        assert len(local_rows(page,'photos'))==1
                        assert client.get('/api/paper/notes/'+a).status_code==410
                        assert client.get('/api/paper/notes/'+b).json()['first_comment']=='B 其他离线记录'
                        assert len(list((data/'paper-images').glob('*')))==1
                        # Deleted book with a lost create ACK must not block C.
                        context.set_offline(True)
                        page.locator('#quick-new').click();page.locator('#quick-book').select_option(other)
                        page.locator('#quick-thought').fill('C 保留书的新记录')
                        expect(page.locator('#quick-status')).to_contain_text('仅本地保存')
                        c=page.evaluate('() => NotesHubQuick.getCurrent().id')
                        page.evaluate("""book => new Promise(resolve => {
                          const q=indexedDB.open('noteshub-quick',1);q.onsuccess=()=>{const db=q.result,tx=db.transaction('meta','readwrite');
                            tx.objectStore('meta').put({key:'book:'+NotesHubQuick.getContext().workspace+':'+book,value:{id:book,title:'目标书',author:''}});
                            tx.oncomplete=()=>{db.close();resolve();};};
                        })""",book)
                        delete('/books/'+book+'/delete')
                        context.set_offline(False)
                        expect(page.locator('#quick-status')).to_have_text('已同步',timeout=30000)
                        assert {r['id'] for r in local_rows(page,'notes')}=={c}
                        assert local_rows(page,'photos')==[]
                        assert not any(r['key'].endswith(':'+book) for r in local_rows(page,'meta'))
                        assert client.get('/api/paper/notes/'+c).json()['first_comment']=='C 保留书的新记录'
                        assert list((data/'paper-images').glob('*'))==[]
                        page.goto(base+'/quick');ready(page)
                        expect(page.locator('#quick-thought')).to_have_value('C 保留书的新记录')
                        page.reload();ready(page)
                        expect(page.locator('#quick-thought')).to_have_value('C 保留书的新记录')
                        # Deletion can race after the feed fetch: handle the
                        # actual 410 too, not only already-known tombstones.
                        page.locator('#quick-new').click();page.locator('#quick-thought').fill('D racing record')
                        expect(page.locator('#quick-status')).to_have_text('已同步',timeout=30000)
                        d=page.evaluate('() => NotesHubQuick.getCurrent().id')
                        context.set_offline(True)
                        page.locator('#quick-thought').fill('D offline edit')
                        page.locator('#quick-new').click();page.locator('#quick-thought').fill('E must still sync')
                        expect(page.locator('#quick-status')).to_contain_text('仅本地保存')
                        e=page.evaluate('() => NotesHubQuick.getCurrent().id')
                        def racing_delete(route):
                            delete('/books/'+other+'/notes/'+d+'/delete')
                            route.continue_()
                        page.route('**/api/paper/notes/'+d+'/sync',racing_delete,times=1)
                        context.set_offline(False)
                        expect(page.locator('#quick-status')).to_have_text('已同步',timeout=30000)
                        assert {r['id'] for r in local_rows(page,'notes')}=={c,e}
                        assert client.get('/api/paper/notes/'+e).json()['first_comment']=='E must still sync'
                        page.goto(base+'/quick?note='+d);ready(page)
                        expect(page.locator('#quick-thought')).to_have_value('')
                        expect(page.locator('#quick-error')).to_contain_text('永久删除',timeout=30000)
                        assert {r['id'] for r in local_rows(page,'notes')}=={c,e}
                        page.goto(base+'/quick?note='+c);ready(page)
                        expect(page.locator('#quick-thought')).to_have_value('C 保留书的新记录')
                        assert not errors,errors
                        print('PASS '+('mobile' if mobile else 'desktop')+': dirty deleted A cannot block B; 503 preserves local notes/photos; attached/detached photos removed; deleted pending book cannot block C; clean cached record removed; reload preserved C',flush=True)
                    finally:context.close()
            finally:server.terminate();server.wait(timeout=15)


if __name__=='__main__':
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            for mobile in (False,True):exercise(browser,mobile)
        finally:browser.close()
