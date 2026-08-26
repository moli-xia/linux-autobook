#!/usr/bin/env bash
# Container entrypoint: prepare configuration on first boot, then run the panel.
set -Eeuo pipefail

CONFIG_DIR="${ADMIN_CONFIG_DIR:-/etc/linux-autobook}"
INSTALL_DIR="${ADMIN_INSTALL_DIR:-/opt/autobook-linux}"
PYTHON="$INSTALL_DIR/.venv/bin/python"
ROLE="${AUTOBOOK_ROLE:-all}"
ADMIN_PORT="${ADMIN_PORT:-8766}"
GATEWAY_PORT="${GATEWAY_PORT:-8765}"

case "$ROLE" in
  all|gateway|worker) ;;
  *) echo "AUTOBOOK_ROLE must be all, gateway or worker (got: $ROLE)" >&2; exit 2 ;;
esac

# The public host ends up in the self-signed certificates and in panel links.
PUBLIC_HOST="${AUTOBOOK_PUBLIC_HOST:-}"
if [[ -z "$PUBLIC_HOST" ]]; then
  PUBLIC_HOST="$(hostname -i 2>/dev/null | awk '{print $1}')"
  [[ -n "$PUBLIC_HOST" ]] || PUBLIC_HOST="localhost"
fi

mkdir -p "$CONFIG_DIR" "$INSTALL_DIR/runtime/logs" "$INSTALL_DIR/runtime/gateway/jobs"
chmod 750 "$INSTALL_DIR/runtime"

set_env() {
  local file="$1" key="$2" value="$3" tmp
  tmp="${file}.tmp.$$"
  if [[ -f "$file" ]]; then
    awk -v key="$key" -v line="$key=$value" \
      'BEGIN{done=0} $0 ~ "^" key "=" {if(!done){print line; done=1}; next} {print} END{if(!done) print line}' \
      "$file" > "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" > "$tmp"
  fi
  mv "$tmp" "$file"
  chmod 600 "$file"
}

ensure_env() {
  local file="$1" key="$2" value="$3"
  grep -q "^${key}=" "$file" 2>/dev/null || set_env "$file" "$key" "$value"
}

read_env_value() {
  sed -n "s/^${2}=//p" "$1" 2>/dev/null | tail -n1 | sed 's/^"//;s/"$//'
}

