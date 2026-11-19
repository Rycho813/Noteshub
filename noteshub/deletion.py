"""Permanent row and photo deletion with recoverable file staging."""
import re
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS deleted_entities (kind TEXT NOT NULL, id TEXT NOT NULL, deleted_at INTEGER NOT NULL,
 PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS retained_files (file_path TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
 sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL);
PRAGMA user_version=7;
"""


def references(db):
    return ({'sources/'+r[0] for r in db.execute('SELECT original_file_path FROM imports')}
            | {r[0] for r in db.execute('SELECT file_path FROM image_drafts')}
            | {r[0] for r in db.execute('SELECT file_path FROM paper_photos WHERE purged_at IS NULL')}
            | {r[0] for r in db.execute('SELECT file_path FROM retained_files')})


def checked(store, relative):
    if not re.fullmatch(r'sources/[0-9a-f]{32}\.md|(?:images|paper-images)/[0-9a-f]{32}\.(?:png|jpg)', relative):
        raise OSError('关联文件路径无效。')
    path = store.root / relative
    if path.parent.is_symlink() or path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError('关联文件类型无效。')
    return path


def remove_paper(db, note_id):
    db.execute("INSERT OR IGNORE INTO deleted_entities SELECT 'photo',id,? FROM paper_photos WHERE note_id=?", (int(time.time()),note_id))
    db.execute('DELETE FROM paper_worker_claims WHERE job_id IN (SELECT id FROM paper_ocr_jobs WHERE note_id=?)', (note_id,))
    for table in ('paper_ocr_jobs','paper_pages','paper_mutations','paper_photos','paper_entries'):
        db.execute(f'DELETE FROM {table} WHERE note_id=?', (note_id,))


def delete(store, book_id, note_id=None):
    store.recover_sources()
    try:
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():
                raise LookupError('书籍不存在或已删除。')
            notes = db.execute('SELECT * FROM notes WHERE book_id=? ORDER BY sort_order',(book_id,)).fetchall()
            target = next((r for r in notes if r['id']==note_id),None) if note_id else None
            if note_id and target is None:
                raise LookupError('笔记不存在、已删除或不属于这本书。')
            next_id = None
            if target:
                index = notes.index(target)
                next_id = notes[index+1]['id'] if index+1<len(notes) else (notes[index-1]['id'] if index else None)
            selected = [target] if target else notes
            selected_ids = {note['id'] for note in selected}
            paths = []
            if note_id:
                paths += [r[0] for r in db.execute('SELECT file_path FROM paper_photos WHERE note_id=?',(note_id,))]
                if target['image_draft_id']:
                    paths += [r[0] for r in db.execute('SELECT file_path FROM image_drafts WHERE id=?',(target['image_draft_id'],))]
            else:
                paths = (['sources/'+r[0] for r in db.execute('SELECT original_file_path FROM imports WHERE book_id=?',(book_id,))]
                         + [r[0] for r in db.execute('SELECT file_path FROM image_drafts WHERE book_id=?',(book_id,))]
                         + [r[0] for r in db.execute('SELECT file_path FROM paper_photos WHERE note_id IN (SELECT id FROM notes WHERE book_id=?)',(book_id,))]
                         + [r[0] for r in db.execute('SELECT file_path FROM retained_files WHERE book_id=?',(book_id,))])
            for relative in sorted(set(paths)):
                path = checked(store,relative)
                other = db.execute('SELECT 1 FROM retained_files WHERE file_path=? AND book_id<>?',(relative,book_id)).fetchone()
                if relative.startswith('images/'):
                    other = other or db.execute('SELECT 1 FROM image_drafts WHERE file_path=? AND book_id<>?',(relative,book_id)).fetchone()
                if relative.startswith('paper-images/'):
                    owners = db.execute('SELECT note_id FROM paper_photos WHERE file_path=?',(relative,)).fetchall()
                    other = other or any(r[0] not in selected_ids for r in owners)
                if other:
                    raise OSError('图片存在其他笔记或书籍的引用。')
                if path.exists():
                    path.rename(path.with_name(path.name+'.deleting'))
                    store.sync_directory(path.parent)
            for note in selected:
                if note['source_type']=='paper':
                    remove_paper(db,note['id'])
                db.execute('INSERT OR IGNORE INTO deleted_entities VALUES (?,?,?)',('note',note['id'],int(time.time())))
                db.execute('DELETE FROM notes WHERE id=?',(note['id'],))
                if note_id and note['image_draft_id']:
                    db.execute('DELETE FROM image_drafts WHERE id=?',(note['image_draft_id'],))
            if note_id:
                db.executemany('DELETE FROM retained_files WHERE file_path=? AND book_id=?',[(p,book_id) for p in paths])
            if not note_id:
                for table in ('previews','imports','image_drafts','retained_files'):
                    db.execute(f'DELETE FROM {table} WHERE book_id=?',(book_id,))
                db.execute('INSERT OR IGNORE INTO deleted_entities VALUES (?,?,?)',('book',book_id,int(time.time())))
                db.execute('DELETE FROM books WHERE id=?',(book_id,))
    except BaseException:
        store.recover_sources()
        raise
    store.recover_sources()
    return next_id
