#!/usr/bin/env bash
# Run from the deployment checkout. Never affects other Compose services.
set -euo pipefail
umask 077
if [ "$#" -ne 1 ]; then
  echo '用法：bash scripts/backup.sh /安全目录/备份名.tar.gz' >&2
  exit 2
fi
output=$(realpath -m -- "$1")
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"
if [ -e "$output" ] || [ -e "$output.sha256" ]; then
  echo '目标备份已存在，不会覆盖。' >&2
  exit 1
fi
mkdir -p -- "$(dirname -- "$output")"
temporary=$(mktemp -- "$output.partial.XXXXXX")
was_running=false
cleanup() {
  result=$?
  rm -f -- "$temporary"
  if [ "$was_running" = true ]; then
    if ! docker compose -f docker-compose.yml start app >&2; then
      echo '备份后应用未能重新启动，请检查 app 服务。' >&2
      result=1
    fi
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
running=$(docker compose -f docker-compose.yml ps --status running -q app)
if [ -n "$running" ]; then
  was_running=true
  docker compose -f docker-compose.yml stop app >&2
fi
docker compose -f docker-compose.yml run --rm --no-deps -T app python -m noteshub.maintenance backup > "$temporary"
# Hard-link publication refuses to overwrite an existing backup, even in a race.
ln -- "$temporary" "$output"
sha256sum -- "$output" > "$output.sha256"
echo "备份完成：$output（包含私人笔记和密码哈希，请安全保存）" >&2
