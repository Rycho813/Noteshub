"""On-demand outbound-only CPU OCR worker. No web server or paid OCR SDK.

Python 3.12 x64; install this directory's requirements in a separate environment.
The server's requirements intentionally exclude all inference dependencies.
"""
import argparse
from contextlib import contextmanager
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class WorkerError(Exception):
    pass


class LeaseExpired(WorkerError):
    pass


class InferenceFailed(WorkerError):
    pass


def validate_url(value):
    url=urllib.parse.urlsplit(value)
    if (url.scheme not in ('https','http') or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ('','/')
            or (url.scheme=='http' and url.hostname not in ('127.0.0.1','localhost','::1'))):
        raise WorkerError('服务器地址必须是 HTTPS 根地址；仅本机测试允许 localhost HTTP。')
    return value.rstrip('/')


def clipboard_token():
    """Explicit Windows-only input; never echo clipboard contents or child errors."""
    if sys.platform != 'win32':
        raise WorkerError('--clipboard 仅支持 Windows，请使用交互输入。')
    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-STA', '-Command',
             '[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); Get-Clipboard -Raw'],
            capture_output=True, encoding='utf-8', timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise WorkerError('无法读取 Windows 剪贴板，请重新复制凭证或使用交互输入。') from None
    if result.returncode != 0:
        raise WorkerError('无法读取 Windows 剪贴板，请重新复制凭证或使用交互输入。')
    return result.stdout.strip().lstrip('\ufeff')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise WorkerError('服务器返回重定向，已停止以保护电脑专用凭证；请检查 HTTPS 地址。')


