"""Single-account credential provisioning and password verification."""

import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets

from noteshub.config import Settings


def password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1,
        maxmem=64 * 1024 * 1024,
    ).hex()


def provision(data_dir: Path, username: str, password: str) -> None:
    username = username.strip()
    if not username or len(username) > 100:
        raise ValueError("用户名不能为空且不能超过 100 字符。")
    if not 12 <= len(password) <= 1024:
        raise ValueError("密码须为 12–1024 字符。")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    salt = secrets.token_hex(16)
    payload = {"username": username, "salt": salt, "hash": password_hash(password, salt)}
    # Exclusive creation protects an existing user's account configuration.
    fd = os.open(data_dir / "account.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        json.dump(payload, target)


def load_account(data_dir: Path) -> dict:
    try:
        account = json.loads((data_dir / "account.json").read_text(encoding="utf-8"))
        if not all(isinstance(account[key], str) for key in ("username", "salt", "hash")):
            raise ValueError("无效账号配置")
        if len(bytes.fromhex(account["salt"])) != 16 or len(bytes.fromhex(account["hash"])) != 64:
            raise ValueError("无效密码哈希")
        return account
    except (OSError, KeyError, ValueError) as exc:
        raise RuntimeError("请先运行 python -m noteshub.auth 创建单用户账号。") from exc


def verify(account: dict, username: str, password: str) -> bool:
    if len(password) > 1024 or len(username) > 100:
        return False
    valid_password = hmac.compare_digest(password_hash(password, account["salt"]), account["hash"])
    return valid_password and hmac.compare_digest(username.encode(), account["username"].encode())


def main() -> None:
    settings = Settings.from_env()
    try:
        username = input("用户名：")
        password = getpass.getpass("密码（至少 12 字符，不显示）：")
        if password != getpass.getpass("再次输入密码："):
            raise ValueError("两次密码不一致。")
        provision(settings.data_dir, username, password)
    except (ValueError, FileExistsError) as exc:
        raise SystemExit(f"未创建账号：{exc}") from exc
    print("账号已创建。密码仅保存为 scrypt 哈希；请妥善备份数据目录。")


if __name__ == "__main__":
    main()
