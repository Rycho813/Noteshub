"""Actual old-v3-volume -> backup -> v4 upgrade/archive/restore, zero OCR calls.

Usage: .venv/bin/python tests/archive_container_upgrade.py OLD_RELEASE NEW_RELEASE
Requires both verified test images and an unused local test project/port/volume.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.test_archive import legacy_v3
from tests.test_web import PASSWORD

DOCKER=['docker','--host=unix:///var/run/docker.sock']
SELECTORS=('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')
PROJECT='noteshub-v2-ocr-test'
VOLUME=PROJECT+'_data'
SNAPSHOT='''import hashlib,json,sqlite3
from pathlib import Path
root=Path('/data')
with sqlite3.connect(root/'noteshub.sqlite3') as db:
 tables={}
 for table in ('books','imports','image_drafts','ocr_runs'):
  tables[table]=db.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()
 tables['notes']=db.execute('SELECT id,book_id,import_id,source_type,original_text,first_comment,second_comment,status,raw_source,sort_order,start_line,end_line,manually_corrected,created_at,updated_at FROM notes ORDER BY id').fetchall()
 digest=hashlib.sha256(json.dumps(tables,ensure_ascii=False).encode()).hexdigest()
 files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and (p.name=='account.json' or p.parent.name in ('sources','images'))}
 print(json.dumps({'tables_digest':digest,'files':files}))'''


def run(old_root,new_root):
    env={k:v for k,v in os.environ.items() if k not in SELECTORS}
    def cmd(args,root=new_root,**kwargs):
        return subprocess.run(args,cwd=root,env=env,capture_output=True,check=True,**kwargs)
    if cmd([*DOCKER,'ps','-aq','--filter',f'label=com.docker.compose.project={PROJECT}']).stdout.strip():
        raise RuntimeError('Existing test project, refusing')
    if cmd([*DOCKER,'volume','ls','--filter',f'name=^{VOLUME}$','--format','{{.Name}}']).stdout.strip():
        raise RuntimeError('Existing test volume, refusing')
    with socket.socket() as s: s.bind(('127.0.0.1',18080))
    old_digest=cmd([sys.executable,'scripts/check_release.py','--digest'],root=old_root).stdout.decode().strip()
    new_digest=cmd([sys.executable,'scripts/check_release.py','--digest']).stdout.decode().strip()
    old_image='noteshub:v2-ocr-test-'+old_digest[:12]
    new_image='noteshub:v2-ocr-test-'+new_digest[:12]
    clone_volumes=[]
    with tempfile.TemporaryDirectory(prefix='noteshub-archive-upgrade-') as tmp:
        tmp=Path(tmp)
        legacy_v3(tmp/'data')
        credentials=tmp/'credentials.json'
        credentials.write_text(json.dumps({'SecretId':'fixture-only-id','SecretKey':'fixture-only-key'}))
        credentials.chmod(0o400)
        env.update({'NOTESHUB_TEST_CREDENTIALS_PATH':str(credentials),'NOTESHUB_TEST_PORT':'18080',
                    'NOTESHUB_TEST_BACKUP_DIR':str(tmp/'backups')})
        def helper(action,old=False): return cmd(['bash','scripts/ocr_test.sh',action],root=old_root if old else new_root)
        def snapshot(image,volume=VOLUME):
            return json.loads(cmd([*DOCKER,'run','--rm','--network','none','--read-only','--mount',f'type=volume,src={volume},dst=/data',image,'python','-c',SNAPSHOT]).stdout)
        def version(image,volume=VOLUME):
            return int(cmd([*DOCKER,'run','--rm','--network','none','--read-only','--mount',f'type=volume,src={volume},dst=/data',image,'python','-c',"import sqlite3; print(sqlite3.connect('/data/noteshub.sqlite3').execute('PRAGMA user_version').fetchone()[0])"]).stdout)
        def restore_clone(archive,image):
            name=PROJECT+'-restorecheck-'+secrets.token_hex(6)
            cmd([*DOCKER,'volume','create','--label','org.noteshub.test=archive-upgrade-check',name])
            clone_volumes.append(name)
            cmd([*DOCKER,'run','--rm','-i','--network','none','--read-only','--mount',f'type=volume,src={name},dst=/data',image,'python','-m','noteshub.maintenance','restore'],input=Path(archive).read_bytes())
            return name
        def get_backup():
            output=helper('backup').stdout.decode()
            match=re.search(r'^BACKUP=(.+)$',output,re.MULTILINE)
            assert match,output
            path=Path(match[1])
            assert path.read_bytes().startswith(b'\x1f\x8b')
            return path
        try:
            cmd([*DOCKER,'volume','create','--label','org.noteshub.test=ocr-v2','--label',f'com.docker.compose.project={PROJECT}','--label','com.docker.compose.volume=data',VOLUME])
            cmd([*DOCKER,'run','--rm','--network','none','--user','0','--mount',f'type=bind,src={tmp},dst=/fixture','--mount',f'type=volume,src={VOLUME},dst=/data',old_image,'python','-c',
                 "import os,shutil; shutil.copytree('/fixture/data','/data',dirs_exist_ok=True); os.chown('/fixture/credentials.json',10001,10001); os.chown('/data',10001,10001); [(os.chown(os.path.join(root,name),10001,10001)) for root,dirs,files in os.walk('/data') for name in dirs+files]"])
            helper('up',old=True)
            base='http://127.0.0.1:18080'
            with httpx.Client(base_url=base,trust_env=False,follow_redirects=False,timeout=20) as c:
                def ready():
                    for _ in range(100):
                        try:
                            response=c.get('/login')
                            if response.status_code in (200,303): return
                        except httpx.HTTPError: pass
                        time.sleep(.1)
                    raise RuntimeError('Readiness timeout')
                def token(response): return re.search(r'name="csrf" value="([^"]+)"',response.text)[1]
                ready()
                self_check=c.post('/login',data={'csrf':token(c.get('/login')),'username':'reader','password':PASSWORD})
                assert self_check.status_code==303
                api='/api/books/book/image-drafts/draft'
                old_state=c.get(api).json()
                assert len(old_state['original_text'])==137 and len(old_state['first_comment'])==17
                helper('stop',old=True)
                assert version(old_image)==3
                before=snapshot(old_image)
                backup=get_backup()
                assert version(old_image)==3,'Backup migrated source prematurely'
                assert snapshot(old_image)==before
                clone=restore_clone(backup,old_image)
                assert version(old_image,clone)==3
                assert snapshot(old_image,clone)==before
                helper('up'); ready()
                assert version(new_image)==4
                assert snapshot(new_image)==before,'v3 to v4 changed saved data'
                check=helper('check').stdout.decode()
                assert 'OCR enabled/configured: False' in check
                current=c.get(api).json()
                assert not current['archived']
                assert current['first_ocr_text']==old_state['first_ocr_text']
                csrf=token(c.get('/books/book'))
                archived=c.post(api+'/archive',data={'csrf':csrf,'revision':str(current['revision'])})
                assert archived.status_code==200,archived.text
                final=archived.json()
                assert final['archived'] and final['note_status']=='pending'
                assert final['original_text']==old_state['original_text'] and final['first_comment']==old_state['first_comment']
                assert c.post(api+'/archive',data={'csrf':csrf,'revision':str(current['revision'])}).json()['note_id']==final['note_id']
                exported=c.get('/books/book/export').content
                assert ('原'*137).encode() in exported and ('评'*17).encode() in exported
                summary=helper('summary').stdout.decode()
                assert 'notes 2' in summary and 'ocr_runs 1' in summary and 'archived_image_notes 1' in summary
                print(summary.strip())
                helper('stop')
                after=snapshot(new_image)
                archived_backup=get_backup()
                clone=restore_clone(archived_backup,new_image)
                assert version(new_image,clone)==4
                assert snapshot(new_image,clone)==after
                helper('up'); ready()
                assert c.get('/books/book/export').content==exported
                assert c.get(api).json()['archived']
                assert c.get('/books/book/image-drafts/draft/file').status_code==200
                print('PASS: actual old v3 container/volume, pre-upgrade verified backup without migration, v3 clone restore, v4 migration with account/image/history/137+17 text unchanged, idempotent archive, export, v4 backup/restore and restart. Zero new OCR jobs or real calls.')
        except subprocess.CalledProcessError as error:
            print(error.stderr.decode(errors='replace')[-2000:]); raise
        finally:
            helper('down')
            for name in clone_volumes:
                label=cmd([*DOCKER,'volume','inspect','--format','{{index .Labels "org.noteshub.test"}}',name]).stdout.strip()
                if label==b'archive-upgrade-check': cmd([*DOCKER,'volume','rm',name])
            if subprocess.run([*DOCKER,'volume','inspect',VOLUME],env=env,capture_output=True).returncode==0:
                label=cmd([*DOCKER,'volume','inspect','--format','{{index .Labels "org.noteshub.test"}}',VOLUME]).stdout.strip()
                if label==b'ocr-v2': cmd([*DOCKER,'volume','rm',VOLUME])


if __name__=='__main__': run(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve())