class Client:
    def __init__(self, url, token):
        self.url=validate_url(url)
        if not isinstance(token,str) or not re.fullmatch(r'nh_[A-Za-z0-9_-]{40,100}',token):
            raise WorkerError('电脑专用凭证格式无效：请复制以 nh_ 开头的完整凭证；终端粘贴无效时可使用 configure --clipboard。')
        self.token=token
        handlers=[NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if urllib.parse.urlsplit(self.url).hostname in ('localhost','127.0.0.1','::1'):
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener=urllib.request.build_opener(*handlers)

    def call(self, path, payload=None, lease=None, binary=False):
        if not path.startswith('/api/local-ocr/'):
            raise WorkerError('本地 OCR 凭证仅用于任务 API。')
        headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'}
        if lease:headers['X-OCR-Lease']=lease
        raw=None
        if payload is not None:
            headers['Content-Type']='application/json';raw=json.dumps(payload,ensure_ascii=False).encode()
        for attempt in range(3):
            try:
                req=urllib.request.Request(self.url+path,data=raw,headers=headers)
                with self.opener.open(req,timeout=30) as response:
                    data=response.read(5*1024*1024+1)
                    if len(data)>5*1024*1024:raise WorkerError('任务响应超过大小限制。')
                    return data if binary else json.loads(data)
            except urllib.error.HTTPError as exc:
                if exc.code in (401,403):raise WorkerError('电脑凭证已失效或被撤销，请重新配置。') from None
                if exc.code==409:raise LeaseExpired('任务租约已过期或结果已完成；本机结果保留在 expired 目录。') from None
                if exc.code<500:raise WorkerError(f'任务请求被拒绝（HTTP {exc.code}），未修改云端笔记。') from None
            except (urllib.error.URLError,TimeoutError,ConnectionError):
                pass
            if attempt<2:time.sleep(attempt+1)
        raise WorkerError('无法连接服务器，已保留本机任务回执，下次运行可继续。')


def private_write(path, value, *, exclusive=False):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    raw=json.dumps(value,ensure_ascii=False,indent=2).encode()
    if exclusive:
        with path.open('xb') as target:
            os.chmod(path,0o600);target.write(raw);target.flush();os.fsync(target.fileno())
        return
    fd,name=tempfile.mkstemp(prefix='.pending-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as target:
            os.chmod(name,0o600);target.write(raw);target.flush();os.fsync(target.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def create_engine(device='cpu'):
    try:
        import onnxruntime as ort
        ort.disable_telemetry_events()
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        raise WorkerError('请先在电脑的独立虚拟环境安装 tools/local_ocr/requirements.txt。') from None
    if device=='cuda' and 'CUDAExecutionProvider' not in ort.get_available_providers():
        raise WorkerError('当前 OCR 环境没有可用 CUDA provider。请使用 --device cpu；GPU 环境需按显卡/驱动另行配置。')
    engine=RapidOCR(intra_op_num_threads=2,inter_op_num_threads=1,
                    det_use_cuda=device=='cuda',cls_use_cuda=device=='cuda',rec_use_cuda=device=='cuda')
    def recognize(image):
        rows,_=engine(image)
        value='\n'.join(str(row[1]) for row in (rows or []))
        if len(value)>50000:raise WorkerError('单页识别结果超过 50000 字符，请分条处理。')
        return value
    return recognize


@contextmanager
def worker_lock(directory):
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (directory/'run.lock').open('a+b') as lock:
        lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WorkerError('这份配置已有 OCR 程序在运行，请等待它结束。') from None
        try:yield
        finally:
            if os.name=='nt':
                lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(lock,fcntl.LOCK_UN)


def process(client, task, recognize, outbox):
    job=task['job'];lease=task['lease'];job_id=job['id']
    prefix='/api/local-ocr/jobs/'+job_id
    stop=threading.Event();lost=[]
    def heartbeat():
        while not stop.wait(30):
            try:client.call(prefix+'/heartbeat',{'lease':lease})
            except WorkerError as exc:lost.append(exc);return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    results=[]
    try:
        for photo in job['photos']:
            if lost:raise lost[0]
            raw=client.call(prefix+'/photos/'+photo['id'],lease=lease,binary=True)
            if len(raw)!=photo['byte_size'] or hashlib.sha256(raw).hexdigest()!=photo['sha256']:
                raise WorkerError('下载图片摘要不一致，未提交识别结果。')
            try:recognized=recognize(raw)
            except Exception:
                failed={'lease':lease,'engine':'RapidOCR 1.4.4 / PaddleOCR ONNX','results':[],'failed':True}
                receipt=outbox/(job_id+'.json');private_write(receipt,failed)
                client.call(prefix+'/result',failed);receipt.unlink()
                raise InferenceFailed('本地模型识别失败；草稿和图片保留，可检查电脑后重新排队。') from None
            results.append({'photo_id':photo['id'],'text':recognized})
        if sum(len(v['text']) for v in results)>50000:
            failed={'lease':lease,'engine':'RapidOCR 1.4.4 / PaddleOCR ONNX','results':[],'failed':True}
            receipt=outbox/(job_id+'.json');private_write(receipt,failed)
            client.call(prefix+'/result',failed);receipt.unlink()
            raise InferenceFailed('多图汇总超过 50000 字符，请拆分记录。')
        if lost:raise lost[0]
        payload={'lease':lease,'engine':'RapidOCR 1.4.4 / PaddleOCR ONNX','results':results,'failed':False}
        # A private durable outbox precedes the result POST. A lost POST reply
        # replays this exact result instead of creating another inference task.
        receipt=outbox/(job_id+'.json');private_write(receipt,payload)
        client.call(prefix+'/result',payload);receipt.unlink()
        return job_id
    finally:
        stop.set();thread.join(timeout=2)


def run_batch(client, directory, maximum, recognize):
    outbox=directory/'outbox';outbox.mkdir(parents=True,exist_ok=True,mode=0o700)
    count=0
    for receipt in sorted(outbox.glob('*.json')):
        job_id=receipt.stem
        if not re.fullmatch(r'[0-9a-f]{32}',job_id):raise WorkerError('本机回执文件名无效。')
        try:client.call('/api/local-ocr/jobs/'+job_id+'/result',json.loads(receipt.read_text()))
        except LeaseExpired:
            expired=directory/'expired';expired.mkdir(exist_ok=True,mode=0o700)
            receipt.replace(expired/receipt.name)
            (directory/'claim.json').unlink(missing_ok=True)
            print('过期结果已保留在本机 expired 目录，将重新领取任务。');continue
        except WorkerError as exc:
            # Do not discard recognized text if a lease expired while offline.
            raise WorkerError(str(exc)+' 可检查 '+str(receipt)) from None
        receipt.unlink();count+=1
    journal=directory/'claim.json'
    for _ in range(maximum):
        claim=json.loads(journal.read_text()) if journal.exists() else {'claim_id':uuid.uuid4().hex}
        private_write(journal,claim)
        task=client.call('/api/local-ocr/claim',claim)
        if not task['job']:
            journal.unlink(missing_ok=True);break
        try:job_id=process(client,task,recognize,outbox)
        except InferenceFailed:
            journal.unlink(missing_ok=True)
            print('本地识别失败：'+task['job']['id']+'；图片与笔记保留，可检查模型后重新排队。')
            continue
        journal.unlink(missing_ok=True);count+=1
        print('本地识别并回传完成：'+job_id)
    print(f'本次处理 {count} 条；可以关闭程序，不影响手机记录。')


def main(argv=None):
    parser=argparse.ArgumentParser(description='NotesHub 本地 OCR：手动批处理，所有连接由电脑发起')
    default=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'NotesHub'/'local-ocr.json'
    parser.add_argument('--config',type=Path,default=default)
    commands=parser.add_subparsers(dest='command',required=True)
    configure=commands.add_parser('configure',help='交互输入专用凭证，不放入命令历史')
    configure.add_argument('--url',required=True)
    configure.add_argument('--clipboard',action='store_true',help='从 Windows 剪贴板读取凭证，不显示内容')
    run=commands.add_parser('run',help='处理已排队任务后退出')
    run.add_argument('--max-jobs',type=int,default=20)
    run.add_argument('--device',choices=['cpu','cuda'],default='cpu')
    args=parser.parse_args(argv)
    try:
        if args.command=='configure':
            url=validate_url(args.url)
            token=clipboard_token() if args.clipboard else getpass.getpass('粘贴电脑专用凭证（不显示）：').strip()
            Client(url,token)
            private_write(args.config,{'url':url,'token':token},exclusive=True)
            print('本地配置已保存；现有配置不会被覆盖。')
        else:
            if not 1<=args.max_jobs<=200:raise WorkerError('--max-jobs 必须为 1–200。')
            config=json.loads(args.config.read_text())
            client=Client(config['url'],config['token'])
            # Initialize before claiming, so a missing model does not occupy a lease.
            recognize=create_engine(args.device)
            with worker_lock(args.config.parent):
                run_batch(client,args.config.parent,args.max_jobs,recognize)
    except FileExistsError:
        parser.exit(1,'配置已存在；轮换凭证请使用 --config 指定另一个私有文件，原配置保留。\n')
    except (WorkerError,OSError,ValueError,KeyError) as exc:
        parser.exit(1,'本地 OCR 未完成：'+str(exc)+'\n')
    except KeyboardInterrupt:
        parser.exit(130,'已停止；手机记录不受影响，未完成任务会在租约过期后可重新领取。\n')
    except Exception:
        parser.exit(1,'本地模型未能运行；请检查独立 OCR 环境与设备配置。手机记录和云端内容保持保留。\n')


if __name__=='__main__':main()
