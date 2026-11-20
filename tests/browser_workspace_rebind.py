"""Real IndexedDB workspace switching against an isolated synthetic server."""
import argparse
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from noteshub.auth import provision
from noteshub.config import Settings
from noteshub.db import Store
from noteshub.paper import PaperService
from tests.test_images import picture

def uid():return uuid.uuid4().hex

def seed(root):
    provision(root,'reader','workspace-test-password')
    store=Store(Settings(root,cookie_secure=False));paper=PaperService(store)
    books=[uid(),uid()];notes=[];deleted_books=[uid() for _ in range(5)];deleted_notes=[uid() for _ in range(3)]
    with store.connection() as db:
        for i,book in enumerate(books):db.execute('INSERT INTO books VALUES (?,?,?,1)',(book,'服务器书籍'+str(i+1),''))
        for kind,ids in [('book',deleted_books),('note',deleted_notes)]:
            db.executemany('INSERT INTO deleted_entities(kind,id,deleted_at) VALUES (?,?,1)',[(kind,id) for id in ids])
    for i in range(11):
        note=uid();notes.append(note)
        paper.sync(note,dict(book_id=books[0],revision=0,mutation_id=uid(),first_comment='服务器原文想法'+str(i),photo_ids=[]))
    photo=uid();paper.upload(notes[0],photo,picture())
    paper.sync(notes[0],dict(book_id=books[0],revision=1,mutation_id=uid(),first_comment='服务器原文想法0',photo_ids=[photo]))
    return store,dict(books=books,notes=notes,photo=photo,deleted_books=deleted_books,deleted_notes=deleted_notes,unsynced=uid())

def snapshot(store):
    with store.connection() as db:
        data={t:[tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in ('books','notes','paper_entries','paper_photos','paper_mutations','deleted_entities')}
    return data,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (store.root/'paper-images').iterdir()}

