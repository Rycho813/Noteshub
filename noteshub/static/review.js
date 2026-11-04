/* Independent row save streams; every page transition drains all rows. */
(() => {
  'use strict';
  const list=document.getElementById('review-list');
  if(!list)return;
  let moving=false;
  const editors=[...list.querySelectorAll('.review-editor')].map(root => {
    const $=selector => root.querySelector(selector),text=$('.second-comment'),output=$('.save-status');
    let saved=text.value,desired=null,uncertain=false,inFlight,timer,composing=false;
    const dirty=() => uncertain || saved!==text.value || desired!==null;
    const message=(value,failed=false) => {output.textContent=value;output.classList.toggle('save-error',failed);};
    const controls=() => {
      text.readOnly=moving;
      for(const b of root.querySelectorAll('button'))b.disabled=moving;
      $('.save-now').textContent=uncertain?'重试保存':'立即保存';
    };
    async function drain() {
      while(dirty()) {
        const snapshot=text.value,status=desired;
        const body=new URLSearchParams({csrf:root.dataset.csrf,second_comment:snapshot});
        if(root.dataset.paperRevision)body.set('revision',root.dataset.paperRevision);
        if(status!==null)body.set('status',status);
        message('保存中…');
        try {
          const response=await fetch(root.dataset.saveUrl,{method:'POST',body,credentials:'same-origin',redirect:'error',signal:AbortSignal.timeout(10000),keepalive:new TextEncoder().encode(body.toString()).length<60000});
          const result=await response.json();
          if(!response.ok) {
            if(response.status===401 || response.status===403)$('.login-hint').hidden=false;
            throw new Error(result.detail || '请重试');
          }
          if(result.second_comment!==snapshot || !['pending','done'].includes(result.status) || !Number.isInteger(result.done) || !Number.isInteger(result.total))throw new Error('未能确认保存结果');
          saved=snapshot;uncertain=false;if(desired===status)desired=null;
          if(Number.isInteger(result.revision))root.dataset.paperRevision=String(result.revision);
          root.dataset.status=result.status;
          $('.note-status').textContent=result.status==='done'?'已完成':'待处理';
          $('.restore-pending').hidden=result.status!=='done';$('.login-hint').hidden=true;
          document.getElementById('book-progress').textContent=result.done+'/'+result.total;
          document.getElementById('export-warning').textContent=`仍有 ${result.total-result.done} 条待处理；导出会包含全部 ${result.total} 条笔记。`;
        } catch(e) {
          uncertain=true;message('保存失败：'+e.message+'。文字仍在本条输入框中。',true);throw e;
        }
      }
      message('已保存');
      const summary=$('.second-reading summary');summary.textContent='二读评述'+(saved?' · 已有内容':'');
    }
    function flush() {
      clearTimeout(timer);
      if(!inFlight)inFlight=drain().finally(() => {inFlight=null;controls();});
      return inFlight;
    }
    function schedule() {clearTimeout(timer);message('尚未保存');if(!composing)timer=setTimeout(() => flush().catch(() => {}),500);}
    text.disabled=false;text.addEventListener('input',schedule);
    text.addEventListener('compositionstart',() => {composing=true;clearTimeout(timer);});
    text.addEventListener('compositionend',() => {composing=false;schedule();});
    $('.save-now').addEventListener('click',() => flush().catch(() => {}));
    $('.complete-note').addEventListener('click',() => {desired='done';flush().catch(() => {});});
    $('.restore-pending').addEventListener('click',() => {desired='pending';flush().catch(() => {});});
    const ocr=$('.record-ocr');
    if(ocr)ocr.addEventListener('toggle',async () => {
      if(!ocr.open || ocr.querySelector('iframe'))return;
      try {
        await flush();
        const iframe=document.createElement('iframe');iframe.src=ocr.dataset.src;iframe.title='本条来源图片与本地识别校对';iframe.className='record-ocr-frame';ocr.append(iframe);
      } catch(_) {ocr.open=false;$('.second-reading').open=true;}
    });
    window.addEventListener('message',async event => {
      if(event.origin!==location.origin || event.source!==ocr?.querySelector('iframe')?.contentWindow || event.data?.type!=='noteshub-record-synced' || event.data.id!==root.dataset.noteId || dirty() || inFlight)return;
      try {
        const response=await fetch('/api/paper/notes/'+root.dataset.noteId,{cache:'no-store',redirect:'error'});
        if(!response.ok)return;const note=await response.json();
        if(dirty() || inFlight)return;
        root.dataset.paperRevision=String(note.revision);saved=note.second_comment;text.value=saved;
        $('.original').textContent=note.original_text;$('.first-comment').textContent=note.first_comment;
        $('.second-reading summary').textContent='二读评述'+(saved?' · 已有内容':'');
      } catch(_) { /* A subsequent edit still uses CAS protection. */ }
    });
    message('已保存');controls();
    return {root,dirty,flush,controls,pending:() => inFlight};
  });
  if(list.dataset.focusNote)document.getElementById('note-'+list.dataset.focusNote)?.scrollIntoView({block:'start'});
  const exportButton=document.getElementById('export-book');exportButton.disabled=false;
  const flushAll=async () => {
    await Promise.all(editors.map(e => e.flush()));
    await Promise.all([...list.querySelectorAll('iframe')].map(frame => frame.contentWindow.NotesHubQuick?.flush()));
  };
  async function action(callback) {
    if(moving)return;moving=true;editors.forEach(e => e.controls());exportButton.disabled=true;
    try {await flushAll();await callback();}
    catch(_) {const failed=editors.find(e => e.dirty());if(failed){failed.root.querySelector('.second-reading').open=true;failed.root.scrollIntoView({block:'center'});}}
    finally {moving=false;editors.forEach(e => e.controls());exportButton.disabled=false;}
  }
  document.addEventListener('click',event => {
    const link=event.target.closest('a[href]');
    if(!link || event.defaultPrevented || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.target==='_blank')return;
    event.preventDefault();action(() => location.assign(link.href));
  });
  document.querySelectorAll('.page-jump, .site-header form[action="/logout"]').forEach(form => form.addEventListener('submit',event => {
    event.preventDefault();if(!form.reportValidity())return;action(() => HTMLFormElement.prototype.submit.call(form));
  }));
  exportButton.addEventListener('click',() => action(async () => {
    try {
      const response=await fetch(list.dataset.exportUrl,{credentials:'same-origin',redirect:'error',signal:AbortSignal.timeout(15000)});
      if(!response.ok || !response.headers.get('Content-Type')?.startsWith('text/markdown'))throw new Error('请检查登录状态或稍后重试');
      const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');
      link.href=url;link.download=list.dataset.exportName;link.addEventListener('click',e => e.stopPropagation());document.body.append(link);link.click();link.remove();setTimeout(() => URL.revokeObjectURL(url),30000);
    } catch(e) {document.getElementById('export-warning').textContent='导出失败：'+e.message;throw e;}
  }));
  window.addEventListener('beforeunload',event => {if(editors.some(e => e.dirty() || e.pending())){event.preventDefault();event.returnValue='';}});
  document.addEventListener('visibilitychange',() => {if(document.visibilityState==='hidden')flushAll().catch(() => {});});
  window.addEventListener('online',() => flushAll().catch(() => {}));
})();
