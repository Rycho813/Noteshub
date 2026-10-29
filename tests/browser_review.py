"""Review/export browser checks, called by browser_smoke.py."""

from pathlib import Path
import hashlib
import re
import sqlite3
from urllib.parse import parse_qs

from playwright.sync_api import expect


def saved(page):
    expect(page.locator("#save-status")).to_have_text("已保存")


def desktop_review(page, book_url, data_dir: Path, artifacts: Path):
    text = page.locator("#second-comment")
    text.fill("二读自动保存\n# 这是纯文本")
    saved(page)
    assert page.locator("#note-status").inner_text() == "待处理"
    page.reload()
    assert page.locator("#second-comment").input_value() == "二读自动保存\n# 这是纯文本"
    # Immediate navigation must flush the pending debounce first.
    text = page.locator("#second-comment")
    text.fill("导航前的最新内容")
    page.get_by_role("link", name="下一条", exact=True).click()
    page.wait_for_url("**?note=*")
    page.get_by_role("link", name="上一条", exact=True).click()
    expect(page.locator("#second-comment")).to_have_value("导航前的最新内容")
    page.get_by_role("button", name="完成并继续", exact=True).click()
    expect(page.locator("#book-progress")).to_have_text("1/154")
    assert page.locator("#second-comment").input_value() == ""
    page.get_by_role("button", name="完成并继续", exact=True).click()
    expect(page.locator("#book-progress")).to_have_text("2/154")
    page.get_by_role("link", name="上一条", exact=True).click()
    expect(page.locator("#note-status")).to_have_text("已完成")
    page.get_by_role("link", name="上一条", exact=True).click()
    expect(page.locator("#second-comment")).to_have_value("导航前的最新内容")
    page.get_by_role("button", name="恢复待处理", exact=True).click()
    expect(page.locator("#book-progress")).to_have_text("1/154")

    # Hold the first save, type again, then navigate. The second request must
    # carry the newer input and finish before navigation, without parallel POSTs.
    held, posted = [], []
    pattern = "**/api/books/*/notes/*"

    def hold_first(route):
        posted.append(parse_qs(route.request.post_data, keep_blank_values=True)["second_comment"][0])
        if len(posted) == 1:
            held.append(route)
        else:
            route.continue_()

    page.route(pattern, hold_first)
    with page.expect_request(pattern):
        page.locator("#second-comment").fill("请求中的旧输入")
    page.wait_for_timeout(20)
    assert held
    current_url = page.url
    page.locator("#second-comment").fill("后输入的最终内容")
    page.get_by_role("link", name="下一条", exact=True).click()
    assert page.url == current_url
    held[0].continue_()
    page.wait_for_url(lambda url: str(url) != current_url)
    page.unroute(pattern, hold_first)
    assert posted == ["请求中的旧输入", "后输入的最终内容"], posted
    page.get_by_role("link", name="上一条", exact=True).click()
    expect(page.locator("#second-comment")).to_have_value("后输入的最终内容")

    # The request commits, but its response is lost. Export/navigation must stop,
    # input must remain, and reverting to the prior saved value must still POST.
    def lose_reply(route):
        route.fetch()
        route.abort("failed")

    page.route(pattern, lose_reply)
    page.locator("#second-comment").fill("回复丢失的内容")
    page.get_by_role("button", name="立即保存", exact=True).click()
    page.get_by_role("button", name="重试保存", exact=True).wait_for()
    assert page.locator("#second-comment").input_value() == "回复丢失的内容"
    current_url = page.url
    page.get_by_role("link", name="下一条", exact=True).click()
    page.get_by_role("button", name="重试保存", exact=True).wait_for()
    assert page.url == current_url
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    page.get_by_role("button", name="导出整书 Markdown", exact=True).click()
    expect(page.locator("#export-book")).to_be_enabled()
    assert not downloads
    page.unroute(pattern, lose_reply)
    page.locator("#second-comment").fill("后输入的最终内容")
    page.get_by_role("button", name="重试保存", exact=True).click()
    saved(page)
    page.reload()
    assert page.locator("#second-comment").input_value() == "后输入的最终内容"

    # Explicit offline failure retains input and blocks navigation. Reconnecting
    # raises the browser's online event, which retries the unsaved content.
    page.context.set_offline(True)
    page.locator("#second-comment").fill("离线期间输入")
    page.get_by_role("button", name="立即保存", exact=True).click()
    page.get_by_role("button", name="重试保存", exact=True).wait_for()
    current_url = page.url
    page.get_by_role("link", name="下一条", exact=True).click()
    page.get_by_role("button", name="重试保存", exact=True).wait_for()
    assert page.url == current_url
    assert page.locator("#second-comment").input_value() == "离线期间输入"
    page.context.set_offline(False)
    saved(page)

    # Click export before the debounce fires: the resulting download must
    # include the latest text, plus every pending note.
    page.locator("#second-comment").fill("导出前最后输入\n# 字面标题")
    with page.expect_download() as event:
        page.get_by_role("button", name="导出整书 Markdown", exact=True).click()
    download = event.value
    assert download.suggested_filename == "论爱欲 浏览器验收.md"
    exported = Path(download.path()).read_text(encoding="utf-8")
    assert "导出前最后输入" in exported
    assert r"\# 字面标题" in exported
    assert len(re.findall(r"^## 笔记 \d+$", exported, re.MULTILINE)) == 154
    assert "## 笔记 154" in exported
    page.screenshot(path=artifacts / "desktop-review.png")

    # Completing the final note, with no second comment, stays on that note.
    book_id = book_url.rsplit("/", 1)[1]
    with sqlite3.connect(data_dir / "noteshub.sqlite3") as db:
        last_id = db.execute("SELECT id FROM notes WHERE book_id = ? ORDER BY sort_order DESC LIMIT 1", (book_id,)).fetchone()[0]
    last_url = book_url + "?note=" + last_id
    page.goto(last_url)
    page.get_by_role("button", name="完成并继续", exact=True).click()
    expect(page.locator("#save-status")).to_contain_text("已到最后一条")
    assert page.url == last_url
    assert page.locator("#note-status").inner_text() == "已完成"
    assert page.locator("#second-comment").input_value() == ""
    assert page.get_by_role("link", name="下一条", exact=True).count() == 0

    # Actual session expiration yields JSON 401, never a false saved indicator
    # after following a login redirect. Keep the local input for copying.
    cookie = next(item["value"] for item in page.context.cookies() if item["name"] == "noteshub_session")
    with sqlite3.connect(data_dir / "noteshub.sqlite3") as db:
        db.execute("UPDATE sessions SET expires_at = 0 WHERE id = ?", (hashlib.sha256(cookie.encode()).hexdigest(),))
    page.locator("#second-comment").fill("登录失效时未保存的二读")
    page.get_by_role("button", name="立即保存", exact=True).click()
    expect(page.locator("#login-hint")).to_be_visible()
    expect(page.locator("#save-status")).to_contain_text("登录已失效")
    page.get_by_role("link", name="上一条", exact=True).click()
    expect(page.locator("#login-hint")).to_be_visible()
    assert page.url == last_url
    assert page.locator("#second-comment").input_value() == "登录失效时未保存的二读"


def mobile_review(page, book_url, artifacts: Path):
    page.goto(book_url)
    assert page.locator("#second-comment").input_value() == "导出前最后输入\n# 字面标题"
    page.locator("#second-comment").fill("手机二读评述\n第二行")
    saved(page)
    assert page.locator("#note-status").inner_text() == "待处理"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#second-comment").scroll_into_view_if_needed()
    page.screenshot(path=artifacts / "mobile-review.png")
    with page.expect_download() as event:
        page.get_by_role("button", name="导出整书 Markdown", exact=True).click()
    assert "手机二读评述" in Path(event.value.path()).read_text(encoding="utf-8")
