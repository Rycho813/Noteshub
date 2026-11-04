"""Browser -> actual outbound worker/model -> browser adoption and cleanup UI."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

import httpx
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


def run(ocr_python):
    with tempfile.TemporaryDirectory(prefix='noteshub-local-ocr-browser-') as tmp:
        tmp=Path(tmp);data=tmp/'data'
        subprocess.run([str(ROOT/'.venv/bin/python'),'-c','from pathlib import Path;import sys;from tests.test_archive import legacy_v3;legacy_v3(Path(sys.argv[1]))',str(data)],cwd=ROOT,check=True,capture_output=True)
        images=[]
        for i,words in enumerate(('NotesHub page one','纸质书阅读 原文识别测试')):
            image=Image.new('RGB',(1100,240),'white')
            font='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf' if i==0 else '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
            ImageDraw.Draw(image).text((30,75),words,font=ImageFont.truetype(font,55),fill='black')
            path=tmp/f'page-{i}.png';image.save(path);images.append(str(path))
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        with (tmp/'app.log').open('w+') as log:
            server=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','noteshub.web:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,
                                    env={**os.environ,'NOTESHUB_DATA_DIR':str(data),'NOTESHUB_COOKIE_SECURE':'false','NOTESHUB_OCR_ENABLED':'false'},stdout=log,stderr=log)
            try:
                with httpx.Client(trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if client.get(base+'/login').status_code==200:break
                        except httpx.ConnectError:pass
                        if server.poll() is not None:raise RuntimeError('Test app exited')
                        time.sleep(.1)
                    else:raise RuntimeError('Readiness timeout')
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                    page=browser.new_page(viewport={'width':1280,'height':1000});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
                    page.goto(base+'/login');page.get_by_label('用户名',exact=True).fill('reader');page.get_by_label('密码',exact=True).fill('only-for-test-password')
                    page.get_by_role('button',name='登录',exact=True).click();page.wait_for_url(base+'/')
                    page.goto(base+'/quick');page.wait_for_function("() => !document.getElementById('quick-thought').disabled")
                    page.locator('#quick-thought').fill('电脑识别不能覆盖的一读')
                    page.get_by_text('补充原文与二读',exact=True).click()
                    page.locator('#quick-original').fill('原来手工填写的原文')
                    page.locator('#quick-second').fill('原来已有的二读')
                    page.locator('#quick-files').set_input_files(images)
                    page.wait_for_function("() => document.querySelectorAll('#quick-photos li').length===2 && document.getElementById('quick-photo-status').textContent==='图片已保存在本机'")
                    page.locator('#quick-sync').click()
                    page.wait_for_function("() => document.getElementById('quick-status').textContent==='已同步'")
                    note_id=page.evaluate('() => NotesHubQuick.getCurrent().id')
                    page.locator('#quick-ocr-section summary').click()
                    page.get_by_role('button',name='交给电脑识别',exact=True).click()
                    page.get_by_text('等待电脑领取',exact=False).wait_for()
                    # Generate a scoped token using the actual UI; never print it.
                    page.get_by_text('电脑本地 OCR',exact=True).click()
                    page.locator('#quick-token-new').click()
                    page.wait_for_function("() => document.getElementById('quick-token-value').value.startsWith('nh_')")
                    token=page.locator('#quick-token-value').input_value()
                    config=tmp/'worker'/'local-ocr.json';config.parent.mkdir(mode=0o700)
                    config.write_text(json.dumps({'url':base,'token':token}));config.chmod(0o600)
                    process=subprocess.run([str(ocr_python),str(ROOT/'tools/local_ocr/worker.py'),'--config',str(config),'run','--max-jobs','5'],capture_output=True,text=True,timeout=120)
                    assert process.returncode==0,process.stdout+process.stderr
                    state=page.request.get(base+'/api/paper/notes/'+note_id).json()
                    assert state['original_text']=='原来手工填写的原文'
                    assert state['first_comment']=='电脑识别不能覆盖的一读'
                    assert state['second_comment']=='原来已有的二读'
                    jobs=page.request.get(base+'/api/paper/notes/'+note_id+'/ocr').json()['jobs']
                    assert len(jobs)==1 and jobs[0]['state']=='succeeded'
                    assert 'NotesHub' in jobs[0]['results'][0]['text']
                    assert '纸质书' in jobs[0]['results'][1]['text'] and '原文' in jobs[0]['results'][1]['text']
                    page.get_by_role('button',name='刷新本地识别状态',exact=True).click()
                    page.get_by_text('识别结果 ·',exact=False).first.click()
                    page.get_by_role('button',name='采用为原文，再人工校对',exact=True).click()
                    page.wait_for_function("() => document.getElementById('quick-original').value.includes('〔图片 2〕')")
                    candidate=page.locator('#quick-original').input_value()
                    assert 'NotesHub' in candidate and '纸质书' in candidate
                    assert page.locator('#quick-thought').input_value()=='电脑识别不能覆盖的一读'
                    page.locator('#quick-original').fill('已人工核对\n'+candidate)
                    page.locator('#quick-sync').click();page.wait_for_function("() => document.getElementById('quick-status').textContent==='已同步'")
                    page.get_by_role('button',name='已校对并同步，30 天后清理图片',exact=True).click()
                    page.get_by_role('button',name='取消图片清理',exact=True).wait_for()
                    confirmed=page.request.get(base+'/api/paper/notes/'+note_id).json()
                    assert confirmed['confirmed_revision']==confirmed['revision']
                    assert all(photo['purge_after']>time.time()+29*86400 for photo in confirmed['photos'])
                    page.get_by_role('button',name='取消图片清理',exact=True).click()
                    page.get_by_role('button',name='已校对并同步，30 天后清理图片',exact=True).wait_for()
                    assert page.request.get(base+'/api/paper/notes/'+note_id).json()['confirmed_revision'] is None
                    # Reader saves the same paper note with revision protection.
                    page.locator('#quick-formal').click()
                    row=page.locator('[data-note-id="'+note_id+'"]')
                    row.locator('.second-reading summary').click()
                    row.locator('.second-comment').fill('统一阅读页继续二读')
                    from playwright.sync_api import expect
                    expect(row.locator('.save-status')).to_have_text('已保存')
                    assert '统一阅读页继续二读' in page.request.get(base+'/books/book/export').text()
                    assert '电脑识别不能覆盖的一读' in page.request.get(base+'/books/book/export').text()
                    assert not errors,errors
                    artifacts=ROOT/'test-results';artifacts.mkdir(exist_ok=True)
                    page.screenshot(path=artifacts/'v3-local-ocr-desktop.png',full_page=True)
                    browser.close()
                print('PASS actual CPU model: browser multi-photo upload -> secure task -> real RapidOCR worker -> per-page results -> explicit adoption with comments retained -> correction/sync -> 30-day cleanup confirmation/undo -> existing reader/export. Zero paid OCR.')
            finally:
                server.terminate();server.wait(timeout=15)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--ocr-python',required=True,type=Path)
    run(parser.parse_args().ocr_python)
