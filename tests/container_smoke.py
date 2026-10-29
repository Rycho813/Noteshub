"""Local-only container + HTTPS + restart + backup/restore integration check.

Requires the noteshub:local image already built. Uses unique Compose projects,
random loopback ports and throwaway volumes; never touches a remote Docker host.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import time

import httpx


ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
SELECTORS = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def token(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def run():
    project = "noteshub-test-" + secrets.token_hex(4)
    restored = project + "-restore"
    env = {key: value for key, value in os.environ.items() if key not in SELECTORS}
    env.update({"COMPOSE_PROJECT_NAME": project, "NOTESHUB_IMAGE_TAG": "local",
                "NOTESHUB_HTTP_PORT": str(port()), "NOTESHUB_DOMAIN": "localhost",
                "NOTESHUB_TLS_BIND": "127.0.0.1", "NOTESHUB_TLS_HTTP_PORT": str(port()),
                "NOTESHUB_TLS_HTTPS_PORT": str(port()), "NOTESHUB_COOKIE_SECURE": "true",
                "NOTESHUB_TEST_PASSWORD": secrets.token_urlsafe(24)})
    base = [*DOCKER, "compose", "--env-file", "/dev/null", "-f", "docker-compose.yml"]
    https = [*base, "-f", "docker-compose.https.yml"]
    restored_env = {**env, "COMPOSE_PROJECT_NAME": restored, "NOTESHUB_HTTP_PORT": str(port()),
                    "NOTESHUB_COOKIE_SECURE": "false"}

    def command(arguments, *, execution_env=env, **kwargs):
        return subprocess.run(arguments, cwd=ROOT, env=execution_env, check=True,
                              capture_output=True, **kwargs)

    def ready(url, client):
        last_error = None
        last_status = None
        for _ in range(80):
            try:
                response = client.get(url + "/login", timeout=2)
                last_status = response.status_code
                if last_status in (200, 303):
                    return
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(.25)
        raise RuntimeError(f"local service not ready: HTTP {last_status}, {last_error!r}")

    def login(client, url):
        response = client.get(url + "/login")
        result = client.post(url + "/login", data={"csrf": token(response), "username": "container-reader",
                                                   "password": env["NOTESHUB_TEST_PASSWORD"]})
        assert result.status_code == 303, result.status_code

    try:
        # Named volume initialization must work as the image's non-root user.
        command([*base, "run", "--rm", "--no-deps", "-T", "-e", "NOTESHUB_TEST_PASSWORD", "app", "python", "-c",
                 "import os; from pathlib import Path; from noteshub.auth import provision; provision(Path('/data'), 'container-reader', os.environ['NOTESHUB_TEST_PASSWORD'])"])
        command([*https, "up", "-d", "--no-build"])
        caddy = command([*https, "ps", "-q", "caddy"]).stdout.decode().strip()
        app = command([*https, "ps", "-q", "app"]).stdout.decode().strip()
        config = json.loads(command([*DOCKER, "inspect", "--format", "{{json .HostConfig}}", app]).stdout)
        assert config["ReadonlyRootfs"]
        assert all(binding["HostIp"] == "127.0.0.1" for bindings in config["PortBindings"].values() for binding in bindings)
        assert command([*DOCKER, "exec", app, "id", "-u"]).stdout.strip() == b"10001"
        with tempfile.TemporaryDirectory(prefix="noteshub-container-") as tmp:
            tmp = Path(tmp)
            ca = tmp / "caddy-root.crt"
            for _ in range(40):
                result = subprocess.run([*DOCKER, "cp", f"{caddy}:/data/caddy/pki/authorities/local/root.crt", str(ca)],
                                        env=env, capture_output=True)
                if result.returncode == 0:
                    break
                time.sleep(.25)
            else:
                raise RuntimeError("Caddy local test certificate unavailable")
            # Trust the test CA explicitly. TLS and hostname checks stay enabled.
            import ssl
            tls = ssl.create_default_context(cafile=str(ca))
            url = "https://localhost:" + env["NOTESHUB_TLS_HTTPS_PORT"]
            with httpx.Client(verify=tls, trust_env=False, timeout=10) as client:
                ready(url, client)
                login(client, url)
                assert client.cookies.get("noteshub_session")
                page = client.get(url + "/")
                created = client.post(url + "/books", data={"csrf": token(page), "title": "容器恢复验收"})
                book = created.headers["location"]
                upload = client.post(url + book + "/imports", data={"csrf": token(page)},
                                     files={"file": ("notes.md", "“容器原文”一读评述".encode(), "text/markdown")})
                preview = upload.headers["location"]
                assert client.post(url + preview + "/confirm", data={"csrf": token(page)}).status_code == 303
                page = client.get(url + book)
                assert 'src="/static/review.js"' in page.text
                assert client.get(url + "/static/review.js").status_code == 200
                api = re.search(r'data-save-url="([^"]+)"', page.text)[1]
                saved = client.post(url + api, data={"csrf": token(page), "second_comment": "容器里的二读", "status": "done"})
                assert saved.status_code == 200
                expected = client.get(url + book + "/export").content
                original = client.get(url + book + "/source").content
                command([*base, "restart", "app"])
                ready(url, client)
                assert client.get(url + book + "/export").content == expected
                archive = tmp / "backup.tar.gz"
                command(["bash", "scripts/backup.sh", str(archive)])
                assert archive.is_file() and archive.stat().st_mode & 0o077 == 0
                assert archive.with_suffix(archive.suffix + ".sha256").is_file()
                ready(url, client)
                assert client.get(url + book + "/export").content == expected

            command(["bash", "scripts/restore.sh", str(archive)], execution_env=restored_env)
            command([*base, "up", "-d", "--no-build", "app"], execution_env=restored_env)
            restored_url = "http://127.0.0.1:" + restored_env["NOTESHUB_HTTP_PORT"]
            with httpx.Client(trust_env=False, timeout=10) as client:
                ready(restored_url, client)
                assert client.get(restored_url + book + "/export").status_code == 303
                login(client, restored_url)
                assert client.get(restored_url + book + "/export").content == expected
                assert client.get(restored_url + book + "/source").content == original
                assert "已完成 1/1" in client.get(restored_url + "/").text
            # Existing nonempty restore data must not be replaced on a rerun.
            command([*base, "stop", "app"], execution_env=restored_env)
            refusal = subprocess.run(["bash", "scripts/restore.sh", str(archive)], cwd=ROOT,
                                     env=restored_env, capture_output=True)
            assert refusal.returncode != 0
        print("Container smoke passed: non-root/read-only app, loopback port, trusted local HTTPS, import/review/export, restart, backup, empty-volume restore, existing-data refusal.")
    except BaseException:
        # Only operational logs, never inspect environment/credential values.
        result = subprocess.run([*https, "logs", "--tail", "25"], cwd=ROOT, env=env, capture_output=True)
        print(result.stdout.decode(errors="replace"))
        raise
    finally:
        # Unique test projects only; never use this flag on production data.
        for current_env, files in ((env, https), (restored_env, base)):
            subprocess.run([*files, "down", "-v", "--remove-orphans"], cwd=ROOT, env=current_env,
                           capture_output=True, check=False)


if __name__ == "__main__":
    run()
