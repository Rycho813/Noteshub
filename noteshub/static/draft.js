/* Ordered draft saves; OCR polling is read-only and never resubmits a job. */
(() => {
  "use strict";
  const editor = document.getElementById("draft-editor");
  if (!editor) return;
  const $ = id => document.getElementById(id);
  const original = $("draft-original"), comment = $("draft-comment");
  const saveButton = $("draft-save"), recognize = $("draft-recognize"), archiveButton = $("draft-archive");
  const saveStatus = $("draft-save-status"), ocrStatus = $("draft-ocr-status");
  let state = null, revision = 0, saved = {original_text: original.value, first_comment: comment.value};
  let ready = false, inFlight = null, uncertain = null, moving = false, conflict = false;
  let timer = null, pollTimer = null, pollFailures = 0, polling = false;
  const composing = new Set();
  const snapshot = () => ({original_text: original.value, first_comment: comment.value});
  const same = (a,b) => a.original_text === b.original_text && a.first_comment === b.first_comment;
  const dirty = () => Boolean(uncertain) || !same(snapshot(), saved);
  function message(text, error = false) {
    saveStatus.textContent = text;
    saveStatus.classList.toggle("save-error", error);
  }
  function controls() {
    const firstRecognition = state?.ocr_state === "running" && revision === 0;
    const archived = Boolean(state?.archived);
    for (const field of [original,comment]) { field.disabled = !ready; field.readOnly = archived || moving || firstRecognition; }
    saveButton.disabled = archived || !ready || moving || firstRecognition;
    saveButton.textContent = uncertain ? "重试保存" : "立即保存";
    recognize.disabled = archived || !ready || moving || Boolean(inFlight) || state?.ocr_state === "running" || conflict || editor.dataset.configured !== "true";
    recognize.textContent = !state?.run_id ? "识别文字" : state.ocr_state === "failed" ? "重试识别（可能计费）" : "重新识别（可能计费）";
    $("draft-adopt").disabled = archived || !ready || moving || Boolean(inFlight) || state?.ocr_state === "running" || conflict;
    archiveButton.disabled = archived || !ready || moving || Boolean(inFlight) || state?.ocr_state === "running" || conflict;
    archiveButton.textContent = archived ? "已归档" : "完成一读并归档";
  }
  async function request(url, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url,{...options, signal:controller.signal, credentials:"same-origin", redirect:"error"});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) $("draft-login-hint").hidden = false;
        const error = new Error(data.detail || "请求未完成，请稍后重试。");
        error.status = response.status;
        throw error;
      }
      return data;
    } finally { clearTimeout(timeout); }
  }
  async function post(suffix, values = {}) {
    const context = await request(editor.dataset.context);
    // Reauthentication in another tab must not require losing local text.
    document.querySelectorAll('input[name="csrf"]').forEach(input => { input.value = context.csrf; });
    return request(editor.dataset.api + suffix,{method:"POST", body:new URLSearchParams({csrf:context.csrf,...values})});
  }
  function showOCR(next) {
    state = next;
    const names = {idle:"尚未识别，可以先手动填写。",running:"正在识别…可离开后回来查看，刷新页面不会再次调用 OCR。",succeeded:"识别成功，请对照图片检查原文。",failed:"识别失败：" + next.error};
    ocrStatus.textContent = names[next.ocr_state] || "识别状态未知，请刷新状态。";
    ocrStatus.classList.toggle("save-error",next.ocr_state === "failed");
    $("draft-first").hidden = next.first_ocr_text === null;
    $("draft-first-text").textContent = next.first_ocr_text || "";
    $("draft-latest").hidden = next.latest_ocr_text === null;
    $("draft-latest-text").textContent = next.latest_ocr_text || "";
    $("draft-scope").hidden = next.archived;
    const link = $("draft-note-link");
    link.hidden = !next.archived;
    if (next.archived) {
      link.setAttribute("href",next.note_url);
      $("draft-archive-status").textContent = "已归档 · " + (next.note_status === "done" ? "二读已完成" : "待二读") + "。来源图片与识别记录长期保留，原文和一读只读。";
    }
    controls();
  }
  function apply(next, force = false) {
    if (force || (!dirty() && !inFlight)) {
      original.value = next.original_text; comment.value = next.first_comment;
      saved = {original_text:next.original_text,first_comment:next.first_comment};
      revision = next.revision;
    } else if (!inFlight && next.revision !== revision) {
      conflict = true;
      clearTimeout(timer);
      message(next.archived ? "草稿已在另一页面归档，当前未保存输入未写入正式笔记，请复制留存后查看。" : "服务器草稿已有更新，当前输入未覆盖。请复制未保存内容，再刷新核对。",true);
    }
    showOCR(next);
  }
  async function refresh() {
    if (polling) return;
    polling = true;
    clearTimeout(pollTimer);
    try {
      const next = await request(editor.dataset.api);
      apply(next);
      pollFailures = 0;
      if (next.ocr_state === "running") pollTimer = setTimeout(() => refresh().catch(() => {}),1500);
    } catch (error) {
      pollFailures++;
      ocrStatus.textContent = "状态读取失败，点击“刷新识别状态”继续查询，不会重新提交识别。";
      if (state?.ocr_state === "running" && pollFailures < 3 && ![401,403,404].includes(error.status)) {
        pollTimer = setTimeout(() => refresh().catch(() => {}),3000);
      }
      throw error;
    } finally { polling = false; }
  }
  async function drain() {
    if (!ready) throw new Error("编辑器尚未就绪。");
    if (conflict) throw new Error("请先复制当前输入并刷新，核对另一个页面的修改。");
    while (dirty()) {
      if (uncertain) {
        // A lost response may follow a committed save. Reconcile its exact
        // snapshot before sending later input with a stale revision.
        const remote = await request(editor.dataset.api);
        if (same(remote,uncertain)) {
          saved = {...uncertain}; revision = remote.revision; uncertain = null;
        } else if (remote.revision === revision) {
          uncertain = null;
        } else {
          conflict = true;
          throw new Error("草稿已在其他页面更新，请复制当前输入并刷新核对。");
        }
        showOCR(remote);
        if (!dirty()) break;
      }
      const value = snapshot();
      message("保存中…");
      try {
        const next = await post("/save",{...value,revision:String(revision)});
        if (!same(next,value) || !Number.isInteger(next.revision)) throw new Error("未能确认保存结果。");
        saved = value; revision = next.revision;
        $("draft-login-hint").hidden = true;
        showOCR(next);
      } catch (error) {
        uncertain = value;
        if (error.status === 409) conflict = true;
        throw error;
      }
    }
    message("已保存");
  }
  function flush() {
    clearTimeout(timer);
    if (!inFlight) {
      inFlight = drain().catch(error => {
        message(error.name === "AbortError" ? "保存超时，输入仍在本页，请重试。" : "保存失败："+error.message+" 输入仍在本页。",true);
        throw error;
      }).finally(() => { inFlight = null; controls(); });
    }
    controls();
    return inFlight;
  }
  function schedule() {
    clearTimeout(timer);
    message("尚未保存");
    if (!composing.size && !conflict) timer = setTimeout(() => flush().catch(() => {}),500);
  }
  for (const field of [original,comment]) {
    field.addEventListener("input",schedule);
    field.addEventListener("compositionstart",() => { composing.add(field); clearTimeout(timer); });
    field.addEventListener("compositionend",() => { composing.delete(field); schedule(); });
  }
  saveButton.addEventListener("click",() => flush().catch(() => {}));
  async function action(callback) {
    if (moving || !ready) return;
    moving = true; controls();
    try { await callback(); }
    catch (error) { if (!dirty()) message(error.message,true); }
    finally { moving = false; controls(); }
  }
  recognize.addEventListener("click",() => action(async () => {
    const previous = state.run_id;
    if (previous && !window.confirm("将再次提交腾讯 OCR，可能再次计费。校对原文和一读笔记不会被覆盖。确认继续？")) return;
    await flush();
    try {
      await post("/ocr",{previous_run_id:previous});
      await refresh();
    } catch (error) {
      // Reading status is safe even if the submission reply was lost.
      await refresh().catch(() => {});
      if (state?.run_id === previous && state?.ocr_state !== "running") {
        ocrStatus.textContent = error.message + " 请先刷新识别状态，系统不会自动重复提交。";
      }
    }
  }));
  $("draft-refresh").addEventListener("click",() => refresh().catch(() => {}));
  $("draft-adopt").addEventListener("click",() => action(async () => {
    const text = state.latest_ocr_text;
    if (text === null) return;
    if (original.value && original.value !== text && !window.confirm("用这份识别结果替换当前原文？一读笔记不会改变。")) return;
    await flush();
    original.value = text;
    await flush();
  }));
  archiveButton.addEventListener("click",() => action(async () => {
    if (!original.value.trim()) {
      $("draft-archive-status").textContent = "请先填写或校对原文，原文不能为空。";
      return;
    }
    await flush();
    try {
      const next = await post("/archive",{revision:String(revision)});
      if (!next.archived || !next.note_url) throw new Error("归档结果未确认，请刷新状态核对。");
      apply(next,true);
      message("已归档");
    } catch (error) {
      // A committed archive whose reply was lost is recovered by a safe GET.
      await refresh().catch(() => {});
      if (state?.archived) message("已归档");
      else $("draft-archive-status").textContent = "归档未确认：" + error.message + " 已保存草稿仍保留，可刷新状态或重试归档。";
    }
  }));
  document.addEventListener("click",event => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.target === "_blank" || !ready) return;
    event.preventDefault();
    action(async () => { await flush(); window.location.assign(link.href); });
  });
  const logout = document.querySelector('.site-header form[action="/logout"]');
  logout.addEventListener("submit",event => {
    if (!ready) return;
    event.preventDefault();
    action(async () => {
      await flush();
      const context = await request(editor.dataset.context);
      logout.querySelector('input[name="csrf"]').value = context.csrf;
      HTMLFormElement.prototype.submit.call(logout);
    });
  });
  window.addEventListener("beforeunload",event => {
    if (dirty() || inFlight) { event.preventDefault(); event.returnValue = ""; }
  });
  document.addEventListener("visibilitychange",() => { if (document.visibilityState === "hidden" && ready && dirty() && !composing.size) flush().catch(() => {}); });
  window.addEventListener("online",() => { if (ready && dirty()) flush().catch(() => {}); if (state?.ocr_state === "running") refresh().catch(() => {}); });
  async function initialize() {
    try {
      const next = await post("/open");
      apply(next,true); ready = true; message("已保存"); controls();
      if (next.ocr_state === "running") await refresh();
    } catch (error) { message("编辑器未就绪："+error.message+" 请重试打开本页。",true); }
  }
  initialize();
})();
