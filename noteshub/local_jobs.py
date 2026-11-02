"""Leased local-CPU OCR tasks. The server never imports an inference engine."""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import Response

from noteshub.paper import identifier, state, text

LEASE_SECONDS = 1800
RETENTION_SECONDS = 30 * 24 * 3600


def job_state(row):
    return {key:row[key] for key in ('id','note_id','revision','state','created_at','finished_at','engine','error')} | {
        'photos':json.loads(row['photos_json']), 'results':json.loads(row['results_json'])}


class LocalJobs:
    def __init__(self, store):
        self.store = store

    def queue(self, note_id, payload):
        job_id = identifier(payload.get('job_id'))
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            note = state(db, identifier(note_id))
            existing = db.execute('SELECT * FROM paper_ocr_jobs WHERE id=?', (job_id,)).fetchone()
            if existing:
                if existing['note_id'] != note_id or existing['revision'] != payload.get('revision'):
                    raise HTTPException(409, '任务编号已被使用。')
                return job_state(existing)
            if type(payload.get('revision')) is not int or note['revision'] != payload['revision']:
                raise HTTPException(409, '请先同步最新记录，再提交识别任务。')
            if not note['photos'] or any(photo['purged_at'] is not None for photo in note['photos']):
                raise HTTPException(422, '请选择至少一张尚未清理且已同步的图片。')
            active = db.execute("SELECT * FROM paper_ocr_jobs WHERE note_id=? AND state IN ('pending','running')", (note_id,)).fetchone()
            if active:
                return job_state(active)
            photos=[{key:p[key] for key in ('id','sha256','media_type','byte_size')} for p in note['photos']]
            db.execute("INSERT INTO paper_ocr_jobs(id,note_id,revision,photos_json,state,created_at) VALUES (?,?,?,?,'pending',?)",
                       (job_id,note_id,note['revision'],json.dumps(photos),int(time.time())))
            db.execute('UPDATE paper_entries SET confirmed_revision=NULL,confirmed_at=NULL WHERE note_id=?', (note_id,))
            db.execute('UPDATE paper_photos SET purge_after=NULL WHERE note_id=? AND purged_at IS NULL', (note_id,))
            return job_state(db.execute('SELECT * FROM paper_ocr_jobs WHERE id=?', (job_id,)).fetchone())

    def batch(self):
        with self.store.connection() as db:
            done={}
            for row in db.execute("SELECT note_id,photos_json FROM paper_ocr_jobs WHERE state IN ('pending','running','succeeded')"):
                done.setdefault(row['note_id'],[]).append([p['id'] for p in json.loads(row['photos_json'])])
            notes=[];skipped=0
            for row in db.execute('SELECT note_id FROM paper_entries ORDER BY rowid DESC'):
                note=state(db,row[0]);ids=[p['id'] for p in note['photos']]
                if not ids or any(p['purged_at'] is not None for p in note['photos']) or ids in done.get(note['id'],[]):
                    skipped+=1;continue
                notes.append(note)
                if len(notes)>=200:break
        queued=0
        for note in notes:
            ids=[p['id'] for p in note['photos']]
            if not ids or any(p['purged_at'] is not None for p in note['photos']) or ids in done.get(note['id'],[]):
                skipped+=1;continue
            try:
                self.queue(note['id'],{'job_id':uuid.uuid4().hex,'revision':note['revision']});queued+=1
            except HTTPException as exc:
                if exc.status_code not in (409,422):raise
                skipped+=1
        return {'queued':queued,'skipped':skipped,'limit':200}

    def claim(self, worker, raw_token, claim_id):
        identifier(claim_id)
        now=int(time.time())
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            receipt=db.execute('SELECT * FROM paper_worker_claims WHERE id=?', (claim_id,)).fetchone()
            if receipt:
                if receipt['worker_id'] != worker:
                    raise HTTPException(409,'领取编号已被使用。')
                row=db.execute('SELECT * FROM paper_ocr_jobs WHERE id=?', (receipt['job_id'],)).fetchone()
                if not row or row['state'] != 'running' or row['lease_hash'] != receipt['lease_hash'] or row['lease_until'] <= now:
                    return {'job':None}
            else:
                row=db.execute("SELECT * FROM paper_ocr_jobs WHERE state='pending' OR (state='running' AND lease_until<=?) ORDER BY created_at,rowid LIMIT 1",(now,)).fetchone()
                if row:
                    lease=hmac.new(raw_token.encode(),(row['id']+claim_id).encode(),hashlib.sha256).hexdigest()
                    digest=hashlib.sha256(lease.encode()).hexdigest()
                    db.execute("UPDATE paper_ocr_jobs SET state='running',worker_id=?,lease_hash=?,lease_until=? WHERE id=?",(worker,digest,now+LEASE_SECONDS,row['id']))
                    db.execute('INSERT INTO paper_worker_claims VALUES (?,?,?,?,?)',(claim_id,worker,row['id'],digest,now))
                    row=db.execute('SELECT * FROM paper_ocr_jobs WHERE id=?',(row['id'],)).fetchone()
                else:
                    db.execute('INSERT INTO paper_worker_claims VALUES (?,?,NULL,NULL,?)',(claim_id,worker,now))
                    return {'job':None}
            lease=hmac.new(raw_token.encode(),(row['id']+claim_id).encode(),hashlib.sha256).hexdigest()
            return {'job':job_state(row),'lease':lease,'lease_seconds':LEASE_SECONDS}

    @staticmethod
    def owned(db, worker, job_id, lease, *, completed=False):
        row=db.execute('SELECT * FROM paper_ocr_jobs WHERE id=?',(identifier(job_id),)).fetchone()
        digest=hashlib.sha256(lease.encode()).hexdigest() if isinstance(lease,str) else ''
        if (not row or row['worker_id'] != worker or not hmac.compare_digest(row['lease_hash'] or '',digest)
                or (row['state'] != 'running' and not (completed and row['state'] in ('succeeded','failed')))
                or (row['state']=='running' and row['lease_until']<=int(time.time()))):
            raise HTTPException(409,'任务租约无效或已过期，请重新领取。')
        return row

    def finish(self, worker, job_id, payload):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self.owned(db,worker,job_id,payload.get('lease'),completed=True)
            engine=text(payload.get('engine',''))[:100]
            results=payload.get('results')
            failed=payload.get('failed',False)
            if type(failed) is not bool or not isinstance(results,list):
                raise HTTPException(422,'识别结果格式无效。')
            photos=json.loads(row['photos_json'])
            if not failed and ([v.get('photo_id') if isinstance(v,dict) else None for v in results] != [v['id'] for v in photos]):
                raise HTTPException(422,'识别结果必须按原图片顺序逐张回传。')
            if failed:
                results=[]
            else:
                results=[{'photo_id':identifier(v['photo_id']),'text':text(v.get('text'))} for v in results]
                if sum(len(v['text']) for v in results)>50000:
                    raise HTTPException(422,'汇总识别结果超过 50000 字符，请分条处理。')
            encoded=json.dumps(results,ensure_ascii=False)
            target='failed' if failed else 'succeeded'
            if row['state'] in ('succeeded','failed'):
                if row['state'] != target or row['results_json'] != encoded or row['engine'] != engine:
                    raise HTTPException(409,'已完成结果不可覆盖，请创建新任务。')
                return {'state':target}
            db.execute('UPDATE paper_ocr_jobs SET state=?,finished_at=?,engine=?,results_json=?,error=? WHERE id=?',
                       (target,int(time.time()),engine,encoded,'本地识别失败，可检查电脑后手动重试。' if failed else '',job_id))
            return {'state':target}

    def heartbeat(self, worker, job_id, lease):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');self.owned(db,worker,job_id,lease)
            db.execute('UPDATE paper_ocr_jobs SET lease_until=? WHERE id=?',(int(time.time())+LEASE_SECONDS,job_id))
        return {'renewed':True}

    def image(self, worker, job_id, photo_id, lease):
        with self.store.connection() as db:
            db.execute('BEGIN')
            job=self.owned(db,worker,job_id,lease)
            if not any(p['id']==photo_id for p in json.loads(job['photos_json'])):
                raise HTTPException(404,'图片不属于此任务。')
            row=db.execute('SELECT * FROM paper_photos WHERE id=? AND note_id=? AND purged_at IS NULL',(identifier(photo_id),job['note_id'])).fetchone()
            if not row:raise HTTPException(404,'来源图片不存在。')
            raw=(self.store.root/row['file_path']).read_bytes()
            if len(raw)!=row['byte_size'] or hashlib.sha256(raw).hexdigest()!=row['sha256']:
                raise HTTPException(503,'来源图片校验失败。')
            return raw,row['media_type']

    def confirm(self, note_id, payload, undo=False):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE');note=state(db,identifier(note_id))
            if type(payload.get('revision')) is not int or note['revision']!=payload['revision']:
                raise HTTPException(409,'请先同步最新文字，确认后再安排图片清理。')
            if undo:
                if any(p['purged_at'] for p in note['photos']):
                    raise HTTPException(409,'部分图片已清理；可从清理前完整备份恢复到独立空卷。')
                db.execute('UPDATE paper_entries SET confirmed_revision=NULL,confirmed_at=NULL WHERE note_id=?',(note_id,))
                db.execute('UPDATE paper_photos SET purge_after=NULL WHERE note_id=?',(note_id,))
            else:
                if not note['original_text'].strip() or not note['photos']:
                    raise HTTPException(422,'请先完成识别并人工校对非空原文。')
                if db.execute("SELECT 1 FROM paper_ocr_jobs WHERE note_id=? AND state IN ('pending','running')",(note_id,)).fetchone():
                    raise HTTPException(409,'还有待处理的识别任务，请等完成后再确认原文。')
                jobs=db.execute("SELECT photos_json FROM paper_ocr_jobs WHERE note_id=? AND state='succeeded'",(note_id,)).fetchall()
                ids=[p['id'] for p in note['photos']]
                if not any([p['id'] for p in json.loads(j[0])]==ids for j in jobs):
                    raise HTTPException(422,'当前图片和顺序尚未完成本地识别，不能自动清理。')
                if any(p['purged_at'] for p in note['photos']):
                    raise HTTPException(409,'这些图片已经清理。')
                now=int(time.time())
                # Repeated confirmation cannot silently shorten the recovery period.
                db.execute('UPDATE paper_entries SET confirmed_revision=revision,confirmed_at=? WHERE note_id=?',(now,note_id))
                db.execute('UPDATE paper_photos SET purge_after=? WHERE id IN (SELECT photo_id FROM paper_pages WHERE note_id=?)',(now+RETENTION_SECONDS,note_id))
            return state(db,note_id)


