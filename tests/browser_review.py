"""Paginated review/export browser checks, called by browser_smoke.py."""
from pathlib import Path
import hashlib
import re
import sqlite3
from urllib.parse import parse_qs

from playwright.sync_api import expect


def open_row(page,index=0):
    row=page.locator('.review-editor').nth(index)
    if not row.locator('.second-reading').get_attribute('open'):
        row.locator('.second-reading summary').click()
    return row


def saved(row):
    expect(row.locator('.save-status')).to_have_text('已保存')


def desktop_review(page,book_url,data_dir:Path,artifacts:Path):
    expect(page.locator('.review-editor')).to_have_count(30)
    assert page.locator('.second-reading[open]').count()==0
    row=open_row(page);row.locator('.second-comment').fill('二读自动保存\n# 这是纯文本');saved(row)
    row.locator('.complete-note').click();expect(row.locator('.note-status')).to_have_text('已完成')
    row.locator('.restore-pending').click();expect(row.locator('.note-status')).to_have_text('待处理')
    page.reload();row=open_row(page);expect(row.locator('.second-comment')).to_have_value('二读自动保存\n# 这是纯文本')
    # Independent row streams and save-before-pagination.
    row.locator('.second-comment').fill('导航前的最新内容')
    second=open_row(page,1);second.locator('.second-comment').fill('第二条独立二读')
    page.get_by_role('link',name='下一页',exact=True).click();page.wait_for_url('**page=2')
    page.get_by_role('link',name='上一页',exact=True).click();page.wait_for_url('**page=1')
    row=open_row(page);expect(row.locator('.second-comment')).to_have_value('导航前的最新内容')
    expect(page.locator('.second-comment').nth(1)).to_have_value('第二条独立二读')
    held,posted=[],[];pattern='**/api/books/*/notes/*'
    def hold_first(route):
        posted.append(parse_qs(route.request.post_data,keep_blank_values=True)['second_comment'][0])
        held.append(route) if len(posted)==1 else route.continue_()
    page.route(pattern,hold_first)
    with page.expect_request(pattern):row.locator('.second-comment').fill('请求中的旧输入')
    row.locator('.second-comment').fill('后输入的最终内容')
    url=page.url;page.get_by_role('link',name='下一页',exact=True).click();assert page.url==url
    held[0].continue_();page.wait_for_url('**page=2');page.unroute(pattern,hold_first)
    assert posted==['请求中的旧输入','后输入的最终内容']
    page.get_by_role('link',name='上一页',exact=True).click();page.wait_for_url('**page=1');row=open_row(page)
    def lose_reply(route):route.fetch();route.abort('failed')
    page.route(pattern,lose_reply)
    row.locator('.second-comment').fill('回复丢失的内容');row.locator('.save-now').click()
    expect(row.locator('.save-status')).to_contain_text('保存失败')
    url=page.url;page.get_by_role('link',name='下一页',exact=True).click();expect(row.locator('.save-status')).to_contain_text('保存失败');assert page.url==url
    page.unroute(pattern,lose_reply);row.locator('.second-comment').fill('后输入的最终内容');row.locator('.save-now').click();saved(row)
    page.context.set_offline(True);row.locator('.second-comment').fill('离线期间输入');row.locator('.save-now').click();expect(row.locator('.save-status')).to_contain_text('保存失败')
    page.context.set_offline(False);saved(row)
    row.locator('.second-comment').fill('导出前最后输入\n# 字面标题')
    with page.expect_download() as event:page.locator('#export-book').click()
    exported=Path(event.value.path()).read_text()
    assert r'\# 字面标题' in exported and len(re.findall(r'^## 笔记 \d+$',exported,re.MULTILINE))==154
    page.screenshot(path=artifacts/'desktop-review.png')
    with sqlite3.connect(data_dir/'noteshub.sqlite3') as db:
        last=db.execute('SELECT id FROM notes WHERE book_id=? ORDER BY sort_order DESC LIMIT 1',(book_url.rsplit('/',1)[1],)).fetchone()[0]
    page.goto(book_url+'?note='+last);row=open_row(page,page.locator('.review-editor').count()-1)
    row.locator('.complete-note').click();expect(row.locator('.note-status')).to_have_text('已完成')
    cookie=next(c['value'] for c in page.context.cookies() if c['name']=='noteshub_session')
    with sqlite3.connect(data_dir/'noteshub.sqlite3') as db:db.execute('UPDATE sessions SET expires_at=0 WHERE id=?',(hashlib.sha256(cookie.encode()).hexdigest(),))
    row.locator('.second-comment').fill('登录失效时未保存的二读');row.locator('.save-now').click()
    expect(row.locator('.login-hint')).to_be_visible();expect(row.locator('.save-status')).to_contain_text('登录已失效')


def mobile_review(page,book_url,artifacts:Path):
    page.goto(book_url);row=open_row(page)
    expect(row.locator('.second-comment')).to_have_value('导出前最后输入\n# 字面标题')
    row.locator('.second-comment').fill('手机二读评述\n第二行');saved(row)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=artifacts/'mobile-review.png')
    with page.expect_download() as event:page.locator('#export-book').click()
    assert '手机二读评述' in Path(event.value.path()).read_text()