# ---------------------------------------------------------------- certificates
make_cert() {
  local cert="$1" key="$2" san
  if [[ "$PUBLIC_HOST" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then san="IP:${PUBLIC_HOST}"; else san="DNS:${PUBLIC_HOST}"; fi
  local valid=0
  if [[ -s "$cert" && -s "$key" ]]; then
    if [[ "$PUBLIC_HOST" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
      openssl x509 -in "$cert" -noout -checkip "$PUBLIC_HOST" >/dev/null 2>&1 && valid=1 || true
    else
      openssl x509 -in "$cert" -noout -checkhost "$PUBLIC_HOST" >/dev/null 2>&1 && valid=1 || true
    fi
  fi
  if ((valid == 0)); then
    rm -f "$cert" "$key"
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 -keyout "$key" -out "$cert" \
      -subj "/CN=${PUBLIC_HOST}" \
      -addext "subjectAltName=${san},DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
    echo "generated self-signed certificate for ${PUBLIC_HOST}: $cert"
  fi
  chmod 644 "$cert"; chmod 640 "$key"
}

make_cert "$CONFIG_DIR/admin.crt" "$CONFIG_DIR/admin.key"

# ----------------------------------------------------------------- role state
cat > "$CONFIG_DIR/install.env" <<EOF
INSTALL_DIR=$INSTALL_DIR
CONFIG_DIR=$CONFIG_DIR
INSTALL_ROLE=$ROLE
PUBLIC_HOST=$PUBLIC_HOST
TLS_MODE=self-signed
ADMIN_PORT=$ADMIN_PORT
DEPLOYMENT=docker
EOF
chmod 600 "$CONFIG_DIR/install.env"

SHARED_TOKEN="${AUTOBOOK_GATEWAY_TOKEN:-}"
if [[ -z "$SHARED_TOKEN" && -f "$CONFIG_DIR/gateway.env" ]]; then
  SHARED_TOKEN="$(read_env_value "$CONFIG_DIR/gateway.env" BAIDU_GATEWAY_TOKEN)"
fi
if [[ -z "$SHARED_TOKEN" && -f "$CONFIG_DIR/worker.env" ]]; then
  SHARED_TOKEN="$(read_env_value "$CONFIG_DIR/worker.env" BAIDU_GATEWAY_TOKEN)"
fi
if [[ -z "$SHARED_TOKEN" && "$ROLE" != "worker" ]]; then
  SHARED_TOKEN="$(openssl rand -hex 32)"
fi

if [[ "$ROLE" == "all" || "$ROLE" == "gateway" ]]; then
  [[ -f "$CONFIG_DIR/gateway.env" ]] || : > "$CONFIG_DIR/gateway.env"
  chmod 600 "$CONFIG_DIR/gateway.env"
  ensure_env "$CONFIG_DIR/gateway.env" BAIDU_GATEWAY_TOKEN "$SHARED_TOKEN"
  ensure_env "$CONFIG_DIR/gateway.env" BAIDU_AUTH_FILE "$INSTALL_DIR/runtime/baidu_credentials.json"
  ensure_env "$CONFIG_DIR/gateway.env" BAIDU_GROUP_NAME "读秀12群"
  ensure_env "$CONFIG_DIR/gateway.env" BAIDU_GROUP_GID "498636198303058255"
  ensure_env "$CONFIG_DIR/gateway.env" BAIDU_SAVE_DIR "/autobook_inbox"
  ensure_env "$CONFIG_DIR/gateway.env" GATEWAY_BIND "0.0.0.0"
  ensure_env "$CONFIG_DIR/gateway.env" GATEWAY_PORT "$GATEWAY_PORT"
  set_env    "$CONFIG_DIR/gateway.env" GATEWAY_TLS_CERT "$INSTALL_DIR/runtime/gateway.crt"
  set_env    "$CONFIG_DIR/gateway.env" GATEWAY_TLS_KEY "$INSTALL_DIR/runtime/gateway.key"
  ensure_env "$CONFIG_DIR/gateway.env" GATEWAY_CONCURRENCY "3"
  ensure_env "$CONFIG_DIR/gateway.env" GATEWAY_CACHE_TTL_SECONDS "3600"
  ensure_env "$CONFIG_DIR/gateway.env" GATEWAY_JOB_ROOT "$INSTALL_DIR/runtime/gateway/jobs"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_ENABLED "${AUTOBOOK_PDG_FALLBACK_ENABLED:-0}"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_IMAGE "${AUTOBOOK_PDG_FALLBACK_IMAGE:-autobook-pdg2pic-wine:local}"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_DOCKER_SOCKET "/var/run/docker.sock"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_RUNTIME_VOLUME "${AUTOBOOK_PDG_FALLBACK_RUNTIME_VOLUME:-}"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_JOB_ROOT "$INSTALL_DIR/runtime/pdg-fallback/jobs"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_TIMEOUT_SECONDS "7200"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_MAX_UPLOAD_MB "1024"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_MEMORY_MB "2048"
  ensure_env "$CONFIG_DIR/gateway.env" PDG_FALLBACK_CPUS "2"
  make_cert "$INSTALL_DIR/runtime/gateway.crt" "$INSTALL_DIR/runtime/gateway.key"
fi

if [[ "$ROLE" == "all" || "$ROLE" == "worker" ]]; then
  [[ -f "$CONFIG_DIR/worker.env" ]] || : > "$CONFIG_DIR/worker.env"
  chmod 600 "$CONFIG_DIR/worker.env"
  ensure_env "$CONFIG_DIR/worker.env" SITE_BASE_URL "${AUTOBOOK_SITE_URL:-https://544544.xyz}"
  ensure_env "$CONFIG_DIR/worker.env" WORKER_TOKEN "${AUTOBOOK_WORKER_TOKEN:-}"
  ensure_env "$CONFIG_DIR/worker.env" WORKER_ID "${AUTOBOOK_WORKER_ID:-$(hostname)-worker}"
  ensure_env "$CONFIG_DIR/worker.env" WORKER_QUEUE "pdf"
  ensure_env "$CONFIG_DIR/worker.env" CONCURRENCY "${AUTOBOOK_CONCURRENCY:-2}"
  if [[ "$ROLE" == "all" ]]; then
    ensure_env "$CONFIG_DIR/worker.env" BAIDU_GATEWAY_URL "https://127.0.0.1:${GATEWAY_PORT}"
    ensure_env "$CONFIG_DIR/worker.env" BAIDU_GATEWAY_CA_FILE "$INSTALL_DIR/runtime/gateway.crt"
  else
    ensure_env "$CONFIG_DIR/worker.env" BAIDU_GATEWAY_URL "${AUTOBOOK_GATEWAY_URL:-}"
    ensure_env "$CONFIG_DIR/worker.env" BAIDU_GATEWAY_CA_FILE "${AUTOBOOK_GATEWAY_CA_FILE:-}"
  fi
  ensure_env "$CONFIG_DIR/worker.env" BAIDU_GATEWAY_TOKEN "$SHARED_TOKEN"
  ensure_env "$CONFIG_DIR/worker.env" WORK_ROOT "$INSTALL_DIR/runtime/work"
  ensure_env "$CONFIG_DIR/worker.env" DOWNLOAD_ROOT "$INSTALL_DIR/runtime/downloads"
  ensure_env "$CONFIG_DIR/worker.env" INDEX_DB "$INSTALL_DIR/runtime/library_index.sqlite3"
  ensure_env "$CONFIG_DIR/worker.env" PASSWORD_DICT "$INSTALL_DIR/runtime/password.txt"
  ensure_env "$CONFIG_DIR/worker.env" DRIVE_BASE_URL "${AUTOBOOK_DRIVE_URL:-https://drive.netupdown.com}"
  ensure_env "$CONFIG_DIR/worker.env" DRIVE_EMAIL "${AUTOBOOK_DRIVE_EMAIL:-}"
  ensure_env "$CONFIG_DIR/worker.env" DRIVE_PASSWORD "${AUTOBOOK_DRIVE_PASSWORD:-}"
  ensure_env "$CONFIG_DIR/worker.env" DRIVE_TARGET_DIR "transfer"
fi

export ADMIN_TLS_CERT="${ADMIN_TLS_CERT:-$CONFIG_DIR/admin.crt}"
export ADMIN_TLS_KEY="${ADMIN_TLS_KEY:-$CONFIG_DIR/admin.key}"
export ADMIN_STATE_FILE="${ADMIN_STATE_FILE:-$CONFIG_DIR/admin-state.json}"
export ADMIN_GATEWAY_ENV="${ADMIN_GATEWAY_ENV:-$CONFIG_DIR/gateway.env}"
export ADMIN_WORKER_ENV="${ADMIN_WORKER_ENV:-$CONFIG_DIR/worker.env}"
export ADMIN_PUBLIC_HOST="$PUBLIC_HOST"
export ADMIN_ROLE="$ROLE"

case "${1:-panel}" in
  panel)
    echo "linux-autobook 容器启动：角色=$ROLE 面板=https://${PUBLIC_HOST}:${ADMIN_PORT}/"
    exec "$PYTHON" "$INSTALL_DIR/run_admin.py"
    ;;
  gateway) exec "$PYTHON" "$INSTALL_DIR/run_gateway.py" ;;
  worker)  exec "$PYTHON" "$INSTALL_DIR/run_worker.py" ;;
  shell)   exec bash ;;
  *)       exec "$@" ;;
esac
