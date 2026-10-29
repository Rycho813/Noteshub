"""Offline, whole-data backup/restore. No web endpoints or remote operations."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile

from noteshub.auth import load_account
from noteshub.config import Settings


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _allowed(name: str) -> bool:
    return name in {"account.json", "noteshub.sqlite3", "manifest.json"} or bool(
        re.fullmatch(r"sources/[0-9a-f]{32}\.md", name)
    )


def _validate(root: Path, db: sqlite3.Connection) -> list[str]:
    load_account(root)
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise ValueError("备份工具仅支持数据库 schema v1。")
    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("数据库完整性检查失败。")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("数据库外键检查失败。")
    files = ["account.json", "noteshub.sqlite3"]
    for filename, checksum in db.execute("SELECT original_file_path, sha256 FROM imports"):
        name = f"sources/{filename}"
        if not _allowed(name):
            raise ValueError("数据库源文件路径无效。")
        path = root / name
        if path.is_symlink() or not path.is_file() or _digest(path) != checksum:
            raise ValueError("源文件缺失、类型无效或校验失败。")
        files.append(name)
    for name in files:
        if (root / name).is_symlink() or not (root / name).is_file():
            raise ValueError("备份文件必须是普通文件。")
    return sorted(files)


def backup(root: Path, output) -> None:
    """Caller stops the app first; lock the database for the whole archive."""
    if not (root / "noteshub.sqlite3").is_file():
        raise ValueError("没有可备份的数据库。")
    with sqlite3.connect(root / "noteshub.sqlite3", timeout=3) as db:
        if db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise ValueError("数据库 WAL 尚在使用，请先停止全部应用写入后重试。")
        db.execute("BEGIN EXCLUSIVE")
        files = _validate(root, db)
        manifest = json.dumps({"format": 1, "files": {name: _digest(root / name) for name in files}},
                              ensure_ascii=False, sort_keys=True).encode()
        with tarfile.open(fileobj=output, mode="w|gz") as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(manifest), 0o600
            archive.addfile(info, io.BytesIO(manifest))
            for name in files:
                archive.add(root / name, arcname=name, recursive=False)
        db.rollback()


def restore(root: Path, source, *, max_bytes: int = 2 * 1024**3) -> None:
    """Restore only into an empty directory, with staged validation and cleanup."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(root.iterdir()):
        raise ValueError("恢复目标不是空目录；请使用新的数据卷，不会覆盖已有数据。")
    published = []
    try:
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=root) as tmp:
            stage = Path(tmp)
            seen, total = set(), 0
            with tarfile.open(fileobj=source, mode="r|gz") as archive:
                for member in archive:
                    if not member.isfile() or not _allowed(member.name) or member.name in seen:
                        raise ValueError("备份含重复、非法路径或非普通文件。")
                    seen.add(member.name)
                    total += member.size
                    if len(seen) > 100000 or total > max_bytes:
                        raise ValueError("备份超过恢复容量限制；可明确增加 --max-bytes 后重试。")
                    target = stage / member.name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.extractfile(member) as content, target.open("xb") as destination:
                        shutil.copyfileobj(content, destination, length=1024 * 1024)
                    target.chmod(0o600)
            manifest = json.loads((stage / "manifest.json").read_text())
            if not isinstance(manifest, dict) or manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
                raise ValueError("不支持的备份格式。")
            expected = manifest["files"]
            if set(expected) != seen - {"manifest.json"}:
                raise ValueError("备份清单与文件不一致。")
            for name, checksum in expected.items():
                if _digest(stage / name) != checksum:
                    raise ValueError("备份文件校验失败。")
            with sqlite3.connect(stage / "noteshub.sqlite3") as db:
                db.execute("PRAGMA foreign_keys = ON")
                if set(_validate(stage, db)) != set(expected):
                    raise ValueError("备份包含未引用的文件或缺失数据。")
                # Restore content, not active logins or temporary uploads.
                db.execute("PRAGMA journal_mode = DELETE")
                db.execute("PRAGMA secure_delete = ON")
                db.execute("DELETE FROM previews")
                db.execute("DELETE FROM sessions")
                db.execute("UPDATE login_guard SET failures = 0, blocked_until = 0 WHERE id = 1")
            (stage / "manifest.json").unlink()
            for path in stage.iterdir():
                target = root / path.name
                path.rename(target)
                published.append(target)
    except BaseException:
        for path in reversed(published):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="离线整目录备份/恢复；须先停止应用")
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("--max-bytes", type=int, default=2 * 1024**3,
                        help="恢复解压上限，默认 2 GiB，可按需增加")
    args = parser.parse_args()
    root = Settings.from_env().data_dir
    try:
        if args.operation == "backup":
            if sys.stdout.isatty():
                raise ValueError("请将备份输出重定向到文件，不要输出到终端。")
            backup(root, sys.stdout.buffer)
        else:
            restore(root, sys.stdin.buffer, max_bytes=args.max_bytes)
            print("恢复完成；会话和临时预览已清理，请重新登录。", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError, KeyError) as exc:
        parser.exit(1, f"备份/恢复未完成：{exc}\n")


if __name__ == "__main__":
    main()
