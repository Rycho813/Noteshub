"""Optional real Chromium smoke run; creates only isolated temporary data.

Run with a Python that has playwright installed. Uses system Chromium and the
project .venv for the web server; no browser downloads or public listener.
"""

import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright
from browser_review import desktop_review, mobile_review


ROOT = Path(__file__).resolve().parents[1]


def run():
    if not (ROOT / "tests/fixtures/论爱欲.md").is_file():
        raise SystemExit("Private fixture missing: provide tests/fixtures/论爱欲.md locally; it is excluded from Git.")
    with tempfile.TemporaryDirectory(prefix="noteshub-browser-") as tmp:
        data_dir = Path(tmp)
        # Provision via the same API as the interactive account setup tool.
        # The runner is independent of the server's dependency environment.
        import sys
        sys.path.insert(0, str(ROOT))
        from noteshub.auth import provision
        password = secrets.token_urlsafe(24)
        provision(data_dir, "smoke-reader", password)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = {**os.environ, "NOTESHUB_DATA_DIR": str(data_dir), "NOTESHUB_COOKIE_SECURE": "false"}
        command = [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "noteshub.web:create_app",
                   "--factory", "--host", "127.0.0.1", "--port", str(port)]
        with (data_dir / "server.log").open("w") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                base = f"http://127.0.0.1:{port}"
                local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(50):
                    try:
                        local.open(base + "/login", timeout=1).close()
                        break
                    except urllib.error.URLError:
                        if process.poll() is not None:
                            raise RuntimeError("Web server exited before readiness")
                        time.sleep(.1)
                else:
                    raise RuntimeError("Web server did not become ready")
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=shutil.which("chromium"), headless=True,
                                                args=["--no-sandbox"])
                    desktop = browser.new_context(viewport={"width": 1280, "height": 900})
                    page = desktop.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def login(page):
                        page.goto(base + "/login")
                        page.get_by_label("用户名", exact=True).fill("smoke-reader")
                        page.get_by_label("密码", exact=True).fill(password)
                        page.get_by_role("button", name="登录", exact=True).click()
                        page.wait_for_url(base + "/")

                    login(page)
                    page.get_by_role("textbox", name="书名 必填", exact=True).fill("论爱欲 浏览器验收")
                    page.get_by_role("button", name="创建书籍").click()
                    book_url = page.url
                    page.get_by_label("Markdown 文件").set_input_files(ROOT / "tests/fixtures/论爱欲.md")
                    page.get_by_role("button", name="上传并预览").click()
                    page.get_by_text("仍有 7 条解析异常", exact=False).wait_for()
                    assert page.get_by_role("button", name="确认导入 154 条笔记").is_disabled()
                    artifacts = ROOT / "test-results"
                    artifacts.mkdir(exist_ok=True)
                    page.locator("#record-3").scroll_into_view_if_needed()
                    page.screenshot(path=artifacts / "desktop-preview.png")
                    for record_id in (3, 4, 15, 16, 32, 42, 56):
                        record = page.locator(f"#record-{record_id}")
                        record.locator('textarea[name="original_text"]').fill(f"浏览器测试修正原文 {record_id}")
                        record.locator('textarea[name="first_comment"]').fill("浏览器测试评述")
                        record.get_by_role("button", name="保存修正").click()
                        page.wait_for_url(f"**#record-{record_id}")
                    assert page.get_by_role("button", name="确认导入 154 条笔记").is_enabled()
                    page.get_by_role("button", name="确认导入 154 条笔记").click()
                    page.wait_for_url(book_url)
                    page.get_by_text("笔记 1 / 154", exact=False).wait_for()
                    assert not page.get_by_role("button", name="上传并预览").count()
                    desktop_review(page, book_url, data_dir, artifacts)

                    mobile = browser.new_context(viewport={"width": 390, "height": 844},
                                                 is_mobile=True, has_touch=True)
                    phone = mobile.new_page()
                    phone.on("pageerror", lambda error: errors.append(str(error)))
                    login(phone)
                    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    phone.get_by_role("textbox", name="书名 必填", exact=True).fill("手机上传验收")
                    phone.get_by_role("button", name="创建书籍").click()
                    phone.get_by_label("Markdown 文件").set_input_files({
                        "name": "phone.md", "mimeType": "text/markdown",
                        "buffer": "“\n手机原文”\n评述\n\n“未闭合".encode(),
                    })
                    phone.get_by_role("button", name="上传并预览").click()
                    phone.get_by_text("仍有 1 条解析异常", exact=False).wait_for()
                    assert phone.locator('#record-1 textarea[name="original_text"]').input_value() == "\n手机原文"
                    assert phone.locator('#record-1 textarea[name="first_comment"]').input_value() == "\n评述"
                    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    phone.screenshot(path=artifacts / "mobile-preview.png")
                    phone.get_by_role("button", name="取消并清理本次上传").click()
                    phone.get_by_role("button", name="上传并预览").wait_for()
                    mobile_review(phone, book_url, artifacts)
                    phone.get_by_role("button", name="退出登录").click()
                    phone.wait_for_url(base + "/login")
                    with sqlite3.connect(data_dir / "noteshub.sqlite3") as db:
                        assert db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 154
                        assert db.execute("SELECT COUNT(*) FROM previews").fetchone()[0] == 0
                        assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1
                    assert len(list((data_dir / "sources").iterdir())) == 1
                    assert not errors, errors
                    browser.close()
                    print("Chromium smoke passed: 154-note import; ordered autosave, navigation, done/restore, lost-response and offline retry; full-book download; mobile edit/export/cancel/logout; no horizontal overflow.")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    run()
