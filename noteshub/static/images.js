/* Local image preparation only. No clipboard polling, external code or OCR. */
(() => {
  "use strict";
  const root = document.getElementById("image-input");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const canvas = $("crop-canvas"), ctx = canvas.getContext("2d");
  const editor = $("image-editor"), result = $("image-result");
  const drop = $("image-drop"), message = $("image-message");
  const inputs = [$("image-file"), $("image-camera")];
  const fields = [$("crop-x"), $("crop-y"), $("crop-w"), $("crop-h")];
  let image = null, rotation = 0, format = "image/png", crop = null;
  let width = 0, height = 0, scale = 1, pointer = null;
  let output = null, outputURL = null, busy = false, dirty = false;
  let sourceURL = null, panning = false;
  const say = (text, error = false) => {
    message.textContent = text;
    message.classList.toggle("save-error", error);
  };
  function working(value) {
    busy = value;
    root.querySelectorAll("button, input").forEach((node) => { node.disabled = value; });
  }
  function clearOutput() {
    output = null;
    if (outputURL) URL.revokeObjectURL(outputURL);
    outputURL = null;
    $("image-result-preview").removeAttribute("src");
    $("image-result-link").removeAttribute("href");
    result.hidden = true;
  }
  function releaseImage() {
    if (image && image.close) image.close();
    image = null;
    if (sourceURL) URL.revokeObjectURL(sourceURL);
    sourceURL = null;
  }
  function oriented(context) {
    context.save();
    if (rotation === 1) context.translate(image.height, 0);
    if (rotation === 2) context.translate(image.width, image.height);
    if (rotation === 3) context.translate(0, image.width);
    context.rotate(rotation * Math.PI / 2);
    context.drawImage(image, 0, 0);
    context.restore();
  }
  function draw() {
    if (!image) return;
    width = rotation % 2 ? image.height : image.width;
    height = rotation % 2 ? image.width : image.height;
    const available = Math.max(180, canvas.parentElement.clientWidth);
    scale = Math.min(available / width, 900 / height, 1) * Number($("image-zoom").value);
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    // Coordinate calculations use actual rounded canvas dimensions.
    ctx.setTransform(canvas.width / width, 0, 0, canvas.height / height, 0, 0);
    oriented(ctx);
    const {x, y, w, h} = crop;
    ctx.fillStyle = "rgba(0,0,0,.48)";
    ctx.fillRect(0, 0, width, y);
    ctx.fillRect(0, y + h, width, height - y - h);
    ctx.fillRect(0, y, x, h);
    ctx.fillRect(x + w, y, width - x - w, h);
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2 / scale;
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = "#ffffff";
    const handle = 10 / scale;
    [[x, y], [x+w, y], [x, y+h], [x+w, y+h]].forEach(([hx, hy]) => {
      ctx.fillRect(hx-handle/2, hy-handle/2, handle, handle);
    });
    fields.forEach((field, index) => { field.value = Math.round([x, y, w, h][index]); });
    fields[0].max = width - 1; fields[1].max = height - 1;
    fields[2].max = width; fields[3].max = height;
    $("crop-size").textContent = `选区 ${Math.round(w)} × ${Math.round(h)} 像素；整图 ${width} × ${height} 像素。`;
  }
  function resetCrop() {
    width = rotation % 2 ? image.height : image.width;
    height = rotation % 2 ? image.width : image.height;
    crop = {x: 0, y: 0, w: width, h: height};
    clearOutput();
    editor.hidden = false;
    draw();
  }
  // Inspect dimensions before asking the browser to decode a potentially huge
  // file. Browser decoding applies JPEG EXIF orientation once, before cropping.
  async function header(file) {
    const data = new DataView(await file.slice(0, 1024 * 1024).arrayBuffer());
    if (data.byteLength >= 24 && data.getUint32(0) === 0x89504e47 && data.getUint32(4) === 0x0d0a1a0a) {
      return ["image/png", data.getUint32(16), data.getUint32(20)];
    }
    if (data.byteLength > 4 && data.getUint16(0) === 0xffd8) {
      let i = 2;
      while (i + 3 < data.byteLength) {
        if (data.getUint8(i++) !== 0xff) break;
        while (i < data.byteLength && data.getUint8(i) === 0xff) i++;
        if (i + 2 >= data.byteLength) break;
        const marker = data.getUint8(i++);
        if (marker === 0xd9 || marker === 0xda) break;
        if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
        const size = data.getUint16(i);
        if (size < 2 || i + size > data.byteLength) break;
        if ([0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf].includes(marker) && size >= 7) {
          return ["image/jpeg", data.getUint16(i+5), data.getUint16(i+3)];
        }
        i += size;
      }
    }
    throw new Error("只支持 PNG/JPEG 图片。HEIC、WebP 或其他格式请先转换；也请检查图片是否损坏。");
  }
  async function load(file) {
    if (busy) return;
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) return say("原图超过 20 MiB，请先缩小或截图后重试。", true);
    working(true);
    say("正在本机读取图片，尚未上传…");
    let next = null, nextURL = null;
    try {
      const [type, w, h] = await header(file);
      if (!w || !h || Math.max(w,h) > 12000 || w*h > 24_000_000) {
        throw new Error("原图不能超过 2400 万像素，且任一边不能超过 12000 像素。请先截图或缩小。");
      }
      if (window.createImageBitmap) {
        try { next = await createImageBitmap(file, {imageOrientation: "from-image"}); } catch (_) { /* Use image decoding fallback below. */ }
      }
      if (!next) {
        nextURL = URL.createObjectURL(file);
        next = new Image();
        await new Promise((resolve, reject) => {
          next.onload = resolve;
          next.onerror = () => reject(new Error("无法读取图片，请重新选择 PNG/JPEG。"));
          next.src = nextURL;
        });
        // HTMLImageElement's width/height initially equal natural dimensions.
      }
      releaseImage(); clearOutput();
      image = next; sourceURL = nextURL; next = null; nextURL = null;
      format = type; rotation = 0; dirty = true;
      $("image-zoom").value = 1;
      $("image-discard").hidden = false;
      resetCrop();
      say("图片仅在本机。请框选需要的文字，或直接使用整张图片。");
    } catch (error) {
      if (next && next.close) next.close();
      if (nextURL) URL.revokeObjectURL(nextURL);
      say(error.message, true);
    } finally {
      inputs.forEach((input) => { input.value = ""; });
      working(false);
    }
  }
  inputs.forEach((input) => input.addEventListener("change", () => load(input.files[0])));
  function editable(target) {
    return target instanceof Element && Boolean(target.closest("input, textarea, [contenteditable]:not([contenteditable='false'])"));
  }
  // Restrict to this workspace and preserve paste in all editable fields.
  root.addEventListener("paste", (event) => {
    if (editable(event.target)) return;
    const files = Array.from(event.clipboardData?.files || []);
    if (!files.length) return;
    event.preventDefault();
    if (files.length !== 1) return say("一次请选择或粘贴一张图片。", true);
    load(files[0]);
  });
  drop.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("dragging"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault(); drop.classList.remove("dragging");
    if (event.dataTransfer.files.length !== 1) return say("一次请拖入一张图片。", true);
    load(event.dataTransfer.files[0]);
  });
  function position(event) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(width, (event.clientX - rect.left) * width / rect.width)),
      y: Math.max(0, Math.min(height, (event.clientY - rect.top) * height / rect.height))
    };
  }
  canvas.addEventListener("pointerdown", (event) => {
    if (!image || busy || panning || (event.pointerType === "mouse" && event.button !== 0) || pointer) return;
    event.preventDefault();
    const p = position(event), radius = 18 / scale;
    let corner = null;
    for (const [name, x, y] of [["nw",crop.x,crop.y], ["ne",crop.x+crop.w,crop.y], ["sw",crop.x,crop.y+crop.h], ["se",crop.x+crop.w,crop.y+crop.h]]) {
      if (Math.abs(p.x-x) < radius && Math.abs(p.y-y) < radius) { corner = name; break; }
    }
    const inside = p.x > crop.x && p.x < crop.x+crop.w && p.y > crop.y && p.y < crop.y+crop.h;
    const whole = crop.x === 0 && crop.y === 0 && crop.w === width && crop.h === height;
    pointer = {id: event.pointerId, start: p, original: {...crop}, mode: corner || (inside && !whole ? "move" : "new")};
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!pointer || event.pointerId !== pointer.id) return;
    const p = position(event), o = pointer.original;
    if (pointer.mode === "move") {
      crop = {x: Math.max(0, Math.min(width-o.w, o.x+p.x-pointer.start.x)), y: Math.max(0, Math.min(height-o.h, o.y+p.y-pointer.start.y)), w: o.w, h: o.h};
    } else {
      let anchor = pointer.start;
      if (pointer.mode !== "new") anchor = {
        x: pointer.mode.includes("w") ? o.x+o.w : o.x,
        y: pointer.mode.includes("n") ? o.y+o.h : o.y
      };
      crop = {x: Math.min(anchor.x,p.x), y: Math.min(anchor.y,p.y), w: Math.max(1,Math.abs(anchor.x-p.x)), h: Math.max(1,Math.abs(anchor.y-p.y))};
      crop.x = Math.min(crop.x,width-crop.w); crop.y = Math.min(crop.y,height-crop.h);
    }
    draw();
  });
  function finishPointer(event) {
    if (pointer && event.pointerId === pointer.id) {
      crop = {x: Math.round(crop.x), y: Math.round(crop.y), w: Math.max(1,Math.round(crop.w)), h: Math.max(1,Math.round(crop.h))};
      crop.w = Math.min(crop.w,width-crop.x); crop.h = Math.min(crop.h,height-crop.y);
      pointer = null; draw();
    }
  }
  canvas.addEventListener("pointerup", finishPointer);
  canvas.addEventListener("pointercancel", finishPointer);
  canvas.addEventListener("lostpointercapture", finishPointer);
  fields.forEach((field) => field.addEventListener("change", () => {
    if (!image) return;
    const [x,y,w,h] = fields.map((input) => Number(input.value));
    if (![x,y,w,h].every(Number.isFinite)) return draw();
    crop = {x: Math.max(0,Math.min(width-1,Math.round(x))), y: Math.max(0,Math.min(height-1,Math.round(y))), w: 1, h: 1};
    crop.w = Math.max(1,Math.min(width-crop.x,Math.round(w)));
    crop.h = Math.max(1,Math.min(height-crop.y,Math.round(h)));
    clearOutput(); draw();
  }));
  $("image-zoom").addEventListener("input", draw);
  $("image-pan").addEventListener("click", () => {
    panning = !panning;
    canvas.classList.toggle("crop-pan", panning);
    $("image-pan").setAttribute("aria-pressed", String(panning));
    $("image-pan").textContent = panning ? "返回框选模式" : "拖动查看放大图";
  });
  $("image-whole").addEventListener("click", resetCrop);
  $("image-rotate").addEventListener("click", () => { rotation = (rotation+1)%4; resetCrop(); });
  window.addEventListener("resize", draw);
  const blobFrom = (surface, type, quality) => new Promise((resolve, reject) => {
    surface.toBlob((blob) => blob ? resolve(blob) : reject(new Error("浏览器无法生成图片，请缩小范围后重试。")), type, quality);
  });
  $("image-preview-button").addEventListener("click", async () => {
    if (!image || busy) return;
    working(true); say("正在本机生成预览…");
    const surface = document.createElement("canvas");
    try {
      if (Math.max(crop.w,crop.h) > 6000 || crop.w*crop.h > 12_000_000) {
        throw new Error("选区超过 1200 万像素或边长超过 6000 像素，请缩小范围。为保留小字，不会自动降低分辨率。");
      }
      surface.width = crop.w; surface.height = crop.h;
      const target = surface.getContext("2d");
      target.translate(-crop.x,-crop.y); oriented(target);
      const blob = await blobFrom(surface, format, format === "image/jpeg" ? 0.92 : undefined);
      if (blob.size > 5 * 1024 * 1024) throw new Error("选定图片超过 5 MiB，请缩小范围或拆成多张。为保证文字清晰，不会强制压缩。");
      clearOutput(); output = blob; outputURL = URL.createObjectURL(blob);
      $("image-result-preview").src = outputURL;
      $("image-result-link").href = outputURL;
      $("image-output-size").textContent = `${crop.w} × ${crop.h} 像素 · ${Math.ceil(blob.size/1024)} KiB · ${format === "image/png" ? "PNG" : "JPEG"}`;
      result.hidden = false; editor.hidden = true;
      say("请核对预览。点击保存后才会上传，当前尚未识别文字。");
    } catch (error) { say(error.message,true); }
    finally { surface.width = 0; surface.height = 0; working(false); }
  });
  $("image-back").addEventListener("click", () => { clearOutput(); editor.hidden = false; draw(); });
  $("image-discard").addEventListener("click", () => {
    clearOutput(); releaseImage(); dirty = false; pointer = null;
    canvas.width = 0; canvas.height = 0;
    editor.hidden = true; $("image-discard").hidden = true;
    say("已放弃本次选图，未上传的图片不会保留。已保存草稿不受影响。");
  });
  $("image-upload").addEventListener("click", async () => {
    if (!output || busy) return;
    working(true); say("正在保存图片草稿，请勿关闭页面…");
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 60000);
    try {
      // Refresh the token without discarding local image data if the user has
      // reauthenticated in another tab since opening this page.
      const context = await fetch(root.dataset.uploadUrl + "/context", {signal:controller.signal,credentials:"same-origin"});
      if (context.status === 401) {
        $("image-login-hint").hidden = false;
        throw new Error("登录已失效。请在另一页重新登录，再回到这里重试保存。");
      }
      if (!context.ok) throw new Error("无法确认书籍或登录状态，请稍后重试。");
      const credentials = await context.json();
      $("image-login-hint").hidden = true;
      const form = new FormData();
      form.append("csrf",credentials.csrf);
      form.append("file",output,format === "image/png" ? "excerpt.png" : "excerpt.jpg");
      const response = await fetch(root.dataset.uploadUrl,{method:"POST",body:form,signal:controller.signal,credentials:"same-origin"});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || (response.status === 413 ? "图片过大，请缩小范围。" : "保存失败，请重试；若登录已过期，请先在另一页重新登录。"));
      if (!data.location || !data.location.startsWith("/books/")) throw new Error("服务器返回无效，请刷新检查草稿。");
      dirty = false;
      const target = new URL(data.location, window.location.origin);
      if (target.pathname === window.location.pathname) {
        // A hash-only navigation does not fetch the newly saved draft list.
        window.location.hash = target.hash;
        window.location.reload();
      } else {
        window.location.assign(target.href);
      }
    } catch (error) {
      say(error.name === "AbortError" ? "保存超时。图片仍在当前页面，可重试；重复上传不会生成重复草稿。" : error.message,true);
      working(false);
    } finally { clearTimeout(timeout); }
  });
  window.addEventListener("beforeunload", (event) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
  document.querySelectorAll("time[data-expires]").forEach((node) => {
    node.textContent = `${new Date(Number(node.dataset.expires)*1000).toLocaleString()} 到期`;
  });
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); });
  });
})();
