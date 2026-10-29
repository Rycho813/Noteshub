"""Create a deployment archive without user data, secrets, Git or real samples."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "Dockerfile", ".dockerignore", "docker-compose.yml", "docker-compose.https.yml",
    ".env.example", "requirements.txt", "constraints.txt", "README.md",
    "deploy/Caddyfile", "docs/deployment.md", "docs/deployment-validation.md",
    "scripts/backup.sh", "scripts/restore.sh",
)


def package(root: Path, output: Path) -> None:
    paths = [root / name for name in FILES]
    paths.extend(path for path in (root / "noteshub").rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts
                 and path.suffix in {".py", ".html", ".css", ".js"})
    entries = {}
    for path in sorted(paths):
        if path.is_symlink() or any((root / parent).is_symlink() for parent in path.relative_to(root).parents if parent != Path('.')):
            raise ValueError("部署源码不能包含符号链接。")
        if not path.is_file():
            raise ValueError(f"缺少部署文件：{path.relative_to(root)}")
        entries[path.relative_to(root).as_posix()] = path.read_bytes()
    manifest = {"format": 1, "sha256": {name: hashlib.sha256(content).hexdigest() for name, content in entries.items()}}
    entries["RELEASE.json"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation, so repeat runs never replace an earlier release.
    with output.open("xb") as target:
        with tarfile.open(fileobj=target, mode="w:gz") as archive:
            for name, content in entries.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(content), 0o644
                archive.addfile(item, io.BytesIO(content))
    print(f"部署包已生成：{output}")
    print("SHA-256：" + hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=ROOT / "artifacts/noteshub-deploy.tar.gz")
    args = parser.parse_args()
    package(ROOT, args.output)
