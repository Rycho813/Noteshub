/* Local-first recorder. IndexedDB commits precede network activity and UI ACKs. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const uuid = () => crypto.randomUUID().replaceAll('-','');
  const clone = value => structuredClone(value);
  let db, context, current, ready = false, saving = Promise.resolve(), syncing = false, adding = false, pendingSaves = 0;
  let lastError = '', localFailed = false, syncFailed = false, urls = [], retryTimer;
  const fields = {first_comment:$('quick-thought'),original_text:$('quick-original'),second_comment:$('quick-second')};
  const pendingPhotos = new Map();
  const viewer = $('quick-viewer'), viewerImage = $('quick-viewer-image'), viewerStage = $('quick-viewer-stage');
  let viewedPhoto, viewerURL, zoom = 1, photoPaint = 0, viewerResizeFrame;
  function sizeViewer() {
    if (!viewer.open || !viewerImage.complete || !viewerImage.naturalWidth) return;
    $('quick-viewer-edit').disabled=false;
    const height = viewerStage.clientHeight-parseFloat(getComputedStyle(viewerStage).paddingBottom);
    const fit = Math.min(viewerStage.clientWidth/viewerImage.naturalWidth,height/viewerImage.naturalHeight);
    const width = Math.max(1,Math.floor(viewerImage.naturalWidth*fit*zoom))+'px';
    const imageHeight = Math.max(1,Math.floor(viewerImage.naturalHeight*fit*zoom))+'px';
    if (viewerImage.style.width !== width) viewerImage.style.width = width;
    if (viewerImage.style.height !== imageHeight) viewerImage.style.height = imageHeight;
    $('quick-viewer-status').textContent = viewedPhoto.label+' · '+Math.round(zoom*100)+'%';
    $('quick-viewer-out').disabled = zoom <= 1;
    $('quick-viewer-in').disabled = zoom >= 4;
    $('quick-viewer-prev').disabled = viewedPhoto.index === 0;
    $('quick-viewer-next').disabled = viewedPhoto.index === current.photos.length-1;
  }
  function zoomViewer(value) {
    zoom = Math.max(1,Math.min(4,value)); sizeViewer();
    viewerStage.scrollTo(0,0);
  }
  function showPhoto(photo,id,index,note) {
    if (viewer.open) return;
    cancelEdit();
    viewedPhoto = {id,noteId:note.id,blob:photo.blob,index,label:'图片 '+(index+1)+' / '+note.photos.length};
    viewerURL = URL.createObjectURL(photo.blob);
    viewerImage.alt = viewedPhoto.label;
    $('quick-viewer-edit').disabled=true;
    viewerImage.src = viewerURL; zoom = 1;
    document.documentElement.classList.add('quick-viewer-open');
    viewer.showModal(); sizeViewer();
    viewerStage.scrollTo(0,0);
  }
  viewerImage.addEventListener('load',sizeViewer);
  function finishViewer() {
    // Native close events are queued. An earlier close must never clean up a
    // new image that the user has already opened in the meantime.
    if (viewer.open) return;
    viewerImage.removeAttribute('src');
    if (viewerURL) URL.revokeObjectURL(viewerURL);
    viewerURL = null;
    document.documentElement.classList.remove('quick-viewer-open');
    if (current?.id === viewedPhoto?.noteId) {
      const button = [...$('quick-photos').querySelectorAll('.quick-photo-open')].find(b => b.dataset.photoId === viewedPhoto.id);
      button?.focus({preventScroll:true});
    }
    viewedPhoto = null;
  }
  function closeViewer() { if (editBusy) return; cancelEdit(); viewer.close(); finishViewer(); }
  $('quick-viewer-close').addEventListener('click',closeViewer);
  viewer.addEventListener('cancel',event => { event.preventDefault(); closeViewer(); });
  $('quick-viewer-in').addEventListener('click',() => zoomViewer(zoom*1.5));
  $('quick-viewer-out').addEventListener('click',() => zoomViewer(zoom/1.5));
  $('quick-viewer-fit').addEventListener('click',() => zoomViewer(1));
  viewer.addEventListener('close',finishViewer);
  new ResizeObserver(() => {
    // Scrollbar changes can resize the stage too. Resize in the next frame so
    // WebKit does not report an observer loop while zooming or rotating.
    cancelAnimationFrame(viewerResizeFrame);
    viewerResizeFrame = requestAnimationFrame(sizeViewer);
  }).observe(viewerStage);
  let editBitmap, editRotation=0, editCrop, editPointer, editBusy=false;
  const editCanvas=$('quick-edit-canvas'), editContext=editCanvas.getContext('2d');
  const cropFields=['x','y','w','h'];
  function cancelEdit() {
    if (editBusy) return;
    editBitmap?.close?.(); editBitmap=null; editPointer=null;
    $('quick-image-edit').hidden=true; viewerStage.hidden=false;
    for (const id of ['prev','next','in','out','fit','edit']) $('quick-viewer-'+id).hidden=false;
  }
  function drawEdit() {
    if (!editBitmap) return;
    const w=editRotation%2 ? editBitmap.height : editBitmap.width;
    const h=editRotation%2 ? editBitmap.width : editBitmap.height;
    editCanvas.width=w;editCanvas.height=h;
    editContext.save();
    if(editRotation===1) editContext.translate(editBitmap.height,0);
    if(editRotation===2) editContext.translate(editBitmap.width,editBitmap.height);
    if(editRotation===3) editContext.translate(0,editBitmap.width);
    editContext.rotate(editRotation*Math.PI/2);editContext.drawImage(editBitmap,0,0);editContext.restore();
    const {x,y,w:cw,h:ch}=editCrop;
    editContext.fillStyle='rgba(0,0,0,.5)';
    editContext.fillRect(0,0,w,y);editContext.fillRect(0,y+ch,w,h-y-ch);
    editContext.fillRect(0,y,x,ch);editContext.fillRect(x+cw,y,w-x-cw,ch);
    editContext.strokeStyle='#fff';editContext.lineWidth=Math.max(2,w/300);editContext.strokeRect(x,y,cw,ch);
    for (const name of cropFields) $('quick-crop-'+name).value=editCrop[name];
    $('quick-edit-size').textContent='选区 '+cw+' × '+ch+' 像素';
  }
  function resetEdit() {
    editCrop={x:0,y:0,w:editRotation%2 ? editBitmap.height : editBitmap.width,h:editRotation%2 ? editBitmap.width : editBitmap.height};drawEdit();
  }
  $('quick-viewer-edit').addEventListener('click',() => safe(async () => {
    if(!viewerImage.complete || !viewerImage.naturalWidth)throw new Error('图片正在加载，请稍后再编辑');
    editBitmap=document.createElement('canvas');editBitmap.width=viewerImage.naturalWidth;editBitmap.height=viewerImage.naturalHeight;
    editBitmap.getContext('2d').drawImage(viewerImage,0,0);editRotation=0;
    $('quick-image-edit').hidden=false;viewerStage.hidden=true;
    for(const id of ['prev','next','in','out','fit','edit']) $('quick-viewer-'+id).hidden=true;
    resetEdit();
  }));
  $('quick-edit-cancel').addEventListener('click',cancelEdit);
  $('quick-edit-reset').addEventListener('click',resetEdit);
  $('quick-edit-rotate').addEventListener('click',() => {editRotation=(editRotation+1)%4;resetEdit();});
  for(const name of cropFields) $('quick-crop-'+name).addEventListener('change',() => {
    const w=editCanvas.width,h=editCanvas.height;
    const val=n => Math.round(Number($('quick-crop-'+n).value)) || 0;
    editCrop={x:Math.max(0,Math.min(w-1,val('x'))),y:Math.max(0,Math.min(h-1,val('y'))),w:1,h:1};
    editCrop.w=Math.max(1,Math.min(w-editCrop.x,val('w')));editCrop.h=Math.max(1,Math.min(h-editCrop.y,val('h')));drawEdit();
  });
  function cropPoint(event) {
    const r=editCanvas.getBoundingClientRect();return {x:Math.round(Math.max(0,Math.min(editCanvas.width-1,(event.clientX-r.left)*editCanvas.width/r.width))),y:Math.round(Math.max(0,Math.min(editCanvas.height-1,(event.clientY-r.top)*editCanvas.height/r.height)))};
  }
  editCanvas.addEventListener('pointerdown',event => {if(editBusy)return;editPointer=cropPoint(event);editCanvas.setPointerCapture(event.pointerId);event.preventDefault();});
  editCanvas.addEventListener('pointermove',event => {
    if(!editPointer)return;const p=cropPoint(event);editCrop={x:Math.min(p.x,editPointer.x),y:Math.min(p.y,editPointer.y),w:Math.max(1,Math.abs(p.x-editPointer.x)),h:Math.max(1,Math.abs(p.y-editPointer.y))};drawEdit();
  });
  for(const event of ['pointerup','pointercancel','lostpointercapture']) editCanvas.addEventListener(event,() => {editPointer=null;});
  $('quick-edit-apply').addEventListener('click',() => safe(async () => {
    if(editBusy || !editBitmap)return;
    editBusy=true;adding=true;
    $('quick-image-edit').querySelectorAll('button,input').forEach(n => {n.disabled=true;});
    try {
      // Render a clean rotated surface; the visible canvas contains the crop overlay.
      const surface=document.createElement('canvas');surface.width=editCanvas.width;surface.height=editCanvas.height;
      const c=surface.getContext('2d');
      if(editRotation===1)c.translate(editBitmap.height,0);
      if(editRotation===2)c.translate(editBitmap.width,editBitmap.height);
      if(editRotation===3)c.translate(0,editBitmap.width);
      c.rotate(editRotation*Math.PI/2);c.drawImage(editBitmap,0,0);
      const output=document.createElement('canvas');output.width=editCrop.w;output.height=editCrop.h;
      output.getContext('2d').drawImage(surface,editCrop.x,editCrop.y,editCrop.w,editCrop.h,0,0,editCrop.w,editCrop.h);
      const blob=await new Promise(resolve => output.toBlob(resolve,'image/jpeg',.92));
      if(!blob || blob.size>5*1024*1024)throw new Error('处理后图片超过 5 MiB，请缩小选区');
      const id=uuid(), index=current.photos.indexOf(viewedPhoto.id);
      if(index<0)throw new Error('图片已改变，请重新打开');
      current.photos[index]=id;
      let saveError;
      try {await persist({key:key(id),id,workspace:context.workspace,blob,uploaded:false});} catch(e) {saveError=e;}
      await paintPhotos();
      editBusy=false;cancelEdit();viewer.close();finishViewer();
      showPhoto({blob},id,index,current);
      $('quick-photo-status').textContent=localFailed ? '处理后的图片暂留本页，请关闭大图后重试本机保存或导出恢复文件' : '编辑后的图片已保存在本机';
      if(saveError)throw saveError;
    } finally {
      editBusy=false;adding=false;
      $('quick-image-edit').querySelectorAll('button,input').forEach(n => {n.disabled=false;});
    }
  }));
  for(const [id,offset] of [['prev',-1],['next',1]]) $('quick-viewer-'+id).addEventListener('click',() => safe(async () => {
    const index=current.photos.indexOf(viewedPhoto.id)+offset,id=current.photos[index];
    const photo=pendingPhotos.get(key(id)) || await get('photos',key(id));
    if(!photo?.blob)throw new Error('该图片尚未下载，请联网后查看');
    closeViewer();showPhoto(photo,id,index,current);
  }));
  let swipeStart;
  viewerStage.addEventListener('touchstart',e => {swipeStart=e.touches[0];},{passive:true});
  viewerStage.addEventListener('touchend',e => {
    if(zoom!==1 || !swipeStart)return;const end=e.changedTouches[0],dx=end.clientX-swipeStart.clientX,dy=end.clientY-swipeStart.clientY;
    if(Math.abs(dx)>70 && Math.abs(dx)>Math.abs(dy)*1.5) $('quick-viewer-'+(dx<0?'next':'prev')).click();swipeStart=null;
  },{passive:true});
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
    $('quick-sync').textContent = localFailed ? '重试本机保存并同步' : '立即同步';
    if (localFailed) return status('本机保存失败 · 请导出恢复文件并保留本页');
    if (current?.conflict) return status('同步失败 · 本机内容保留，云端有更新');
    if (syncing) return status('同步中');
    if (syncFailed) return status('同步失败 · 本机内容保留');
    status(current?.dirty ? '草稿已保存 · 仅本地保存' : current?.revision ? '已同步' : '可以开始记录');
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
    const note = {...current,photos:[...current.photos]}, generation = ++photoPaint, photos = [];
    for (const [index,id] of note.photos.entries()) {
      const photo = pendingPhotos.get(key(id)) || await get('photos',key(id));
      if (current.id !== note.id || generation !== photoPaint || current.photos.length !== note.photos.length
          || current.photos.some((id,index) => id !== note.photos[index])) return;
      photos.push({id,index,photo});
    }
    urls.forEach(url => URL.revokeObjectURL(url)); urls = [];
    $('quick-photos').replaceChildren();
    for (const {id,index,photo} of photos) {
      const li = document.createElement('li'), img = document.createElement('img');
      const preview = document.createElement('button'), caption = document.createElement('span');
      preview.type = 'button'; preview.className = 'quick-photo-open'; preview.dataset.photoId = id;
      preview.setAttribute('aria-haspopup','dialog');
      preview.setAttribute('aria-label','查看图片 '+(index+1)+' 大图');
      img.alt = '图片 ' + (index+1);
      if (photo?.blob) {
        const url = URL.createObjectURL(photo.blob); urls.push(url); img.src = url;
        caption.textContent = '查看大图';
        preview.addEventListener('click',() => showPhoto(photo,id,index,note));
      } else {
        caption.textContent = note.photo_meta?.find(p => p.id===id)?.purged_at ? '已确认清理' : '联网后可查看';
        preview.disabled = true;
      }
      preview.append(img,caption); li.append(preview);
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
    renderBooks();
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
    pendingSaves++;
    for (const [name,input] of Object.entries(fields)) current[name] = input.value;
    const expected = current.localVersion;
    current.localVersion++; current.dirty = true; current.updated_at = Date.now();
    const value = clone(current);
    if (photo) pendingPhotos.set(photo.key,{...photo,note_id:value.id});
    const volatile = [...pendingPhotos.values()].filter(p => p.note_id===value.id);
    status('正在本地保存…');
    saving = saving.catch(() => {}).then(() => transaction(['notes','photos','recoveries','meta'],'readwrite',async tx => {
      const store = tx.objectStore('notes'); const old = await request(store.get(key(value.id)));
      for (const pending of volatile) tx.objectStore('photos').put(pending);
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
      } else { localFailed = false; volatile.forEach(p => pendingPhotos.delete(p.key)); }
      displayStatus(); await list();
      if (success) scheduleSync();
    }).catch(e => {
      localFailed = true;
      if (current.id===value.id && current.localVersion===value.localVersion) current.localVersion=expected;
      error('本机保存失败：'+e.message+'。请保留本页，腾出空间后重试；也可导出含本页照片的恢复文件。');
      displayStatus();throw e;
    }).finally(() => { pendingSaves--; });
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
        if (!photo?.uploaded && !await get('meta','uploaded:'+key(id))) {
          if (!photo?.blob) {
            if (note.photo_meta?.some(p => p.id===id)) continue;
            throw new Error('本机缺少来源图片，请导入恢复文件');
          }
          await api(endpoint(note.id)+'/photos/'+id,undefined,photo.blob);
          // Keep the Blob record immutable. Rewriting an IndexedDB Blob after
          // upload can invalidate an already rendered Blob URL in WebKit.
          await put('meta',{key:'uploaded:'+key(id),value:true});
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
  async function pendingBooks(workspace=context?.workspace) { return workspace ? (await all('meta')).filter(m => m.key.startsWith('book:'+workspace+':')).map(m => m.value) : []; }
  function renderBooks() {
    const selected=current?.book_id || $('quick-book').value;
    const query=$('quick-book-search').value.trim().toLocaleLowerCase();
    $('quick-book').replaceChildren();
    for(const book of context.books) {
      if(book.id!==selected && query && !(book.title+' '+book.author).toLocaleLowerCase().includes(query))continue;
      const option=document.createElement('option');option.value=book.id;option.textContent=book.title+(book.author?' · '+book.author:'');$('quick-book').append(option);
    }
    if(selected)$('quick-book').value=selected;
  }
  async function selectBook(id) {
    if(adding)throw new Error('正在保存图片，请稍后切换书籍');
    await saving;
    current.book_id=id;await put('meta',{key:'lastBook:'+context.workspace,value:id});
    renderBooks();
    if(current.localVersion || current.photos.length || Object.values(fields).some(f => f.value))await persist();
  }
  let creatingBook=false;
  async function createBook() {
    if(creatingBook)return;
    if(adding)throw new Error('正在保存图片，请稍后建书');
    const title=$('quick-book-title').value.trim(),author=$('quick-book-author').value.trim();
    if(!title)throw new Error('请输入书名');
    creatingBook=true;$('quick-book-create').disabled=true;
    try {
    const book={id:uuid(),title,author};
    await put('meta',{key:'book:'+context.workspace+':'+book.id,value:book});
    context.books.unshift(book);await put('meta',{key:'context',value:{...context,csrf:''}});
    await selectBook(book.id);$('quick-create-book').open=false;$('quick-book-form').reset();scheduleSync();
    } finally {creatingBook=false;$('quick-book-create').disabled=false;}
  }
  async function refreshContext() {
    const remote = await (await api('/api/paper/context')).json();
    if (context && context.workspace !== remote.workspace) throw new Error('登录账号或服务器已改变。请先导出本机恢复文件，重新打开页面选择正确资料库。');
    remote.books.push(...(await pendingBooks(remote.workspace)).filter(b => !remote.books.some(r => r.id===b.id)));
    context = remote;
    renderBooks();
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
    syncing = true; displayStatus();
    const run = async () => {
      await refreshContext();
      for(const book of await pendingBooks()) {
        await api('/api/paper/books/'+book.id,{title:book.title,author:book.author});
        await transaction(['meta'],'readwrite',tx => {tx.objectStore('meta').delete('book:'+context.workspace+':'+book.id);});
      }
      for (let pass=0;pass<3;pass++) {
        const notes = (await all('notes')).filter(n => n.workspace === context.workspace && n.book_id && n.dirty && !n.conflict);
        if (!notes.length) break;
        for (const note of notes) await syncOne(note);
      }
      await pull(); syncFailed = false; error();
    };
    try {
      await saving;
      if(localFailed)return;
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
      if(parent!==window && !current.dirty)parent.postMessage({type:'noteshub-record-synced',id:current.id},location.origin);
    }
  }
  async function prepare(file) {
    if (file.size > 20*1024*1024) throw new Error('来源图片超过 20 MiB，请缩小后选择');
    const bitmap = await createImageBitmap(file,{imageOrientation:'from-image'});
    try {
      if (bitmap.width*bitmap.height > 24000000) throw new Error('来源图片超过 2400 万像素');
      const scale = Math.min(1,2400/Math.max(bitmap.width,bitmap.height));
      const canvas = document.createElement('canvas'); canvas.width = Math.max(1,Math.round(bitmap.width*scale)); canvas.height = Math.max(1,Math.round(bitmap.height*scale));
      const drawing=canvas.getContext('2d');
      drawing.fillStyle='#fff';drawing.fillRect(0,0,canvas.width,canvas.height);
      drawing.drawImage(bitmap,0,0,canvas.width,canvas.height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve,'image/jpeg',.9));
      if (!blob || blob.size > 5*1024*1024) throw new Error('图片处理失败或仍超过 5 MiB，请缩小后选择');
      return blob;
    } finally { bitmap.close(); }
  }
  async function addFiles(input) {
    if(adding)throw new Error('正在处理图片，请稍后追加');
    const files = [...input.files]; input.value = '';
    adding = true;
    $('quick-camera').disabled = $('quick-files').disabled = $('quick-book').disabled = $('quick-new').disabled = true;
    try {
      for (const [index,file] of files.entries()) {
        if (current.photos.length >= 20) throw new Error('每条记录最多 20 张图片，可新建下一条');
        $('quick-photo-status').textContent = '正在本机保存图片 '+(index+1)+' / '+files.length;
        const blob = await prepare(file), id = uuid();
        current.photos.push(id);
        try { await persist({key:key(id),id,workspace:context.workspace,blob,uploaded:false}); }
        catch(e) { await paintPhotos();throw e; }
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
      const photo = pendingPhotos.get(key(id)) || await get('photos',key(id));
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
    const photos = [...new Map([...(await all('photos')).map(p => [p.key,p]),...pendingPhotos.entries()]).values()], encoded = [];
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
    for(const entry of data.meta || []) {
      const book=entry.value;
      if(typeof entry.key==='string' && entry.key.startsWith('book:'+context.workspace+':') && book && /^[0-9a-f]{32}$/.test(book.id)
        && typeof book.title==='string' && book.title.trim() && book.title.length<=200 && typeof book.author==='string' && book.author.length<=200
        && !context.books.some(b => b.id===book.id)) {
        await put('meta',{key:'book:'+context.workspace+':'+book.id,value:book});context.books.push(book);
      }
    }
    renderBooks();

    // Import as fresh notes: never overwrite this device's or the server's IDs.
    const sources = new Map();
    for (const note of [...data.notes,...(data.recoveries || []).map(r => r.note),...(data.unsaved ? [data.unsaved] : [])]) {
      if (!note.first_comment && !note.original_text && !note.second_comment && !note.photos?.length) continue;
      const identity=JSON.stringify([note.workspace,note.id,note.first_comment,note.original_text,note.second_comment,note.photos]);
      sources.set(identity,note);
    }
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
    for(const book of await pendingBooks())if(!context.books.some(b => b.id===book.id))context.books.push(book);
    renderBooks();
    if(location.hash==='#quick-ocr-section')$('quick-ocr-section').open=true;
    if(new URLSearchParams(location.search).get('view')==='ocr') {document.body.classList.add('quick-ocr-view');$('quick-ocr-section').open=true;}
    if (navigator.onLine) { await pull().catch(() => {}); }
    const params = new URLSearchParams(location.search), noteId=params.get('note') || (!params.has('book') && (await get('meta','lastNote:'+context.workspace))?.value);
    if (params.has('note') && !await get('notes',key(noteId))) {
      const remote = await (await api(endpoint(noteId))).json();
      await put('notes',{...applyServer(null,remote),key:key(remote.id)});
    }
    if (noteId && await get('notes',key(noteId))) await open(noteId);
    else {
      const book=params.get('book') || (await get('meta','lastBook:'+context.workspace))?.value;
      await newNote(context.books.some(b => b.id===book) ? book : context.books[0]?.id || '');
    }
    for (const input of Object.values(fields)) input.addEventListener('input',() => safe(() => persist()));
    $('quick-book').addEventListener('change',() => safe(() => selectBook($('quick-book').value)));
    $('quick-book-search').addEventListener('input',renderBooks);
    $('quick-book-form').addEventListener('submit',e => {e.preventDefault();safe(createBook);});
    $('quick-book-create').disabled=false;
    document.addEventListener('paste',e => {
      const files=[...(e.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
      if(!files.length || e.clipboardData.getData('text/plain'))return;
      e.preventDefault();safe(() => addFiles({files,value:''}));
    });
    $('quick-new').addEventListener('click',() => safe(() => newNote()));
    $('quick-sync').addEventListener('click',() => safe(async () => { if (localFailed) await persist();await sync(); }));
    $('quick-copy').addEventListener('click',() => safe(copyNote));
    for (const id of ['quick-camera','quick-files']) $(id).addEventListener('change',() => safe(() => addFiles($(id))));
    $('quick-backup').addEventListener('click',() => safe(downloadBackup));
    $('quick-restore').addEventListener('change',() => safe(() => restore($('quick-restore').files[0])));
    window.addEventListener('online',() => safe(sync));
    document.addEventListener('visibilitychange',() => { if (document.visibilityState === 'visible') safe(sync); });
    setInterval(() => { if (document.visibilityState === 'visible') safe(sync); },30000);
    window.addEventListener('beforeunload',event => { if (localFailed || adding || pendingSaves) { event.preventDefault(); event.returnValue=''; } });
    await list();
    ready = true;
    for (const input of [...Object.values(fields),$('quick-camera'),$('quick-files'),$('quick-book'),$('quick-new'),$('quick-sync')]) input.disabled=false;
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/quick-sw.js',{scope:'/quick'}).catch(() => error('离线页面尚未准备完成，请联网重新打开本页后再离线使用'));
    const persisted = await navigator.storage?.persist?.().catch(() => false);
    $('quick-storage').textContent = persisted ? '浏览器已授予持久存储。' : '浏览器未承诺永久保留本机存储，请及时同步或导出本机恢复文件。';
    safe(sync);
  }
  // Small integration surface for the optional desktop OCR/confirmation panel.
  window.NotesHubQuick = {api,endpoint,getContext:() => context,getCurrent:() => current,
    save:async values => { Object.entries(values).forEach(([name,value]) => { if (fields[name]) fields[name].value=value; }); await persist(); },
    sync,flush:async () => {await saving;if(localFailed || adding)throw new Error('本机保存尚未完成');await sync();},open:async () => open(current.id),safe,error};
  safe(initialize);
})();
