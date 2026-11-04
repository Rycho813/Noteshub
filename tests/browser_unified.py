"""Seven-point workflow, synthetic data only, loopback 18081."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid

import httpx
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from browser_photo_viewer import login, ready, BOOK_SELECT_LINE

ROOT=Path(__file__).resolve().parents[1]


def synced(page):
    expect(page.locator('#quick-status')).to_have_text('已同步',timeout=30000)



def credentials(context,base,tmp,label):
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    login(page,base);page.goto(base+'/quick');ready(page)
    page.locator('#quick-desktop summary').click()
    page.locator('#quick-token-new').click()
    page.wait_for_function("() => document.getElementById('quick-token-value').value.startsWith('nh_')")
    expect(page.locator('#quick-token-value')).to_have_attribute('type','password')
    expect(page.locator('#quick-token-tools')).to_be_visible()
    page.locator('#quick-token-show').click()
    expect(page.locator('#quick-token-value')).to_have_attribute('type','text')
    page.locator('#quick-token-show').click()
    expect(page.locator('#quick-token-value')).to_have_attribute('type','password')
    context.grant_permissions(['clipboard-read','clipboard-write'])
    page.locator('#quick-token-copy').click()
    expect(page.locator('#quick-token-status')).to_contain_text('已复制完整凭证')
    assert page.evaluate("async () => (await navigator.clipboard.readText())===document.getElementById('quick-token-value').value")
    assert page.evaluate("""async () => {
      const secret=document.getElementById('quick-token-value').value;
      const db=await new Promise((resolve,reject) => {const r=indexedDB.open('noteshub-quick');r.onsuccess=() => resolve(r.result);r.onerror=reject;});
      try {
        for(const name of db.objectStoreNames) {
          const rows=await new Promise((resolve,reject) => {const r=db.transaction(name).objectStore(name).getAll();r.onsuccess=() => resolve(r.result);r.onerror=reject;});
          if(JSON.stringify(rows).includes(secret))return false;
        }
        return true;
      } finally {db.close();}
    }""")
    # A rejected clipboard permission offers selectable visible text.
    page.evaluate("() => Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async () => {throw new DOMException('fixture denied','NotAllowedError');}}})")
    page.locator('#quick-token-copy').click()
    expect(page.locator('#quick-token-status')).to_contain_text('已显示并选中')
    expect(page.locator('#quick-token-value')).to_have_attribute('type','text')
    assert page.locator('#quick-token-value').evaluate('(input) => input.selectionStart===0 && input.selectionEnd===input.value.length')
    page.locator('#quick-desktop summary').click()
    expect(page.locator('#quick-token-value')).to_have_value('')
    expect(page.locator('#quick-token-tools')).to_be_hidden()
    # A response arriving after the panel closes must not redisplay a secret.
    page.locator('#quick-desktop summary').click()
    page.evaluate("""() => {
      const q=NotesHubQuick,original=q.api;
      q.api=async (...args) => {
        const response=await original(...args);
        if(args[0]==='/api/paper/worker-tokens' && args[1])await new Promise(resolve => {window.releaseToken=resolve;});
        return response;
      };
    }""")
    page.locator('#quick-token-new').click()
    page.wait_for_function('() => Boolean(window.releaseToken)')
    page.locator('#quick-desktop summary').click()
    page.evaluate('() => window.releaseToken()')
    expect(page.locator('#quick-token-new')).to_be_enabled()
    expect(page.locator('#quick-token-value')).to_have_value('')
    assert not errors,errors
    page.close();print('PASS credential show/hide/copy/fallback/private-storage/late-response '+label,flush=True)


def workflow(context, base, tmp, label):
    page=context.new_page();errors=[]
    def watch(target):
        target.on('pageerror',lambda e:errors.append(str(e)))
        def console(msg):
            if 'Content Security Policy' not in msg.text:return
            loc=msg.location
            if label.startswith('webkit') and msg.text.startswith('Refused to apply a stylesheet') and loc.get('lineNumber',loc.get('line'))==BOOK_SELECT_LINE:return
            errors.append(msg.text)
        target.on('console',console)
    watch(page)
    login(page,base)
    page.goto(base+'/quick?book=book');ready(page)
    assert not page.locator('#quick-ocr-section').get_attribute('open')
    page.wait_for_function('() => Boolean(navigator.serviceWorker.controller)')
    context.set_offline(True)
    page.locator('#quick-thought').fill('离线建书前正在输入的想法')
    page.locator('#quick-files').set_input_files([tmp/'one.png',tmp/'two.png'])
    expect(page.locator('#quick-photo-status')).to_have_text('图片已保存在本机')
    original=page.evaluate('() => NotesHubQuick.getCurrent()')
    page.locator('#quick-create-book summary').click()
    page.locator('#quick-book-title').fill('离线新书 '+label)
    page.locator('#quick-book-author').fill('检索作者 '+label)
    page.locator('#quick-book-create').click()
    page.wait_for_function("() => document.getElementById('quick-book').selectedOptions[0].textContent.includes('离线新书')")
    expect(page.locator('#quick-thought')).to_have_value('离线建书前正在输入的想法')
    assert page.evaluate('() => NotesHubQuick.getCurrent().photos')==original['photos']
    book=page.evaluate('() => NotesHubQuick.getCurrent().book_id')
    page.locator('#quick-book-search').fill('不会匹配的词')
    assert page.evaluate('() => NotesHubQuick.getCurrent().book_id')==book
    page.locator('#quick-book-search').fill('检索作者 '+label)
    assert page.locator('#quick-book option').count()==1
    # Crop cancel leaves identity and bytes intact, apply creates a new version.
    page.locator('.quick-photo-open').first.click()
    page.locator('#quick-viewer-edit').click()
    page.locator('#quick-edit-rotate').click()
    page.locator('#quick-edit-cancel').click()
    page.locator('#quick-viewer-close').click()
    assert page.evaluate('() => NotesHubQuick.getCurrent().photos')==original['photos']
    page.locator('.quick-photo-open').first.click();page.locator('#quick-viewer-edit').click()
    page.locator('#quick-edit-rotate').click()
    page.locator('#quick-crop-w').fill('100');page.locator('#quick-crop-w').dispatch_event('change')
    page.locator('#quick-crop-h').fill('160');page.locator('#quick-crop-h').dispatch_event('change')
    page.locator('#quick-edit-apply').click()
    expect(page.locator('#quick-photo-status')).to_have_text('编辑后的图片已保存在本机')
    page.wait_for_function("() => document.getElementById('quick-viewer-image').naturalWidth===100 && document.getElementById('quick-viewer-image').naturalHeight===160")
    page.locator('#quick-viewer-next').click()
    expect(page.locator('#quick-viewer-status')).to_have_text('图片 2 / 2 · 100%')
    page.locator('#quick-viewer-close').click()
    edited=page.evaluate('() => NotesHubQuick.getCurrent().photos')
    assert edited[0]!=original['photos'][0] and edited[1]==original['photos'][1]
    # Clipboard handling: actual keyboard paste on Chromium, ClipboardEvent on WebKit.
    encoded=base64.b64encode((tmp/'one.png').read_bytes()).decode()
    if label.startswith('chromium'):
        context.grant_permissions(['clipboard-read','clipboard-write'])
        page.evaluate("""async encoded => {
          const blob=new Blob([Uint8Array.from(atob(encoded),c => c.charCodeAt(0))],{type:'image/png'});
          await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
        }""",encoded)
        page.locator('#quick-thought').focus();page.keyboard.press('Control+v')
    else:
        page.evaluate("""encoded => {
          const blob=new Blob([Uint8Array.from(atob(encoded),c => c.charCodeAt(0))],{type:'image/png'});
          const data=new DataTransfer();data.items.add(new File([blob],'paste.png',{type:'image/png'}));
          document.getElementById('quick-thought').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
        }""",encoded)
    page.wait_for_function('() => NotesHubQuick.getCurrent().photos.length===3')
    expect(page.locator('#quick-photo-status')).to_have_text('图片已保存在本机')
    assert page.evaluate('''() => {
      const data=new DataTransfer();data.setData('text/plain','普通文本');
      return document.getElementById('quick-thought').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
    }''')
    expect(page.locator('#quick-thought')).to_have_value('离线建书前正在输入的想法')
    # New book + cropped images survive a completely closed page and offline reopen.
    page.wait_for_timeout(200);page.close();page=context.new_page();watch(page)
    page.goto(base+'/quick');ready(page)
    expect(page.locator('#quick-thought')).to_have_value('离线建书前正在输入的想法')
    assert page.evaluate('() => NotesHubQuick.getCurrent().book_id')==book
    assert page.locator('.quick-photo-open').count()==3
    note=page.evaluate('() => NotesHubQuick.getCurrent().id')
    # Lose the new-book acknowledgment. Retry must create exactly one book/note.
    page.evaluate("""() => {
      window.testOriginalFetch=window.fetch;
      window.fetch=async (...args) => {
        const response=await window.testOriginalFetch(...args);
        if(String(args[0]).startsWith('/api/paper/books/') && args[1]?.method==='POST')throw new TypeError('synthetic lost book acknowledgment');
        return response;
      };
    }""")
    context.set_offline(False);page.evaluate("dispatchEvent(new Event('online'))")
    expect(page.locator('#quick-status')).to_contain_text('同步失败',timeout=15000)
    page.evaluate('() => {window.fetch=window.testOriginalFetch;delete window.testOriginalFetch;}')
    page.locator('#quick-sync').click();synced(page)
    remote=page.request.get(base+'/api/paper/notes/'+note).json()
    assert remote['book_id']==book and len(remote['photos'])==3
    raw=page.request.get(base+'/api/paper/notes/'+note+'/photos/'+edited[0]).body()
    with Image.open(io.BytesIO(raw)) as image:assert image.size==(100,160)
    books=page.request.get(base+'/api/paper/context').json()['books']
    assert sum(b['title']=='离线新书 '+label for b in books)==1
    # Editing a synced photo also uses a new immutable ID; old source remains readable.
    page.locator('.quick-photo-open').first.click();page.locator('#quick-viewer-edit').click()
    page.locator('#quick-crop-w').fill('80');page.locator('#quick-crop-w').dispatch_event('change')
    page.locator('#quick-edit-apply').click()
    page.wait_for_function("() => document.getElementById('quick-viewer-image').naturalWidth===80")
    page.locator('#quick-viewer-close').click();synced(page)
    assert page.request.get(base+'/api/paper/notes/'+note+'/photos/'+edited[0]).ok
    # Move current record to an existing book, retaining ID/photos; then move back.
    page.locator('#quick-book-search').fill('')
    revision=page.evaluate('() => NotesHubQuick.getCurrent().revision')
    page.locator('#quick-book').select_option('book')
    page.wait_for_function('(r) => NotesHubQuick.getCurrent().book_id==="book" && NotesHubQuick.getCurrent().revision>r && !NotesHubQuick.getCurrent().dirty',arg=revision)
    synced(page)
    assert page.request.get(base+'/api/paper/notes/'+note).json()['book_id']=='book'
    revision=page.evaluate('() => NotesHubQuick.getCurrent().revision')
    page.locator('#quick-book').select_option(book)
    page.wait_for_function('(v) => NotesHubQuick.getCurrent().book_id===v.book && NotesHubQuick.getCurrent().revision>v.revision && !NotesHubQuick.getCurrent().dirty',arg={'book':book,'revision':revision})
    synced(page)
    assert page.evaluate('() => NotesHubQuick.getCurrent().id')==note
    page.locator('#quick-new').click()
    assert page.evaluate('() => NotesHubQuick.getCurrent().book_id')==book
    assert page.evaluate('() => NotesHubQuick.getCurrent().id')!=note
    # Add 61 text notes using the real sync API, yielding 62 rows / three pages.
    csrf=page.request.get(base+'/api/paper/context').json()['csrf']
    for i in range(61):
        id=uuid.uuid4().hex
        r=page.request.post(base+'/api/paper/notes/'+id+'/sync',headers={'X-CSRF-Token':csrf},data={'mutation_id':uuid.uuid4().hex,'book_id':book,'revision':0,'original_text':'原文 '+str(i),'first_comment':'一读 '+str(i),'second_comment':'已有二读' if i==0 else '', 'photo_ids':[]})
        assert r.ok
    page.goto(base+'/books/'+book)
    expect(page.locator('.review-editor')).to_have_count(30)
    assert page.locator('.second-reading[open],.record-ocr[open],iframe').count()==0
    assert not page.get_by_role('link',name='图片摘录 · 上传与草稿').count()
    assert len(page.locator('[id]').evaluate_all('(nodes) => nodes.map(n => n.id)'))==len(set(page.locator('[id]').evaluate_all('(nodes) => nodes.map(n => n.id)')))
    row=page.locator('.review-editor').nth(0);row2=page.locator('.review-editor').nth(1)
    row.locator('.second-reading summary').click();row2.locator('.second-reading summary').click()
    row.locator('.second-comment').fill('第一条最新二读')
    row2.locator('.second-comment').fill('第二条最新二读')
    row.locator('.second-reading summary').click()
    page.get_by_role('link',name='下一页',exact=True).click();page.wait_for_url('**page=2')
    expect(page.locator('.review-editor')).to_have_count(30)
    page.get_by_role('link',name='上一页',exact=True).click();page.wait_for_url('**page=1')
    expect(page.locator('.second-comment').nth(0)).to_have_value('第一条最新二读')
    expect(page.locator('.second-comment').nth(1)).to_have_value('第二条最新二读')
    # Save failure blocks pagination and keeps the exact row editable for retry.
    row=page.locator('.review-editor').first;row.locator('.second-reading summary').click()
    page.route('**/api/books/*/notes/*',lambda route:route.fulfill(status=503,json={'detail':'synthetic row failure'}))
    row.locator('.second-comment').fill('失败时保留的输入');page.get_by_role('link',name='下一页',exact=True).click()
    expect(row.locator('.save-status')).to_contain_text('保存失败')
    assert 'page=1' in page.url
    page.unroute('**/api/books/*/notes/*');row.locator('.save-now').click();expect(row.locator('.save-status')).to_have_text('已保存')
    # Jump to final page; export remains whole book and drains unsaved row input.
    page.get_by_label('跳到页码').fill('3');page.get_by_role('button',name='跳转',exact=True).click();page.wait_for_url('**page=3')
    expect(page.locator('.review-editor')).to_have_count(2)
    last=page.locator('.review-editor').last;last.locator('.second-reading summary').click();last.locator('.second-comment').fill('最后一页导出前输入')
    with page.expect_download() as download:page.locator('#export-book').click()
    exported=Path(download.value.path()).read_text()
    assert exported.count('## 笔记 ')==62
    for text in ('失败时保留的输入','第二条最新二读','最后一页导出前输入','离线建书前正在输入的想法'):assert text in exported
    # On-demand book OCR panel reuses the source images and saves candidates only explicitly.
    page.get_by_label('跳到页码').fill('1');page.get_by_role('button',name='跳转',exact=True).click();page.wait_for_url('**page=1')
    first=page.locator('.review-editor').first
    first.locator('.record-ocr summary').click()
    iframe=page.frame_locator('.record-ocr-frame')
    expect(iframe.locator('#quick-thought')).to_have_value('离线建书前正在输入的想法')
    expect(iframe.locator('.quick-photo-open')).to_have_count(3)
    iframe.get_by_role('button',name='交给电脑识别',exact=True).click()
    expect(iframe.get_by_text('等待电脑领取',exact=False)).to_be_visible()
    # Synthetic worker completion writes only candidate results; no paid or real model call.
    with sqlite3.connect(tmp/'data'/'noteshub.sqlite3') as db:
        before=db.execute('SELECT original_text,first_comment,second_comment FROM notes WHERE id=?',(note,)).fetchone()
        results=[{'text':'合成候选 '+str(i),'confidence':.9} for i in range(3)]
        db.execute("UPDATE paper_ocr_jobs SET state='succeeded',results_json=?,engine='synthetic-browser',finished_at=? WHERE note_id=?",(json.dumps(results),int(time.time()),note))
    iframe.get_by_role('button',name='刷新本地识别状态',exact=True).click()
    iframe.get_by_text('识别结果 ·',exact=False).click()
    assert page.request.get(base+'/api/paper/notes/'+note).json()['original_text']==before[0]
    iframe.get_by_role('button',name='采用为原文，再人工校对',exact=True).click()
    iframe.locator('#quick-original').fill('人工校对后的候选')
    iframe.locator('#quick-sync').click();expect(iframe.locator('#quick-status')).to_have_text('已同步',timeout=30000)
    expect(first.locator('.original')).to_have_text('人工校对后的候选')
    remote=page.request.get(base+'/api/paper/notes/'+note).json()
    assert remote['first_comment']==before[1] and remote['second_comment']==before[2]
    assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
    if label.startswith('chromium'):page.screenshot(path=ROOT/'test-results'/('unified-'+label+'.png'),full_page=True)
    # The 21st selection must retain the first 20 images and explain the limit.
    page.goto(base+'/quick?book='+book);ready(page);context.set_offline(True)
    page.locator('#quick-files').set_input_files([tmp/'one.png']*21)
    expect(page.locator('#quick-error')).to_contain_text('最多 20 张',timeout=30000)
    expect(page.locator('.quick-photo-open')).to_have_count(20)
    assert len(set(page.evaluate('() => NotesHubQuick.getCurrent().photos')))==20
    assert not errors,errors
    page.close()
    print('PASS workflow '+label,flush=True)


def run(name,mode='all'):
    with tempfile.TemporaryDirectory(prefix='noteshub-unified-') as folder:
        tmp=Path(folder);data=tmp/'data'
        subprocess.run([str(ROOT/'.venv/bin/python'),'-c','from pathlib import Path;import sys;from tests.test_archive import legacy_v3;legacy_v3(Path(sys.argv[1]))',str(data)],cwd=ROOT,check=True,capture_output=True)
        Image.new('RGB',(320,200),'#ff7755').save(tmp/'one.png');Image.new('RGB',(200,320),'#5577ff').save(tmp/'two.png')
        base='http://127.0.0.1:18081'
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',18081))
        with (tmp/'server.log').open('w+') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','noteshub.web:create_app','--factory','--host','127.0.0.1','--port','18081'],cwd=ROOT,env={**os.environ,'NOTESHUB_DATA_DIR':str(data),'NOTESHUB_COOKIE_SECURE':'false','NOTESHUB_OCR_ENABLED':'false'},stdout=log,stderr=log)
            try:
                with httpx.Client(trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if client.get(base+'/login').status_code==200:break
                        except httpx.ConnectError:pass
                        if server.poll() is not None:raise RuntimeError('Temporary app exited')
                        time.sleep(.1)
                    else:raise RuntimeError('Readiness timeout')
                with sync_playwright() as p:
                    engine=getattr(p,name)
                    for mobile in ((True,) if mode=='mobile' else (False,) if mode=='desktop' else (False,True)):
                        label=name+('-mobile' if mobile else '-desktop')
                        context=engine.launch_persistent_context(str(tmp/label),headless=True,viewport={'width':390,'height':844} if mobile else {'width':1280,'height':1000},is_mobile=mobile,has_touch=mobile,**({'args':['--no-sandbox']} if name=='chromium' else {}))
                        try:(credentials if mode=='credentials' else workflow)(context,base,tmp,label)
                        except Exception:
                            for failed in context.pages:
                                try:print('Failure UI:',failed.evaluate("() => ({url:location.href,error:document.getElementById('quick-error')?.textContent,viewer:document.getElementById('quick-viewer')?.open,editing:document.getElementById('quick-image-edit')?.hidden,diag:document.getElementById('quick-viewer')?.dataset})"),flush=True)
                                except Exception:pass
                            raise
                        finally:context.close()
                print('PASS '+name+' ('+mode+'):  offline books/retry/reopen, crop bytes and immutable versions, clipboard, moves, 30-row pagination/multi-row saves/failure/export, on-demand book OCR candidate/correction; desktop and mobile. Zero paid OCR.')
            finally:
                server.terminate();server.wait(timeout=15)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--browser',choices=('chromium','webkit'),default='chromium');parser.add_argument('--mode',choices=('all','desktop','mobile','credentials'),default='all');args=parser.parse_args();run(args.browser,args.mode)
