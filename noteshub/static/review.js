/* One active editor, one ordered save stream. No offline storage or merging. */
"use strict";

(() => {
  const editor = document.getElementById("review-editor");
  if (!editor) return;
  const text = document.getElementById("second-comment");
  const output = document.getElementById("save-status");
  const saveButton = document.getElementById("save-now");
  const completeButton = document.getElementById("complete-next");
  const restoreButton = document.getElementById("restore-pending");
  const exportButton = document.getElementById("export-book");
  const loginHint = document.getElementById("login-hint");
  let savedText = text.value;
  let desiredStatus = null;
  let uncertain = false;
  let inFlight = null;
  let timer = null;
  let moving = false;
  let composing = false;

  const dirty = () => uncertain || text.value !== savedText || desiredStatus !== null;
  function message(value, failed = false) {
    output.textContent = value;
    output.classList.toggle("save-error", failed);
  }
  function controls() {
    text.readOnly = moving;
    for (const button of [saveButton, completeButton, restoreButton, exportButton]) {
      button.disabled = moving;
    }
    saveButton.textContent = uncertain ? "重试保存" : "立即保存";
  }
  function progress(result) {
    editor.dataset.status = result.status;
    document.getElementById("note-status").textContent = result.status === "done" ? "已完成" : "待处理";
    restoreButton.hidden = result.status !== "done";
    document.getElementById("book-progress").textContent = `${result.done}/${result.total}`;
    document.getElementById("export-warning").textContent = result.done < result.total
      ? `仍有 ${result.total - result.done} 条待处理；导出会包含全部 ${result.total} 条笔记。`
      : "全部笔记已完成，导出包含整本书。";
  }

  async function drain() {
    while (dirty()) {
      const snapshot = text.value;
      const status = desiredStatus;
      const body = new URLSearchParams({csrf: editor.dataset.csrf, second_comment: snapshot});
      if (status !== null) body.set("status", status);
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      message("保存中…");
      try {
        const response = await fetch(editor.dataset.saveUrl, {
          method: "POST", body, credentials: "same-origin", redirect: "error",
          signal: controller.signal,
          keepalive: new TextEncoder().encode(body.toString()).length < 60000,
        });
        const result = await response.json();
        if (!response.ok) {
          if (response.status === 401 || response.status === 403) loginHint.hidden = false;
          throw new Error(result.detail || "保存失败，请重试。");
        }
        if (result.second_comment !== snapshot || !["pending", "done"].includes(result.status)
            || !Number.isInteger(result.done) || !Number.isInteger(result.total)) {
          throw new Error("未能确认保存结果，请重试。");
        }
        savedText = snapshot;
        uncertain = false;
        if (desiredStatus === status) desiredStatus = null;
        loginHint.hidden = true;
        progress(result);
      } catch (error) {
        // The server may have committed even if its reply was lost. Force a
        // retry even if the user has since reverted to the prior saved value.
        uncertain = true;
        message(error.name === "AbortError" ? "保存超时，文字仍在输入框中，请重试。"
          : `保存失败：${error.message} 文字仍在输入框中。`, true);
        throw error;
      } finally {
        clearTimeout(timeout);
      }
    }
    message("已保存");
  }

  function flush() {
    clearTimeout(timer);
    if (!inFlight) {
      inFlight = drain().finally(() => {
        inFlight = null;
        controls();
      });
    }
    return inFlight;
  }
  function schedule() {
    clearTimeout(timer);
    message("尚未保存");
    if (!composing) timer = setTimeout(() => flush().catch(() => {}), 500);
  }
  async function action(callback) {
    if (moving) return;
    moving = true;
    controls();
    try {
      await callback();
    } catch (_) {
      // drain/export already displayed the error. Keep the editor and input.
    } finally {
      moving = false;
      controls();
    }
  }

  text.disabled = false;
  text.addEventListener("input", schedule);
  text.addEventListener("compositionstart", () => { composing = true; clearTimeout(timer); });
  text.addEventListener("compositionend", () => { composing = false; schedule(); });
  saveButton.addEventListener("click", () => flush().catch(() => {}));
  completeButton.addEventListener("click", () => action(async () => {
    desiredStatus = "done";
    await flush();
    if (editor.dataset.nextUrl) window.location.assign(editor.dataset.nextUrl);
    else message("已保存并完成，已到最后一条。");
  }));
  restoreButton.addEventListener("click", () => action(async () => {
    desiredStatus = "pending";
    await flush();
  }));

  document.addEventListener("click", event => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey
        || event.shiftKey || event.altKey || link.target === "_blank") return;
    event.preventDefault();
    action(async () => {
      await flush();
      window.location.assign(link.href);
    });
  });
  document.querySelector('.site-header form[action="/logout"]').addEventListener("submit", event => {
    event.preventDefault();
    const form = event.target;
    action(async () => {
      await flush();
      HTMLFormElement.prototype.submit.call(form);
    });
  });

  exportButton.addEventListener("click", () => action(async () => {
    await flush();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(editor.dataset.exportUrl, {
        credentials: "same-origin", redirect: "error", signal: controller.signal,
      });
      if (!response.ok || !response.headers.get("Content-Type")?.startsWith("text/markdown")) {
        throw new Error("请检查登录状态或稍后重试。");
      }
      const file = await response.blob();
      const url = URL.createObjectURL(file);
      const link = document.createElement("a");
      link.href = url;
      link.download = editor.dataset.exportName;
      // This is a download, not navigation; bypass our save-first listener.
      link.addEventListener("click", event => event.stopPropagation());
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      message("已保存，已生成整书 Markdown。");
    } catch (error) {
      message(`导出失败：${error.message} 已保存的笔记不受影响。`, true);
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }));

  window.addEventListener("beforeunload", event => {
    if (dirty() || inFlight) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && dirty()) flush().catch(() => {});
  });
  window.addEventListener("online", () => { if (dirty()) flush().catch(() => {}); });
  message("已保存");
  controls();
})();
