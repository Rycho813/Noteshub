#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -lt 1 ]; then
  echo '用法：bash scripts/restore.sh 备份.tar.gz [--max-bytes 字节上限]' >&2
  exit 2
fi
archive=$1
shift
if [ ! -f "$archive" ]; then
  echo '备份文件不存在。' >&2
  exit 1
fi
# Preserve the caller's relative backup path before changing working directory.
archive=$(realpath -- "$archive")
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"
running=$(docker compose -f docker-compose.yml ps --status running -q app)
if [ -n "$running" ]; then
  echo '应用仍在运行。请用新的 Compose 项目和空数据卷恢复，不会停止或覆盖现有应用。' >&2
  exit 1
fi
docker compose -f docker-compose.yml run --rm --no-deps -T app python -m noteshub.maintenance restore "$@" < "$archive"
