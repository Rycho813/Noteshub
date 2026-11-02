/* Optional local OCR: candidates never replace a thought or corrected text. */
(() => {
  'use strict';
  const q=window.NotesHubQuick, $=id => document.getElementById(id);
  if (!q) return;
  const uuid=() => crypto.randomUUID().replaceAll('-','');
  let rendering=0;
  function button(label,fn) {
    const b=document.createElement('button'); b.type='button';b.className='secondary';b.textContent=label;
    b.addEventListener('click',() => q.safe(async () => { b.disabled=true;try { await fn(); } finally { b.disabled=false; } }));return b;
  }
  async function synced() {
    const id=q.getCurrent()?.id;
    await q.sync();const note=q.getCurrent();
    if (!navigator.onLine || note?.id!==id || !note.revision || note.dirty || note.conflict) throw new Error('请先将当前记录完整同步，再进行这项操作');
    return note;
  }
  async function render() {
    const generation=++rendering,note=q.getCurrent(), panel=$('quick-ocr-panel');
    if (!note) return;
    panel.hidden=false;panel.replaceChildren();
    const title=document.createElement('h2');title.textContent='本地识别与校对';panel.append(title);
    const hint=document.createElement('p');hint.className='muted';hint.textContent='原文可稍后识别。任务在自己的电脑运行，识别结果先保留为候选，不覆盖一读。';panel.append(hint);
    if (!navigator.onLine || !note.revision) { hint.textContent='照片和想法可先保存在本机；完整同步后可交给电脑识别。';return; }
    const queue=button('交给电脑识别',async () => {
      const current=await synced();
      await q.api(q.endpoint(current.id)+'/ocr',{job_id:uuid(),revision:current.revision});
      await render();
    }); queue.disabled=!note.photos.length;panel.append(queue);
    const refresh=button('刷新本地识别状态',render);panel.append(refresh);
    const response=await q.api(q.endpoint(note.id)+'/ocr');const data=await response.json();
    if (generation!==rendering || q.getCurrent()?.id!==note.id) return;
    for (const job of data.jobs) {
      const details=document.createElement('details'),summary=document.createElement('summary');
      const names={pending:'等待电脑领取',running:'电脑正在识别',succeeded:'识别结果',failed:'识别失败'};
      summary.textContent=names[job.state]+' · '+new Date(job.created_at*1000).toLocaleString();details.append(summary);
      if (job.error) { const p=document.createElement('p');p.textContent=job.error;details.append(p); }
      if (job.state==='succeeded') {
        const candidate=job.results.map((page,i) => '〔图片 '+(i+1)+'〕\n'+page.text).join('\n\n');
        const pre=document.createElement('pre');pre.className='raw';pre.textContent=candidate;details.append(pre);
        const compatible=JSON.stringify(job.photos.map(p => p.id))===JSON.stringify(note.photos);
        if (compatible) details.append(button('采用为原文，再人工校对',async () => {
          if (q.getCurrent()?.id!==note.id) throw new Error('记录已切换，请重新核对');
          if (q.getCurrent().original_text && !confirm('用这份本地识别结果替换当前原文？一读和二读评论会保留。')) return;
          if (candidate.length>50000) throw new Error('候选超过 50000 字符，请分条处理');
          await q.save({original_text:candidate});
          document.getElementById('quick-original').closest('details').open=true;
        }));
        else { const p=document.createElement('p');p.textContent='这是旧图片或旧顺序的历史结果，仍保留供核对。';details.append(p); }
      }
      panel.append(details);
    }
    const confirmed=note.confirmed_revision===note.revision;
    if (confirmed) {
      const p=document.createElement('p');
      const purged=note.photo_meta?.some(photo => photo.purged_at);
      const deadline=note.photo_meta?.find(photo => photo.purge_after)?.purge_after;
      p.textContent=purged ? '已确认的图片已按计划清理；原文、评注及识别历史保留。' : '原文已确认并同步；图片在 30 天恢复期内仍可查看和取消清理。'+(deadline ? '计划清理：'+new Date(deadline*1000).toLocaleString() : '');panel.append(p);
      if (!purged) panel.append(button('取消图片清理',async () => {
        const current=await synced();await q.api(q.endpoint(current.id)+'/undo-cleanup',{revision:current.revision});await q.sync();await render();
      }));
    } else if (note.photos.length) {
      panel.append(button('已校对并同步，30 天后清理图片',async () => {
        const current=await synced();
        if (!confirm('确认已对照图片校对原文？图片仍保留 30 天，期间可取消清理；修改文字或图片会取消这次确认。')) return;
        await q.api(q.endpoint(current.id)+'/confirm',{revision:current.revision});await q.sync();await render();
      }));
    }
  }
  async function tokens() {
    const data=await (await q.api('/api/paper/worker-tokens')).json();$('quick-tokens').replaceChildren();
    for (const token of data.tokens) {
      const p=document.createElement('p');p.textContent=token.label+' · '+(token.revoked_at ? '已撤销' : '有效')+' ';
      if (!token.revoked_at) p.append(button('撤销',async () => { await q.api('/api/paper/worker-tokens/'+token.id+'/revoke',{});await tokens(); }));
      $('quick-tokens').append(p);
    }
  }
  $('quick-token-new').addEventListener('click',() => q.safe(async () => {
    await q.sync();
    const result=await (await q.api('/api/paper/worker-tokens',{label:'Windows 本地 OCR'})).json();
    $('quick-token-label').hidden=false;$('quick-token-value').value=result.token;
    $('quick-token-value').focus();$('quick-token-value').select();await tokens();
  }));
  $('quick-desktop').addEventListener('toggle',() => {
    if ($('quick-desktop').open && q.getContext()) q.safe(tokens);
    if (!$('quick-desktop').open) { $('quick-token-value').value='';$('quick-token-label').hidden=true; }
  });
  $('quick-batch').addEventListener('click',() => q.safe(async () => {
    await q.sync();
    const result=await (await q.api('/api/paper/ocr-batch',{})).json();
    $('quick-batch-status').textContent='已排队 '+result.queued+' 条；已有结果或暂不符合条件 '+result.skipped+' 条。请在电脑运行本地识别程序。';await render();
  }));
  let timer;
  for (const name of ['quick-note-open','quick-sync-complete']) document.addEventListener(name,() => {
    clearTimeout(timer);timer=setTimeout(() => q.safe(render),150);
  });
  if (q.getCurrent()) q.safe(render);
})();
