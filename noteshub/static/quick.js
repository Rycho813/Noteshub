/* Local-first recorder. IndexedDB commits precede network activity and UI ACKs. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const uuid = () => crypto.randomUUID().replaceAll('-','');
  const clone = value => structuredClone(value);
  let db, context, current, ready = false, saving = Promise.resolve(), syncing = false, adding = false;
  let lastError = '', localFailed = false, syncFailed = false, urls = [], retryTimer;
  const fields = {first_comment:$('quick-thought'),original_text:$('quick-original'),second_comment:$('quick-second')};
  const request = item => new Promise((resolve,reject) => { item.onsuccess = () => resolve(item.result); item.onerror = () => reject(item.error); });
  function transaction(stores,mode,callback) {
    return new Promise((resolve,reject) => {
      const tx = db.transaction(stores,mode); let result;
      tx.oncomplete = () => resolve(result);
      tx.onabort = tx.onerror = () => reject(tx.error || new Error('本机保存未完成'));
      Promise.resolve(callback(tx)).then(value => { result = value; }).catch(error => { try { tx.abort(); } catch (_) {} reject(error); });
    });
  }
  const get = (store,key) => transaction([store],'readonly',tx => request(tx.objectStore(store).get(key)));
  const all = store => transaction([store],'readonly',tx => request(tx.objectStore(store).getAll()));
  const put = (store,value) => transaction([store],'readwrite',tx => { tx.objectStore(store).put(value); });
  const key = id => context.workspace + ':' + id;
  function error(message = '') { lastError = message; $('quick-error').hidden = !message; $('quick-error').textContent = message; }
  function status(message) { $('quick-status').textContent = message; }
  function displayStatus() {
    if (localFailed) return status('本机保存失败 · 请导出恢复文件并保留本页');
    if (current?.conflict) return status('同步失败 · 本机内容保留，云端有更新');
    if (syncing) return status('同步中');
    if (syncFailed) return status('同步失败 · 本机内容保留');
    status(current?.dirty ? '仅本地保存' : current?.revision ? '已同步' : '可以开始记录');
  }
  async function api(url,payload,blob) {
    const options = {credentials:'same-origin',redirect:'error',cache:'no-store',signal:AbortSignal.timeout(30000)};
    if (payload !== undefined || blob) Object.assign(options,{method:'POST',headers:{'X-CSRF-Token':context.csrf,'Content-Type':blob ? 'application/octet-stream' : 'application/json'},body:blob || JSON.stringify(payload)});
    const response = await fetch(url,options);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const e = new Error(data.detail || '同步未完成，请稍后重试'); e.status = response.status; throw e;
    }
    return response;
  }
  const endpoint = id => '/api/paper/notes/' + id;
  function fresh(book) {
    return {id:uuid(),workspace:context.workspace,book_id:book,original_text:'',first_comment:'',second_comment:'',photos:[],photo_meta:[],revision:0,localVersion:0,dirty:false,updated_at:Date.now()};
  }
  async function list() {
    const notes = (await all('notes')).filter(n => n.workspace === context.workspace).sort((a,b) => b.updated_at-a.updated_at);
    $('quick-list').replaceChildren();
    for (const note of notes) {
      const button = document.createElement('button'); button.className = 'secondary';
      const book = context.books.find(b => b.id === note.book_id);
      button.textContent = (book?.title || '书籍待更新') + ' · ' + (note.first_comment || note.original_text || '图片记录').slice(0,65) + ' · ' + (note.conflict ? '同步失败' : note.dirty ? '仅本地保存' : '已同步');
      button.addEventListener('click',() => safe(async () => { await saving; await open(note.id); }));
      $('quick-list').append(button);
    }
  }
  async function paintPhotos() {
    urls.forEach(url => URL.revokeObjectURL(url)); urls = [];
    $('quick-photos').replaceChildren();
    const note = current;
    for (const [index,id] of note.photos.entries()) {
      const photo = await get('photos',key(id));
      if (current !== note) return;
      const li = document.createElement('li'), img = document.createElement('img');
      img.alt = '图片 ' + (index+1);
      if (photo?.blob) { const url = URL.createObjectURL(photo.blob); urls.push(url); img.src = url; }
      else img.alt += note.photo_meta?.find(p => p.id===id)?.purged_at ? '（已确认清理）' : '（联网后可查看）';
      li.append(img);
      for (const [label,offset] of [['前移',-1],['后移',1],['移除',0]]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary'; button.textContent = label;
        button.disabled = offset !== 0 && (index+offset < 0 || index+offset >= note.photos.length);
        button.addEventListener('click',() => safe(async () => {
          if (!offset) current.photos.splice(index,1);
          else [current.photos[index],current.photos[index+offset]] = [current.photos[index+offset],current.photos[index]];
          await persist(); await paintPhotos();
        })); li.append(button);
      }
      $('quick-photos').append(li);
    }
  }
  function applyServer(note,remote) {
    return {...note,...remote,photos:remote.photos.map(p => p.id),photo_meta:remote.photos,workspace:context.workspace,
      updated_at:remote.updated_at*1000,localVersion:note?.localVersion || 0,dirty:false,attempt:null,conflict:null};
  }
  async function open(id) {
    if (adding) throw new Error('正在保存照片，请完成后再切换记录');
    const note = await get('notes',key(id));
    if (!note) return;
    current = note;
    await put('meta',{key:'lastNote:'+context.workspace,value:note.id});
    await put('meta',{key:'lastBook:'+context.workspace,value:note.book_id});
    for (const [name,input] of Object.entries(fields)) input.value = note[name];
    $('quick-book').value = note.book_id;
    $('quick-copy').hidden = !note.conflict;
    $('quick-remote').hidden = !note.conflict;
    $('quick-remote').textContent = note.conflict ? '云端内容（未覆盖本机）：\n' + note.conflict.original_text + '\n一读：\n' + note.conflict.first_comment : '';
    $('quick-formal').hidden = !note.revision;
    $('quick-formal').href = '/books/' + note.book_id + '?note=' + note.id;
    displayStatus(); await paintPhotos();
    if (navigator.onLine) safe(() => cachePhotos(note));
    document.dispatchEvent(new CustomEvent('quick-note-open',{detail:note}));
  }
  async function cachePhotos(note) {
    for (const id of note.photos) {
      if (note.photo_meta?.find(p => p.id===id)?.purged_at || (await get('photos',key(id)))?.blob) continue;
      const response = await api(endpoint(note.id)+'/photos/'+id);
      await put('photos',{key:key(id),id,workspace:context.workspace,blob:await response.blob(),uploaded:true});
    }
    if (current.id === note.id) await paintPhotos();
  }
  async function newNote(book) {
    if (adding) throw new Error('正在保存照片，请完成后再切换书籍或记录');
    await saving;
    current = fresh(book || $('quick-book').value || context.books[0]?.id || '');
    for (const input of Object.values(fields)) input.value = '';
    $('quick-copy').hidden = true; $('quick-remote').hidden = true; $('quick-formal').hidden = true;
    $('quick-book').value = current.book_id;
    await put('meta',{key:'lastBook:'+context.workspace,value:current.book_id});
    error(); displayStatus(); await paintPhotos();
    document.dispatchEvent(new CustomEvent('quick-note-open',{detail:current}));
    fields.first_comment.focus();
  }
  function persist(photo) {
    for (const [name,input] of Object.entries(fields)) current[name] = input.value;
    const expected = current.localVersion;
    current.localVersion++; current.dirty = true; current.updated_at = Date.now();
    const value = clone(current);
    status('正在本地保存…');
    saving = saving.catch(() => {}).then(() => transaction(['notes','photos','recoveries','meta'],'readwrite',async tx => {
      const store = tx.objectStore('notes'); const old = await request(store.get(key(value.id)));
      if (photo) tx.objectStore('photos').put(photo);
      if ((old?.localVersion || 0) !== expected) {
        tx.objectStore('recoveries').put({key:context.workspace+':'+uuid(),note:value});
        return false;
      }
      store.put({...value,key:key(value.id),revision:old?.revision || 0,attempt:old?.attempt || null,conflict:old?.conflict || null});
      tx.objectStore('meta').put({key:'lastNote:'+context.workspace,value:value.id});
      return true;
    })).then(async success => {
      if (!success) {
        localFailed = true;
        error('另一标签页已修改这条记录。本次输入已保存为本机恢复副本；请点“另存一条”，或导出本机恢复文件。');
        $('quick-copy').hidden = false;
      } else { localFailed = false; }
      displayStatus(); await list();
      if (success) scheduleSync();
    }).catch(e => { localFailed = true; error('本机保存失败：'+e.message+'。请保留本页并导出本机恢复文件。'); displayStatus(); throw e; });
    return saving;
  }
  function scheduleSync() { clearTimeout(retryTimer); retryTimer = setTimeout(() => safe(sync),700); }
  async function patchLocal(id,callback) {
    return transaction(['notes'],'readwrite',async tx => {
      const store = tx.objectStore('notes'), note = await request(store.get(key(id)));
      if (!note) return;
      callback(note); store.put(note); return note;
    });
  }
  async function sendAttempt(note,payload,final) {
    // Persist the exact operation before sending. Lost replies replay this ID,
    // even if more typing occurs while the request is in flight.
    if (!note.attempt) note = await patchLocal(note.id,n => { n.attempt = {payload,version:note.localVersion,final}; });
    const attempt = note.attempt;
    const remote = await (await api(endpoint(note.id)+'/sync',attempt.payload)).json();
    return patchLocal(note.id,n => {
      n.revision = remote.revision; n.attempt = null; n.conflict = null; n.photo_meta = remote.photos;
      n.confirmed_revision = remote.confirmed_revision;
      if (attempt.final && n.localVersion === attempt.version) n.dirty = false;
    });
  }
  function payload(note,photos = note.photos) {
    return {mutation_id:uuid(),book_id:note.book_id,revision:note.revision,original_text:note.original_text,
      first_comment:note.first_comment,second_comment:note.second_comment,photo_ids:photos};
  }
  async function syncOne(note) {
    if (note.conflict) return;
    try {
      if (note.attempt) note = await sendAttempt(note,null,false);
      if (!note.revision) note = await sendAttempt(note,payload(note,[]),false);
      for (const id of note.photos) {
        const photo = await get('photos',key(id));
        if (!photo?.uploaded) {
          if (!photo?.blob) {
            if (note.photo_meta?.some(p => p.id===id)) continue;
            throw new Error('本机缺少来源图片，请导入恢复文件');
          }
          await api(endpoint(note.id)+'/photos/'+id,undefined,photo.blob);
          await put('photos',{...photo,uploaded:true});
        }
      }
      await sendAttempt(note,payload(note),true);
    } catch (e) {
      if (e.status === 409) {
        const response = await api(endpoint(note.id)).catch(() => null);
        if (response) {
          const remote = await response.json();
          await patchLocal(note.id,n => { n.conflict = remote; });
        }
      }
      throw e;
    }
  }
  async function refreshContext() {
    const remote = await (await api('/api/paper/context')).json();
    if (context && context.workspace !== remote.workspace) throw new Error('登录账号或服务器已改变。请先导出本机恢复文件，重新打开页面选择正确资料库。');
    context = remote;
    await put('meta',{key:'context',value:{...remote,csrf:''}});
    return remote;
  }
  async function pull() {
    const remote = await (await api('/api/paper/notes')).json();
    for (const note of remote.notes) await transaction(['notes'],'readwrite',async tx => {
      const store = tx.objectStore('notes'), old = await request(store.get(key(note.id)));
      if (!old?.dirty && !old?.attempt) store.put({...applyServer(old,note),key:key(note.id)});
    });
  }
  async function sync() {
    if (!ready || syncing || !navigator.onLine || localFailed) return;
    await saving;
    syncing = true; displayStatus();
    const run = async () => {
      await refreshContext();
      for (let pass=0;pass<3;pass++) {
        const notes = (await all('notes')).filter(n => n.workspace === context.workspace && n.dirty && !n.conflict);
        if (!notes.length) break;
        for (const note of notes) await syncOne(note);
      }
      await pull(); syncFailed = false; error();
    };
    try {
      if (navigator.locks) await navigator.locks.request('noteshub-sync:'+context.workspace,run);
      else await run();
    } catch(e) { syncFailed = true; error('同步失败：'+e.message+'。本机内容仍保留。登录失效时请在另一页登录，再点立即同步。'); }
    finally {
      syncing = false;
      const stored = current && await get('notes',key(current.id));
      if (stored && stored.localVersion === current.localVersion) {
        // Never overwrite text being typed while network I/O was pending.
        const same = Object.keys(fields).every(name => fields[name].value === current[name]);
        if (same) { current = stored; Object.keys(fields).forEach(name => { fields[name].value=stored[name]; });
          $('quick-copy').hidden = !stored.conflict; $('quick-remote').hidden = !stored.conflict;
          $('quick-remote').textContent = stored.conflict ? '云端原文：\n'+stored.conflict.original_text+'\n云端一读：\n'+stored.conflict.first_comment : '';
          $('quick-formal').hidden = !stored.revision; $('quick-formal').href = '/books/'+stored.book_id+'?note='+stored.id; }
      }
      displayStatus(); await list();
      document.dispatchEvent(new CustomEvent('quick-sync-complete',{detail:current}));
    }
  }
  async function prepare(file) {
    if (file.size > 20*1024*1024) throw new Error('来源图片超过 20 MiB，请缩小后选择');
    const bitmap = await createImageBitmap(file,{imageOrientation:'from-image'});
    try {
      if (bitmap.width*bitmap.height > 24000000) throw new Error('来源图片超过 2400 万像素');
      const scale = Math.min(1,2400/Math.max(bitmap.width,bitmap.height));
      const canvas = document.createElement('canvas'); canvas.width = Math.max(1,Math.round(bitmap.width*scale)); canvas.height = Math.max(1,Math.round(bitmap.height*scale));
      canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve,'image/jpeg',.9));
      if (!blob || blob.size > 5*1024*1024) throw new Error('图片处理失败或仍超过 5 MiB，请缩小后选择');
      return blob;
    } finally { bitmap.close(); }
  }
  async function addFiles(input) {
    const files = [...input.files]; input.value = '';
    adding = true;
    $('quick-camera').disabled = $('quick-files').disabled = $('quick-book').disabled = $('quick-new').disabled = true;
    try {
      for (const [index,file] of files.entries()) {
        if (current.photos.length >= 20) throw new Error('每条记录最多 20 张图片，可新建下一条');
        $('quick-photo-status').textContent = '正在本机保存图片 '+(index+1)+' / '+files.length;
        const blob = await prepare(file), id = uuid();
        current.photos.push(id);
        await persist({key:key(id),id,workspace:context.workspace,blob,uploaded:false});
        await paintPhotos();
      }
      $('quick-photo-status').textContent = '图片已保存在本机';
    } finally { adding = false; $('quick-camera').disabled = $('quick-files').disabled = $('quick-book').disabled = $('quick-new').disabled = false; }
  }
  async function copyNote() {
    await saving;
    const old = clone(current);
    if (navigator.onLine) await cachePhotos(old);
    const copied = fresh(old.book_id);
    Object.keys(fields).forEach(name => { copied[name] = fields[name].value; });
    const photos = [];
    for (const id of old.photos) {
      const photo = await get('photos',key(id));
      if (!photo?.blob) throw new Error('另存需要本机图片副本，请联网加载图片后重试');
      const newId = uuid(); copied.photos.push(newId); photos.push({...photo,id:newId,key:key(newId),uploaded:false});
    }
    copied.dirty = true; copied.localVersion = 1;
    await transaction(['notes','photos'],'readwrite',tx => {
      tx.objectStore('notes').put({...copied,key:key(copied.id)});
      if (old.conflict) tx.objectStore('notes').put({...applyServer(old,old.conflict),key:key(old.id)});
      photos.forEach(p => tx.objectStore('photos').put(p));
    });
    localFailed = false; await open(copied.id); await list(); scheduleSync(); error('已另存本机内容为新记录，原云端记录保留。');
  }
  async function downloadBackup() {
    await saving.catch(() => {});
    const photos = await all('photos'), encoded = [];
    for (const photo of photos) {
      const bytes = new Uint8Array(await photo.blob.arrayBuffer()); let binary = '';
      for (let i=0;i<bytes.length;i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192));
      encoded.push({...photo,blob:undefined,data:btoa(binary),type:photo.blob.type});
    }
    const data = {format:'noteshub-local-v1',notes:await all('notes'),photos:encoded,recoveries:await all('recoveries'),meta:await all('meta'),unsaved:current ? {...clone(current),...Object.fromEntries(Object.entries(fields).map(([name,input]) => [name,input.value]))} : null};
    const url = URL.createObjectURL(new Blob([JSON.stringify(data)],{type:'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = 'noteshub-local-'+new Date().toISOString().replaceAll(':','-')+'.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url),10000);
  }
  async function restore(file) {
    if (!file || file.size > 200*1024*1024) throw new Error('请选择不超过 200 MiB 的本机恢复文件');
    const data = JSON.parse(await file.text());
    if (data.format !== 'noteshub-local-v1' || !Array.isArray(data.notes) || !Array.isArray(data.photos)) throw new Error('恢复文件格式无效');
    const photos = new Map(data.photos.map(p => [p.key,p]));
    // Import as fresh notes: never overwrite this device's or the server's IDs.
    const sources = new Map(data.notes.map(n => [n.workspace+':'+n.id,n]));
    if (data.unsaved) sources.set(data.unsaved.workspace+':'+data.unsaved.id,data.unsaved);
    for (const [index,r] of (data.recoveries || []).entries()) sources.set('recovery:'+index,r.note);
    for (const source of sources.values()) {
      if (source.workspace !== context.workspace || !context.books.some(b => b.id===source.book_id)) throw new Error('恢复文件属于其他账号或书籍，请打开原资料库后恢复');
      const note = fresh(source.book_id), additions = [];
      for (const name of Object.keys(fields)) { if (typeof source[name] !== 'string' || source[name].length>50000) throw new Error('恢复文字无效'); note[name] = source[name]; }
      for (const id of source.photos) {
        const photo = photos.get(source.workspace+':'+id);
        if (!photo) throw new Error('恢复文件缺少图片，未覆盖现有记录');
        const newId = uuid(), bytes = Uint8Array.from(atob(photo.data),c => c.charCodeAt(0));
        note.photos.push(newId); additions.push({key:key(newId),id:newId,workspace:context.workspace,blob:new Blob([bytes],{type:photo.type}),uploaded:false});
      }
      note.dirty = true; note.localVersion = 1;
      await transaction(['notes','photos'],'readwrite',tx => { tx.objectStore('notes').put({...note,key:key(note.id)}); additions.forEach(p => tx.objectStore('photos').put(p)); });
    }
    await list(); error('已作为新记录恢复到本机，可核对后点击立即同步。原记录未覆盖。');
  }
  function safe(fn) { return Promise.resolve().then(fn).catch(e => error(e.message)); }
  async function initialize() {
    if (!window.indexedDB || !window.crypto?.randomUUID || !window.isSecureContext) throw new Error('离线记录需要 HTTPS（本机测试可用 localhost）及支持 IndexedDB 的浏览器');
    const opening = indexedDB.open('noteshub-quick',1);
    opening.onupgradeneeded = () => { for (const name of ['notes','photos','meta','recoveries']) opening.result.createObjectStore(name,{keyPath:'key'}); };
    db = await request(opening);
    db.onversionchange = () => { db.close(); error('本机存储版本已更新，请重新打开页面'); };
    context = (await get('meta','context'))?.value;
    try { await refreshContext(); } catch(e) { if (!context) throw new Error('首次使用请先联网登录，再打开快速记录。'); error('当前离线或登录失效，仍可保存本机记录。'); }
    for (const book of context.books) { const option = document.createElement('option'); option.value=book.id; option.textContent=book.title; $('quick-book').append(option); }
    if (!context.books.length) throw new Error('请先联网登录，在书籍页面创建一本书，再打开快速记录');
    ready = true;
    for (const input of [...Object.values(fields),$('quick-camera'),$('quick-files'),$('quick-book'),$('quick-new'),$('quick-sync')]) input.disabled=false;
    if (navigator.onLine) { await pull().catch(() => {}); }
    const params = new URLSearchParams(location.search), noteId=params.get('note') || (!params.has('book') && (await get('meta','lastNote:'+context.workspace))?.value);
    if (params.has('note') && !await get('notes',key(noteId))) {
      const remote = await (await api(endpoint(noteId))).json();
      await put('notes',{...applyServer(null,remote),key:key(remote.id)});
    }
    if (noteId && await get('notes',key(noteId))) await open(noteId);
    else {
      const book=params.get('book') || (await get('meta','lastBook:'+context.workspace))?.value;
      await newNote(context.books.some(b => b.id===book) ? book : context.books[0].id);
    }
    for (const input of Object.values(fields)) input.addEventListener('input',() => safe(() => persist()));
    $('quick-book').addEventListener('change',() => safe(() => newNote($('quick-book').value)));
    $('quick-new').addEventListener('click',() => safe(() => newNote()));
    $('quick-sync').addEventListener('click',() => safe(sync));
    $('quick-copy').addEventListener('click',() => safe(copyNote));
    for (const id of ['quick-camera','quick-files']) $(id).addEventListener('change',() => safe(() => addFiles($(id))));
    $('quick-backup').addEventListener('click',() => safe(downloadBackup));
    $('quick-restore').addEventListener('change',() => safe(() => restore($('quick-restore').files[0])));
    window.addEventListener('online',() => safe(sync));
    document.addEventListener('visibilitychange',() => { if (document.visibilityState === 'visible') safe(sync); });
    setInterval(() => { if (document.visibilityState === 'visible') safe(sync); },30000);
    window.addEventListener('beforeunload',event => { if (localFailed) { event.preventDefault(); event.returnValue=''; } });
    await list();
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/quick-sw.js',{scope:'/quick'}).catch(() => error('离线页面尚未准备完成，请联网重新打开本页后再离线使用'));
    const persisted = await navigator.storage?.persist?.().catch(() => false);
    $('quick-storage').textContent = persisted ? '浏览器已授予持久存储。' : '浏览器未承诺永久保留本机存储，请及时同步或导出本机恢复文件。';
    safe(sync);
  }
  // Small integration surface for the optional desktop OCR/confirmation panel.
  window.NotesHubQuick = {api,endpoint,getContext:() => context,getCurrent:() => current,
    save:async values => { Object.entries(values).forEach(([name,value]) => { if (fields[name]) fields[name].value=value; }); await persist(); },
    sync,open:async () => open(current.id),safe,error};
  safe(initialize);
})();