def workflow(browser,base,store,fixture):
    context=browser
    page=context.new_page();errors=[];writes=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:writes.append(r.url) if r.method=='POST' and '/api/' in r.url else None)
    page.goto(base+'/login');page.get_by_label('用户名',exact=True).fill('reader');page.get_by_label('密码',exact=True).fill('workspace-test-password')
    page.get_by_role('button',name='登录',exact=True).click();page.wait_for_url(base+'/')
    server=page.request.get(base+'/api/paper/context').json()
    # Seed the actual v1 IndexedDB before quick.js starts. Include dirty data in
    # both the wrong workspace AND the target's previous local namespace.
    page.evaluate('''async ({server,fixture}) => {
      const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('noteshub-quick',1);r.onupgradeneeded=()=>{for(const n of ['notes','photos','meta','recoveries'])r.result.createObjectStore(n,{keyPath:'key'});};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
      await new Promise((resolve,reject)=>{const tx=db.transaction(['notes','photos','meta','recoveries'],'readwrite');tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);
        tx.objectStore('meta').put({key:'context',value:{...server,workspace:'a'.repeat(64),csrf:''}});
        for(const w of ['a'.repeat(64),server.workspace]) {
          for(const id of [...fixture.deleted_notes,fixture.unsynced])tx.objectStore('notes').put({key:w+':'+id,id,workspace:w,book_id:fixture.books[0],first_comment:'不应上传',original_text:'',second_comment:'',photos:[fixture.photo],revision:0,localVersion:1,dirty:true});
          for(const id of fixture.deleted_books)tx.objectStore('meta').put({key:'book:'+w+':'+id,value:{id,title:'已删除的书',author:''}});
          tx.objectStore('photos').put({key:w+':'+fixture.photo,id:fixture.photo,workspace:w,blob:new Blob(['旧图片']),uploaded:false});
        }
      });db.close();
    }''',dict(server=server,fixture=fixture))
    before=snapshot(store)
    page.goto(base+'/quick?note='+fixture['unsynced'])
    expect(page.locator('#quick-workspace-switch')).to_be_visible()
    expect(page.locator('#quick-thought')).to_be_disabled()
    assert page.evaluate("async () => {try {await NotesHubQuick.api('/api/paper/notes');return false;} catch(e) {return e.message.includes('已阻止');}}")
    assert not writes
    # Reopening doesn't silently bypass identity or perform pull/POST.
    page.reload();expect(page.locator('#quick-workspace-switch')).to_be_visible()
    assert not writes and snapshot(store)==before
    # Failed confirmation cannot replace the local context or upload anything.
    page.route('**/api/paper/context',lambda r:r.fulfill(json={**server,'workspace':'invalid'}))
    page.locator('#quick-workspace-confirm').click()
    expect(page.locator('#quick-error')).to_contain_text('资料库信息无效')
    page.unroute('**/api/paper/context')
    page.route('**/api/paper/context',lambda r:r.fulfill(status=401,json={'detail':'请登录'}))
    page.locator('#quick-workspace-confirm').click()
    expect(page.locator('#quick-error')).to_contain_text('切换未完成')
    page.unroute('**/api/paper/context')
    page.locator('#quick-workspace-confirm').click()
    page.wait_for_url(base+'/quick');expect(page.locator('#quick-thought')).to_be_enabled()
    expect(page.locator('#quick-list button')).to_have_count(11)
    expect(page.locator('#quick-book option')).to_have_count(2)
    assert snapshot(store)==before and not writes
    assert fixture['unsynced'] not in page.url
    current=page.evaluate('NotesHubQuick.getContext()')
    assert current['workspace']==server['workspace'] and current['storage'].startswith(server['workspace']+':rebind:')
    old=page.evaluate('''async () => {const db=await new Promise(r=>{const q=indexedDB.open('noteshub-quick',1);q.onsuccess=()=>r(q.result);});const result=await new Promise(r=>{const q=db.transaction('notes').objectStore('notes').getAll();q.onsuccess=()=>r(q.result);});db.close();return result;}''')
    assert len([n for n in old if n['workspace']=='a'*64])==4
    assert len([n for n in old if n['workspace']==server['workspace']])==4
    old_photos=page.evaluate('''async () => {const db=await new Promise(r=>{const q=indexedDB.open('noteshub-quick',1);q.onsuccess=()=>r(q.result);});const rows=await new Promise(r=>{const q=db.transaction('photos').objectStore('photos').getAll();q.onsuccess=()=>r(q.result);});db.close();return await Promise.all(rows.map(async p=>({workspace:p.workspace,text:await p.blob.text()})));}''')
    assert len(old_photos)==2 and all(p['text']=='旧图片' for p in old_photos)
    # Repeated initialization is idempotent and performs no server mutation.
    for _ in range(2):
        page.reload();expect(page.locator('#quick-thought')).to_be_enabled();expect(page.locator('#quick-list button')).to_have_count(11)
    assert snapshot(store)==before and not writes
    # Reading the existing photo must not upload a duplicate.
    page.goto(base+'/quick?note='+fixture['notes'][0]);expect(page.locator('#quick-thought')).to_have_value('服务器原文想法0')
    expect(page.locator('.quick-photo-open img')).to_have_attribute('src',__import__('re').compile('blob:'))
    assert not writes
    page.locator('#quick-thought').fill('编辑后自动同步')
    expect(page.locator('#quick-status')).to_have_text('已同步',timeout=15000)
    with store.connection() as db:
        assert db.execute('SELECT first_comment FROM notes WHERE id=?',(fixture['notes'][0],)).fetchone()[0]=='编辑后自动同步'
        assert db.execute('SELECT COUNT(*) FROM paper_photos').fetchone()[0]==1
    page.goto(base+'/quick');expect(page.locator('#quick-thought')).to_be_enabled()
    page.locator('#quick-new').click();page.locator('#quick-thought').fill('新建自动同步')
    expect(page.locator('#quick-status')).to_have_text('已同步',timeout=15000)
    new_id=page.evaluate('NotesHubQuick.getCurrent().id')
    page.locator('#quick-files').set_input_files({'name':'new.png','mimeType':'image/png','buffer':picture()})
    expect(page.locator('#quick-photo-status')).to_have_text('图片已保存在本机')
    expect(page.locator('#quick-status')).to_have_text('已同步',timeout=15000)
    count_uploads=len([w for w in writes if '/photos/' in w]);assert count_uploads==1
    page.reload();expect(page.locator('#quick-thought')).to_have_value('新建自动同步')
    expect(page.locator('#quick-status')).to_have_text('已同步',timeout=15000)
    assert len([w for w in writes if '/photos/' in w])==count_uploads
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM books').fetchone()[0]==2
        assert db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]==12
        assert db.execute('SELECT COUNT(*) FROM paper_photos').fetchone()[0]==2
        assert not db.execute('SELECT 1 FROM notes WHERE id=?',(fixture['unsynced'],)).fetchone()
        assert db.execute('SELECT COUNT(*) FROM deleted_entities').fetchone()[0]==8
    # A changed server identity during an active session still blocks POST.
    page.wait_for_timeout(1000)  # Let the preceding reload's initial sync drain.
    # Change only the disposable server's workspace: Service Worker controlled
    # requests can bypass Playwright routing, especially in WebKit.
    with store.connection() as db:db.execute('UPDATE paper_workspace SET id=?',(uid(),))
    writes.clear();page.locator('#quick-thought').fill('身份变化后不得上传');page.locator('#quick-sync').click()
    expect(page.locator('#quick-workspace-switch')).to_be_visible()
    assert not writes
    assert not errors,errors
    context.close()
    print('PASS: mismatch block; failed/confirmed rebind; 2 books/11 notes; deleted 5 books/3 notes stay deleted; old namespaces retained without upload; photo read/upload dedup; auto edit/create sync; reload/idempotence; active identity change block',flush=True)

def run(engine):
    with tempfile.TemporaryDirectory(prefix='noteshub-workspace-') as tmp:
        root=Path(tmp);store,fixture=seed(root)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        with (root/'server.log').open('w') as log:
            process=subprocess.Popen([sys.executable,'-m','uvicorn','noteshub.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env={**os.environ,'NOTESHUB_DATA_DIR':str(root),'NOTESHUB_COOKIE_SECURE':'false','NOTESHUB_OCR_ENABLED':'false'},stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                with httpx.Client(trust_env=False) as client:
                    for _ in range(600):
                        try:
                            if client.get(base+'/login').status_code==200:break
                        except httpx.ConnectError:pass
                        time.sleep(.1)
                    else:raise RuntimeError('Server readiness failed')
                with sync_playwright() as p:
                    browser=getattr(p,engine).launch_persistent_context(str(root/'browser-profile'),headless=True,viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                    workflow(browser,base,store,fixture);browser.close()
                print('PASS '+engine+' workspace rebind',flush=True)
            finally:
                process.terminate();process.wait(timeout=15)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',choices=['chromium','webkit'],default='chromium');run(parser.parse_args().browser)
