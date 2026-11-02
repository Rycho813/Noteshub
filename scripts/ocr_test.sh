#!/usr/bin/env bash
# Operates only on the named local test project; never sources production .env.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"
readonly project=noteshub-v2-ocr-test
readonly marker=ocr-v2
readonly volume=noteshub-v2-ocr-test_data
command=${1:-help}
case "$command" in
  help) echo 'Usage: bash scripts/ocr_test.sh {preflight|build|account|up|check|network|enable-ocr|disable-ocr|summary|stop|down}'; exit 0 ;;
  preflight|build|account|up|check|network|enable-ocr|disable-ocr|summary|stop|down) ;;
  *) echo 'Unknown command; no changes made.' >&2; exit 2 ;;
esac
release_sha=$(python3 scripts/check_release.py --digest)
export NOTESHUB_TEST_IMAGE_TAG="v2-ocr-test-${release_sha:0:12}"
export NOTESHUB_TEST_PORT=${NOTESHUB_TEST_PORT:-18080}
export NOTESHUB_TEST_CREDENTIALS_PATH=${NOTESHUB_TEST_CREDENTIALS_PATH:-/etc/noteshub-v2-ocr-test/tencent-ocr.json}
export NOTESHUB_TEST_OCR_ENABLED=false
if ! [[ "$NOTESHUB_TEST_PORT" =~ ^[0-9]{4,5}$ ]] || (( NOTESHUB_TEST_PORT < 1024 || NOTESHUB_TEST_PORT > 65535 || NOTESHUB_TEST_PORT == 8000 )); then
  echo 'Choose an unused test port from 1024..65535, excluding production port 8000.' >&2; exit 1
fi
local_docker=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock)
compose=("${local_docker[@]}" compose --env-file /dev/null --project-directory "$project_root" -p "$project" -f docker-compose.ocr-test.yml)
"${local_docker[@]}" info --format 'Local Docker: {{.ServerVersion}} {{.OSType}}/{{.Architecture}}'
# An unmarked pre-existing resource with the same name is not ours to manage.
while IFS= read -r id; do
  [ -z "$id" ] && continue
  label=$("${local_docker[@]}" inspect --format '{{index .Config.Labels "org.noteshub.test"}}' "$id")
  [ "$label" = "$marker" ] || { echo 'Unrecognized container in test namespace; refusing.' >&2; exit 1; }
done < <("${local_docker[@]}" ps -aq --filter "label=com.docker.compose.project=$project")
if "${local_docker[@]}" volume inspect "$volume" >/dev/null 2>&1; then
  label=$("${local_docker[@]}" volume inspect --format '{{index .Labels "org.noteshub.test"}}' "$volume")
  [ "$label" = "$marker" ] || { echo 'Unrecognized existing test volume; refusing.' >&2; exit 1; }
fi
if "${local_docker[@]}" network inspect "${project}_default" >/dev/null 2>&1; then
  label=$("${local_docker[@]}" network inspect --format '{{index .Labels "org.noteshub.test"}}' "${project}_default")
  [ "$label" = "$marker" ] || { echo 'Unrecognized existing test network; refusing.' >&2; exit 1; }
fi
credentials_metadata() {
  # Check names, modes, ownership and size only, never open the secret file.
  python3 - "$NOTESHUB_TEST_CREDENTIALS_PATH" "$project_root" <<'PY'
import os, stat, sys
from pathlib import Path
file=Path(sys.argv[1])
try:
    resolved=file.resolve(strict=True)
    root=Path(sys.argv[2]).resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError()
    metadata=resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != 10001 or metadata.st_size > 16384:
        raise ValueError()
except (OSError,ValueError):
    raise SystemExit('Credential file metadata invalid: use a private file outside the release, owner UID 10001, mode 0400/0600, at most 16 KiB. No contents were read.')
print('Credential file metadata OK; contents were not read.')
PY
}
image_ready() {
  label=$("${local_docker[@]}" image inspect --format '{{index .Config.Labels "org.noteshub.release"}}' "noteshub:$NOTESHUB_TEST_IMAGE_TAG" 2>/dev/null || true)
  [ "$label" = "$release_sha" ] || { echo 'Verified test image missing; run build first.' >&2; exit 1; }
}
case "$command" in
  preflight)
    "${compose[@]}" config --quiet
    df -h "$project_root"
    free -m
    if command -v ss >/dev/null; then ss -H -ltn "sport = :$NOTESHUB_TEST_PORT"; fi
    echo "Test project=$project port=127.0.0.1:$NOTESHUB_TEST_PORT image=noteshub:$NOTESHUB_TEST_IMAGE_TAG"
    ;;
  build)
    if "${local_docker[@]}" image inspect "noteshub:$NOTESHUB_TEST_IMAGE_TAG" >/dev/null 2>&1; then
      image_ready
      echo 'Reusing already verified test image.'
    else
      build_args=()
      if [ -n "${NOTESHUB_TEST_BUILD_CA:-}" ]; then build_args+=(--secret "id=proxy_ca,src=$NOTESHUB_TEST_BUILD_CA"); fi
      "${local_docker[@]}" build "${build_args[@]}" --label "org.noteshub.release=$release_sha" -t "noteshub:$NOTESHUB_TEST_IMAGE_TAG" .
    fi
    ;;
  account)
    credentials_metadata; image_ready
    "${compose[@]}" run --rm --no-deps app python -m noteshub.auth
    ;;
  up|enable-ocr|disable-ocr)
    credentials_metadata; image_ready
    if [ "$command" = enable-ocr ]; then export NOTESHUB_TEST_OCR_ENABLED=true; fi
    "${compose[@]}" up -d --no-build app
    ;;
  check)
    "${compose[@]}" exec -T -e NOTESHUB_OCR_ENABLED=true app python -c "from noteshub.ocr import TencentOCR; TencentOCR().prepare(); print('Local credential readability OK; no OCR request made')"
    "${compose[@]}" exec -T app python -c "import urllib.request; page=urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/login',timeout=5); assert page.status == 200 and b'csrf' in page.read(); print('Login page functional; no OCR request made')"
    "${compose[@]}" exec -T app python -c "from noteshub.ocr import TencentOCR; print('OCR enabled/configured:',TencentOCR().configured())"
    ;;
  network)
    "${compose[@]}" exec -T app python -c 'import os,ssl,urllib.request,urllib.error
try:
 context=ssl.create_default_context(cafile=os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or None)
 with urllib.request.urlopen("https://ocr.tencentcloudapi.com",context=context,timeout=10) as response: code=response.status
except urllib.error.HTTPError as error:
 code=error.code
except Exception:
 raise SystemExit("HTTPS/TLS connectivity failed; inspect network and CA settings. No OCR action, image or credentials were sent.")
print("HTTPS returned HTTP",code,"; no OCR action, image or credentials sent; authorization not verified.")
if code not in (200,400,404,405): raise SystemExit("Unexpected HTTP response; do not treat this as ready.")'
    ;;
  summary)
    "${compose[@]}" exec -T app python -c 'import sqlite3
with sqlite3.connect("/data/noteshub.sqlite3") as db:
 for table in ("books","image_drafts","notes","ocr_runs"): print(table,db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0])
 print("OCR states:",db.execute("SELECT state,COUNT(*) FROM ocr_runs GROUP BY state").fetchall())
 print("Draft lengths/retention:",db.execute("SELECT length(original_text),length(first_comment),editing_started FROM image_drafts").fetchall())'
    ;;
  stop) "${compose[@]}" stop app ;;
  down) "${compose[@]}" down; echo "Test volume retained: $volume. No credentials or data removed." ;;
esac
