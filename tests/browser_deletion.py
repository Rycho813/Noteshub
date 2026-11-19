"""Optional synthetic browser regression; no private fixture or live data.

Run: python tests/browser_deletion.py (requires playwright and Chromium).
"""

import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from noteshub.auth import provision
from noteshub.config import Settings
from noteshub.db import Store


RAW = "“浏览器原文一”一读一\n\n“浏览器原文二”\n\n“浏览器原文三”一读三".encode()


def run():
    with tempfile.TemporaryDirectory(prefix="noteshub-delete-browser-") as tmp:
        data_dir = Path(tmp)
        password = secrets.token_urlsafe(24)
        provision(data_dir, "delete-reader", password)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "NOTESHUB_DATA_DIR": str(data_dir), "NOTESHUB_COOKIE_SECURE": "false"}
        command = [sys.executable, "-m", "uvicorn", "noteshub.web:create_app", "--factory",
                   "--host", "127.0.0.1", "--port", str(port)]
        artifacts = ROOT / "test-results" / "deletion"
        artifacts.mkdir(parents=True, exist_ok=True)
        with (data_dir / "server.log").open("w") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(600):
                    try:
                        local.open(base + "/login", timeout=1).close()
                        break
                    except urllib.error.URLError:
                        if process.poll() is not None:
                            raise RuntimeError("Web server exited before readiness: " + (data_dir / "server.log").read_text(errors="replace"))
                        time.sleep(.1)
                else:
                    raise RuntimeError("Web server did not become ready: " + (data_dir / "server.log").read_text(errors="replace"))
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    errors = []
                    for name, viewport, mobile in [
                        ("desktop", {"width": 1280, "height": 900}, False),
                        ("mobile", {"width": 390, "height": 844}, True),
                    ]:
                        context = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile)
                        page = context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(base + "/login")
                        page.get_by_label("用户名", exact=True).fill("delete-reader")
                        page.get_by_label("密码", exact=True).fill(password)
                        page.get_by_role("button", name="登录", exact=True).click()
                        page.wait_for_url(base + "/")
                        page.get_by_role("textbox", name="书名 必填", exact=True).fill(name + " 删除验收")
                        page.get_by_role("button", name="创建书籍", exact=True).click()
                        book_url = page.url
                        page.get_by_label("Markdown 文件").set_input_files({
                            "name": "synthetic.md", "mimeType": "text/markdown", "buffer": RAW,
                        })
                        page.get_by_role("button", name="上传并预览", exact=True).click()
                        page.get_by_role("button", name="确认导入 3 条笔记", exact=True).click()
                        expect(page.locator("#book-progress")).to_have_text("0/3")

                        row = page.locator('.review-editor').first
                        row.locator('.second-reading summary').click()
                        row.locator('.second-comment').fill('分页阅读删除前保存')
                        row.get_by_role('link', name='删除当前笔记', exact=True).click()
                        expect(page.get_by_text('确定永久删除当前笔记吗？', exact=False)).to_be_visible()
                        page.get_by_role('link', name='取消', exact=True).click()
                        expect(page.locator('.second-comment').first).to_have_value('分页阅读删除前保存')
                        row = page.locator('.review-editor').first
                        row.get_by_role('link', name='删除当前笔记', exact=True).click()
                        page.get_by_role('button', name='确认永久删除笔记', exact=True).click()
                        expect(page.locator('.review-editor')).to_have_count(2)
                        expect(page.locator('#book-progress')).to_have_text('0/2')
                        assert page.locator('.review-editor').first.get_attribute('data-note-id') in page.url
                        with page.expect_download() as event:
                            page.locator('#export-book').click()
                        exported = Path(event.value.path()).read_text(encoding='utf-8')
                        assert '浏览器原文一' not in exported and '浏览器原文二' in exported
                        page.locator('.review-editor').last.get_by_role('link', name='删除当前笔记', exact=True).click()
                        page.get_by_role('button', name='确认永久删除笔记', exact=True).click()
                        expect(page.locator('.review-editor')).to_have_count(1)
                        page.get_by_role('link', name='删除当前笔记', exact=True).click()
                        page.get_by_role('button', name='确认永久删除笔记', exact=True).click()
                        expect(page.get_by_role('heading', name='当前没有笔记', exact=True)).to_be_visible()
                        assert not page.locator('#export-book').count()
                        page.get_by_text('导入信息', exact=True).click()
                        with page.expect_download() as event:
                            page.get_by_text('下载原始 Markdown 文件', exact=True).click()
                        assert Path(event.value.path()).read_bytes() == RAW
                        page.goto(base + '/')
                        page.get_by_role('link', name=f'删除书籍：{name} 删除验收', exact=True).click()
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                        page.screenshot(path=artifacts / f'{name}-book-confirm.png', full_page=True)
                        page.get_by_role('button', name='确认永久删除书籍及全部笔记', exact=True).click()
                        expect(page.get_by_text('从新建一本书开始。', exact=True)).to_be_visible()

                        # Confirmation and book deletion also work without JS.
                        no_js = browser.new_context(java_script_enabled=False, storage_state=context.storage_state())
                        basic = no_js.new_page()
                        basic.goto(base + "/")
                        basic.get_by_role("textbox", name="书名 必填", exact=True).fill("无脚本书籍")
                        basic.get_by_role("button", name="创建书籍", exact=True).click()
                        basic.goto(base + "/")
                        basic.get_by_role("link", name="删除书籍：无脚本书籍", exact=True).click()
                        basic.get_by_role("button", name="确认永久删除书籍及全部笔记", exact=True).click()
                        expect(basic.get_by_text("从新建一本书开始。", exact=True)).to_be_visible()
                        no_js.close()
                        context.close()
                        print(f"PASS {name}: paginated reader save/cancel, delete navigation, export, empty state, book delete, no-JS")
                    assert not errors, errors
                    browser.close()
                store = Store(Settings(data_dir, cookie_secure=False))
                with store.connection() as db:
                    for table in ("books", "notes", "imports", "previews"):
                        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
                    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                assert list(store.sources.iterdir()) == []
                print("PASS: no JavaScript errors; all test books, notes, imports, previews and source files removed")
                print(f"Screenshots: {artifacts}")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    run()
