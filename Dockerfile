# linux-autobook — panel, gateway and worker in one multi-architecture image.
#
# The image works on linux/amd64 and linux/arm64.  Build it with:
#   docker buildx build --platform linux/amd64,linux/arm64 -t <repo>/linux-autobook:latest --push .
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="linux-autobook" \
      org.opencontainers.image.description="文献传递集群：管理面板 + 百度下载网关 + 任务 Worker" \
      org.opencontainers.image.source="https://github.com/moli-xia/linux-autobook" \
      org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AUTOBOOK_CONTAINER=1 \
    AUTOBOOK_SUPERVISOR=internal \
    ADMIN_CONFIG_DIR=/etc/linux-autobook \
    ADMIN_INSTALL_DIR=/opt/autobook-linux \
    ADMIN_BIND=0.0.0.0 \
    ADMIN_PORT=8766 \
    TZ=Asia/Shanghai

# p7zip-full and aria2 are the two external commands the pipeline shells out to;
# openssl generates the self-signed certificates on first boot.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        p7zip-full \
        p7zip-rar \
        aria2 \
        openssl \
        ca-certificates \
        curl \
        tzdata \
        procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/autobook-linux

# Dependencies first so application edits do not invalidate the wheel layer.
COPY requirements.txt ./
RUN python -m venv .venv \
 && .venv/bin/pip install --upgrade pip \
 && .venv/bin/pip install -r requirements.txt

COPY autobook_linux ./autobook_linux
COPY tests ./tests
COPY deploy ./deploy
COPY docs ./docs
COPY run_admin.py run_gateway.py run_worker.py install.sh password.example.txt README.md ./
COPY docker/entrypoint.sh /usr/local/bin/autobook-entrypoint
RUN chmod +x /usr/local/bin/autobook-entrypoint \
 && mkdir -p /etc/linux-autobook /opt/autobook-linux/runtime/logs \
 && .venv/bin/python -m unittest discover -s tests -q

VOLUME ["/etc/linux-autobook", "/opt/autobook-linux/runtime"]
EXPOSE 8766 8765

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -kfsS "https://127.0.0.1:${ADMIN_PORT}/health" || exit 1

ENTRYPOINT ["/usr/local/bin/autobook-entrypoint"]
CMD ["panel"]
