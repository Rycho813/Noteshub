# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NOTESHUB_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt constraints.txt ./
# Optional public CA bundle for managed build environments. No CA or proxy
# credential is copied into the image; normal servers use the image's trust.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 noteshub ./noteshub
RUN mkdir -m 0700 /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/login', timeout=3).close()"
CMD ["python", "-m", "uvicorn", "noteshub.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
