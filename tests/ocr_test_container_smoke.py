"""Validate the extracted release on the local daemon; zero real OCR calls.

Usage: .venv/bin/python tests/ocr_test_container_smoke.py /tmp/.../release
Refuses any pre-existing fixed test project/volume. Uses only fake credentials.
"""
import io
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
from PIL import Image

PROJECT='noteshub-v2-ocr-test'
VOLUME=PROJECT+'_data'
SELECTORS=('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')
DOCKER=['docker','--host=unix:///var/run/docker.sock']


def run(root):
    env={key:value for key,value in os.environ.items() if key not in SELECTORS}
    def cmd(args,*,environment=env,**kwargs):
        return subprocess.run(args,cwd=root,env=environment,capture_output=True,check=True,**kwargs)
    if cmd([*DOCKER,'ps','-aq','--filter',f'label=com.docker.compose.project={PROJECT}']).stdout.strip():
        raise RuntimeError('Existing test project; refusing to operate')
    if cmd([*DOCKER,'volume','ls','--filter',f'name=^{VOLUME}$','--format','{{.Name}}']).stdout.strip():
        raise RuntimeError('Existing test volume; refusing to operate')
    with socket.socket() as s:
        s.bind(('127.0.0.1',18080))
    digest=cmd([sys.executable,'scripts/check_release.py','--digest']).stdout.decode().strip()
    image='noteshub:v2-ocr-test-'+digest[:12]
    temporary_volume='noteshub-ocr-restore-check-'+secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='noteshub-ocr-fixture-') as tmp:
        tmp=Path(tmp)
        credentials=tmp/'credentials.json'
        credentials.write_text(json.dumps({'SecretId':'fixture-only-id','SecretKey':'fixture-only-key'}))
        credentials.chmod(0o400)
        env.update({'NOTESHUB_TEST_CREDENTIALS_PATH':str(credentials),'NOTESHUB_TEST_PORT':'18080',
                    'NOTESHUB_TEST_IMAGE_TAG':image.split(':')[1], 'NOTESHUB_TEST_OCR_ENABLED':'false',
                    'NOTESHUB_TEST_PASSWORD':secrets.token_urlsafe(24)})
        compose=[*DOCKER,'compose','--env-file','/dev/null','-p',PROJECT,'-f','docker-compose.ocr-test.yml']
        helper=['bash','scripts/ocr_test.sh']
        try:
            cmd([*DOCKER,'run','--rm','--network','none','--user','0','--mount',f'type=bind,src={tmp},dst=/fixture',image,
                 'python','-c',"import os; os.chown('/fixture/credentials.json',10001,10001)"])
            cmd([*helper,'preflight'])
            cmd([*compose,'run','--rm','--no-deps','-T','-e','NOTESHUB_TEST_PASSWORD','app','python','-c',
                 "import os; from pathlib import Path; from noteshub.auth import provision; provision(Path('/data'),'container-reader',os.environ['NOTESHUB_TEST_PASSWORD'])"])
            cmd([*helper,'up'])
            container=cmd([*compose,'ps','-q','app']).stdout.decode().strip()
            host=json.loads(cmd([*DOCKER,'inspect','--format','{{json .HostConfig}}',container]).stdout)
            assert host['ReadonlyRootfs']
            assert host['Memory']==512*1024**2
            assert host['NanoCpus']==500000000
            assert host['PidsLimit']==128
            assert host['PortBindings']['8000/tcp']==[{'HostIp':'127.0.0.1','HostPort':'18080'}]
            assert cmd([*compose,'exec','-T','app','id','-u']).stdout.strip()==b'10001'
            mounts=json.loads(cmd([*DOCKER,'inspect','--format','{{json .Mounts}}',container]).stdout)
            assert next(m for m in mounts if m['Destination']=='/data')['Name']==VOLUME
            url='http://127.0.0.1:18080'
            with httpx.Client(base_url=url,trust_env=False,timeout=15,follow_redirects=False) as c:
                for _ in range(80):
                    try:
                        response=c.get('/login')
                        if response.status_code==200: break
                    except httpx.HTTPError: pass
                    time.sleep(.1)
                else: raise RuntimeError('Readiness timeout')
                assert b'csrf' in response.content
                check=cmd([*helper,'check']).stdout.decode()
                assert 'OCR enabled/configured: False' in check
                def token(page): return re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
                def login():
                    assert c.post('/login',data={'csrf':token(c.get('/login')),'username':'container-reader','password':env['NOTESHUB_TEST_PASSWORD']}).status_code==303
                login()
                csrf=token(c.get('/'))
                book=c.post('/books',data={'csrf':csrf,'title':'Isolated container check'}).headers['location']
                raw=io.BytesIO()
                Image.new('RGB',(4000,3000),'white').save(raw,format='PNG')
                saved=c.post('/api'+book+'/image-drafts',data={'csrf':csrf},files={'file':('synthetic.png',raw.getvalue(),'image/png')})
                assert saved.status_code==201,saved.status_code
                draft=saved.json()['id']
                api='/api'+book+'/image-drafts/'+draft
                assert c.post(api+'/open',data={'csrf':csrf}).status_code==200
                assert c.post(api+'/save',data={'csrf':csrf,'revision':'0','original_text':'Synthetic original','first_comment':'Synthetic first reading'}).status_code==200
                assert c.get(api).json()['first_comment']=='Synthetic first reading'
                c.post('/logout',data={'csrf':csrf})
                login()
                assert c.get(api).json()['original_text']=='Synthetic original'
                cmd([*helper,'enable-ocr'])  # No request is submitted.
                for _ in range(80):
                    try:
                        if c.get(api).status_code==200: break
                    except httpx.HTTPError: pass
                    time.sleep(.1)
                check=cmd([*helper,'check']).stdout.decode()
                assert 'OCR enabled/configured: True' in check
                assert c.get(api).json()['ocr_state']=='idle'
                cmd([*helper,'disable-ocr'])
                for _ in range(80):
                    try:
                        state=c.get(api)
                        if state.status_code==200: break
                    except httpx.HTTPError: pass
                    time.sleep(.1)
                assert state.json()['first_comment']=='Synthetic first reading'
                summary=cmd([*helper,'summary']).stdout.decode()
                assert 'ocr_runs 0' in summary
                assert 'notes 0' in summary
                print(summary.strip())
                print(cmd([*DOCKER,'stats','--no-stream','--format','{{.Name}} CPU={{.CPUPerc}} MEM={{.MemUsage}}',cmd([*compose,'ps','-q','app']).stdout.decode().strip()]).stdout.decode().strip())
                cmd([*helper,'stop'])
                backup=cmd([*compose,'run','--rm','--no-deps','-T','app','python','-m','noteshub.maintenance','backup']).stdout
                assert backup.startswith(b'\x1f\x8b'),'Backup command did not produce gzip'
                cmd([*DOCKER,'volume','create','--label','org.noteshub.test=ocr-restore-smoke',temporary_volume])
                cmd([*DOCKER,'run','--rm','-i','--network','none','--read-only','--mount',f'type=volume,src={temporary_volume},dst=/data',image,'python','-m','noteshub.maintenance','restore'],input=backup)
                cmd([*DOCKER,'run','--rm','--network','none','--read-only','--mount',f'type=volume,src={temporary_volume},dst=/data',image,'python','-c',
                     "import sqlite3; from pathlib import Path; db=sqlite3.connect('/data/noteshub.sqlite3'); row=db.execute('SELECT first_comment,file_path FROM image_drafts').fetchone(); assert row[0]=='Synthetic first reading'; assert (Path('/data')/row[1]).is_file(); assert db.execute('SELECT COUNT(*) FROM ocr_runs').fetchone()[0]==0; print('Container backup/restore OK, zero OCR jobs')"])
                cmd([*helper,'up'])
                for _ in range(80):
                    try:
                        response=c.get(api)
                        if response.status_code==200: break
                    except httpx.HTTPError: pass
                    time.sleep(.1)
                assert response.json()['first_comment']=='Synthetic first reading'
            print('PASS: verified release image, isolated labels/volume/loopback port, 512 MiB/0.5 CPU/read-only/non-root limits, private fake credential mount, 12MP image, save/relogin/recreate/restart, container backup/restore, zero real OCR calls.')
        except subprocess.CalledProcessError as error:
            print(error.stderr.decode(errors='replace')[-2000:])
            raise
        finally:
            # Preflight proved these names absent; delete only resources created
            # by this synthetic smoke test, checking marker before volume rm.
            cmd([*helper,'down'])
            if subprocess.run([*DOCKER,'volume','inspect',temporary_volume],env=env,capture_output=True).returncode==0:
                marker=cmd([*DOCKER,'volume','inspect','--format','{{index .Labels "org.noteshub.test"}}',temporary_volume]).stdout.strip()
                if marker==b'ocr-restore-smoke': cmd([*DOCKER,'volume','rm',temporary_volume])
            if subprocess.run([*DOCKER,'volume','inspect',VOLUME],env=env,capture_output=True).returncode==0:
                marker=cmd([*DOCKER,'volume','inspect','--format','{{index .Labels "org.noteshub.test"}}',VOLUME]).stdout.strip()
                if marker==b'ocr-v2': cmd([*DOCKER,'volume','rm',VOLUME])


if __name__=='__main__':
    run(Path(sys.argv[1]).resolve())
