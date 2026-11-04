"""Offline removal of unarchived V2 drafts only. Stop app and back up first."""
import argparse
from pathlib import Path
import re
import sqlite3

from noteshub.config import Settings


def retire(root: Path, *, apply=False):
    database = root / 'noteshub.sqlite3'
    if not database.is_file() or database.is_symlink():
        raise ValueError('需要已有数据库；先停止测试应用并完整备份。')
    with sqlite3.connect(database, timeout=3) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN EXCLUSIVE')
        if db.execute('PRAGMA user_version').fetchone()[0] not in (4, 5, 6):
            raise ValueError('仅支持 schema v4–v6。')
        rows = db.execute('SELECT d.id,d.file_path FROM image_drafts d WHERE NOT EXISTS '
                          '(SELECT 1 FROM notes n WHERE n.image_draft_id=d.id)').fetchall()
        for _, name in rows:
            if not re.fullmatch(r'images/[0-9a-f]{32}\.(png|jpg)', name) or (root/name).is_symlink() or (root/'images').is_symlink():
                raise ValueError('旧草稿路径无效，未执行清理。')
        counts = {'drafts':len(rows), 'ocr_runs':sum(db.execute('SELECT COUNT(*) FROM ocr_runs WHERE draft_id=?',(id,)).fetchone()[0] for id,_ in rows)}
        if apply:
            db.executemany('DELETE FROM image_drafts WHERE id=?',[(id,) for id,_ in rows])
        db.commit()
        if apply:
            # Database first: interrupted removal leaves only unreferenced files,
            # never a committed row pointing at a deleted source image.
            for _, name in rows:
                (root/name).unlink(missing_ok=True)
        return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='实际清理；默认只统计')
    args = parser.parse_args()
    counts = retire(Settings.from_env().data_dir, apply=args.apply)
    print(('已清理' if args.apply else '预览，不修改数据')+f"：旧草稿 {counts['drafts']}，附属 OCR 历史 {counts['ocr_runs']}。已归档来源、书籍、快速记录及 Markdown 笔记保留。")


if __name__ == '__main__':
    main()