def cleanup_photos(store):
    """Commit tombstones before unlinking; crash retries never lose live images."""
    now=int(time.time())
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE paper_photos SET purged_at=? WHERE purged_at IS NULL AND purge_after<=? "
                   "AND EXISTS (SELECT 1 FROM paper_entries p WHERE p.note_id=paper_photos.note_id AND p.confirmed_revision=p.revision) "
                   "AND EXISTS (SELECT 1 FROM paper_pages p WHERE p.photo_id=paper_photos.id) "
                   "AND NOT EXISTS (SELECT 1 FROM paper_ocr_jobs j WHERE j.note_id=paper_photos.note_id AND j.state IN ('pending','running'))",(now,now))
        paths=[row[0] for row in db.execute('SELECT file_path FROM paper_photos WHERE purged_at IS NOT NULL')]
    for path in paths:
        if re.fullmatch(r'paper-images/[0-9a-f]{32}\.(png|jpg)',path):
            (store.root/path).unlink(missing_ok=True)


def install_local_jobs(app, jobs, *, require_session, body, operation):
    store=jobs.store

    async def worker(request):
        authorization=request.headers.get('authorization','')
        raw=authorization.removeprefix('Bearer ')
        if not authorization.startswith('Bearer ') or not re.fullmatch(r'nh_[A-Za-z0-9_-]{40,100}',raw):
            raise HTTPException(401,'需要专用本地 OCR 凭证。')
        with store.connection() as db:
            row=db.execute('SELECT id FROM paper_worker_tokens WHERE token_hash=? AND revoked_at IS NULL',(hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
            if not row:raise HTTPException(401,'本地 OCR 凭证无效或已撤销。')
            db.execute('UPDATE paper_worker_tokens SET last_used_at=? WHERE id=?',(int(time.time()),row['id']))
        return row['id'],raw

    async def worker_body(request):
        try:payload=await request.json()
        except (ValueError,UnicodeDecodeError):raise HTTPException(422,'任务请求格式无效。')
        if not isinstance(payload,dict):raise HTTPException(422,'任务请求格式无效。')
        return payload

    @app.get('/api/paper/worker-tokens')
    async def tokens(request: Request):
        require_session(request)
        with store.connection() as db:
            return {'tokens':[dict(r) for r in db.execute('SELECT id,label,created_at,last_used_at,revoked_at FROM paper_worker_tokens ORDER BY created_at DESC,rowid DESC')]}

    @app.post('/api/paper/worker-tokens')
    async def token(request: Request):
        payload=await body(request);label=text(payload.get('label','Windows 本地 OCR'))[:100]
        raw='nh_'+secrets.token_urlsafe(40);token_id=uuid.uuid4().hex
        with store.connection() as db:
            db.execute('INSERT INTO paper_worker_tokens(id,token_hash,label,created_at) VALUES (?,?,?,?)',
                       (token_id,hashlib.sha256(raw.encode()).hexdigest(),label,int(time.time())))
        return {'id':token_id,'token':raw}

    @app.post('/api/paper/worker-tokens/{token_id}/revoke')
    async def revoke(request: Request,token_id: str):
        await body(request)
        with store.connection() as db:
            db.execute('UPDATE paper_worker_tokens SET revoked_at=? WHERE id=?',(int(time.time()),identifier(token_id)))
            db.execute("UPDATE paper_ocr_jobs SET state='pending',worker_id=NULL,lease_hash=NULL,lease_until=NULL WHERE worker_id=? AND state='running'",(token_id,))
        return {'revoked':True}

    @app.get('/api/paper/notes/{note_id}/ocr')
    async def history(request: Request,note_id: str):
        require_session(request)
        with store.connection() as db:
            state(db,identifier(note_id))
            return {'jobs':[job_state(r) for r in db.execute('SELECT * FROM paper_ocr_jobs WHERE note_id=? ORDER BY created_at DESC,rowid DESC',(note_id,))]}

    @app.post('/api/paper/notes/{note_id}/ocr')
    async def queue(request: Request,note_id: str):
        return await operation(jobs.queue,note_id,await body(request))

    @app.post('/api/paper/ocr-batch')
    async def batch(request: Request):
        await body(request)
        return await operation(jobs.batch)

    @app.post('/api/paper/notes/{note_id}/confirm')
    async def confirm(request: Request,note_id: str):
        return await operation(jobs.confirm,note_id,await body(request))

    @app.post('/api/paper/notes/{note_id}/undo-cleanup')
    async def undo(request: Request,note_id: str):
        return await operation(jobs.confirm,note_id,await body(request),True)

    @app.post('/api/local-ocr/claim')
    async def claim(request: Request):
        worker_id,raw=await worker(request);payload=await worker_body(request)
        return await operation(jobs.claim,worker_id,raw,payload.get('claim_id'))

    @app.post('/api/local-ocr/jobs/{job_id}/heartbeat')
    async def heartbeat(request: Request,job_id: str):
        worker_id,_=await worker(request);payload=await worker_body(request)
        return await operation(jobs.heartbeat,worker_id,job_id,payload.get('lease'))

    @app.post('/api/local-ocr/jobs/{job_id}/result')
    async def finish(request: Request,job_id: str):
        worker_id,_=await worker(request)
        return await operation(jobs.finish,worker_id,job_id,await worker_body(request))

    @app.get('/api/local-ocr/jobs/{job_id}/photos/{photo_id}')
    async def image(request: Request,job_id: str,photo_id: str):
        worker_id,_=await worker(request)
        raw,media=await operation(jobs.image,worker_id,job_id,photo_id,request.headers.get('x-ocr-lease'))
        return Response(raw,media_type=media)
