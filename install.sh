#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="/opt/autobook-linux"
CONFIG_DIR="/etc/linux-autobook"
ADMIN_PORT="8766"
ADMIN_BIND="0.0.0.0"
PUBLIC_HOST=""
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "Usage: sudo bash install.sh [--install-dir PATH] [--admin-port PORT] [--admin-bind ADDRESS] [--public-host HOST]"
}

while (($#)); do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --admin-port) ADMIN_PORT="$2"; shift 2 ;;
    --admin-bind) ADMIN_BIND="$2"; shift 2 ;;
    --public-host) PUBLIC_HOST="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo bash install.sh" >&2
  exit 1
fi
if [[ ! "$ADMIN_PORT" =~ ^[0-9]{2,5}$ ]] || ((ADMIN_PORT < 1 || ADMIN_PORT > 65535)); then
  echo "Invalid admin port: $ADMIN_PORT" >&2
  exit 2
fi
if [[ ! "$INSTALL_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
  echo "Invalid install directory: $INSTALL_DIR" >&2
  exit 2
fi
if [[ ! "$ADMIN_BIND" =~ ^[A-Za-z0-9.:-]+$ ]]; then
  echo "Invalid admin bind address: $ADMIN_BIND" >&2
  exit 2
fi
if [[ ! -f "$SOURCE_DIR/run_admin.py" || ! -d "$SOURCE_DIR/autobook_linux" ]]; then
  echo "install.sh must be run from a complete linux-autobook checkout." >&2
  exit 1
fi

echo "[1/7] Installing system dependencies..."
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-venv python3-pip p7zip-full aria2 openssl ca-certificates
else
  echo "This installer currently supports Debian/Ubuntu (apt-get)." >&2
  exit 1
fi

echo "[2/7] Installing application files..."
id autobook >/dev/null 2>&1 || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin autobook
install -d -m 755 "$INSTALL_DIR"
if [[ "$(readlink -f "$SOURCE_DIR")" != "$(readlink -f "$INSTALL_DIR")" ]]; then
  tar -C "$SOURCE_DIR" --exclude=.git --exclude=.env --exclude=runtime --exclude=password.txt --exclude=.venv -cf - . | tar -C "$INSTALL_DIR" -xf -
fi
install -d -m 750 -o autobook -g autobook "$INSTALL_DIR/runtime"
install -d -m 750 -o root -g autobook "$CONFIG_DIR"
if [[ ! -f "$INSTALL_DIR/password.txt" ]]; then
  install -m 600 -o root -g root "$INSTALL_DIR/password.example.txt" "$INSTALL_DIR/password.txt"
fi

echo "[3/7] Creating Python environment..."
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "[4/7] Creating TLS certificates and configuration..."
if [[ -z "$PUBLIC_HOST" ]]; then
  PUBLIC_HOST="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+(\.[0-9]+){3}$' | head -n1 || true)"
fi
if [[ ! "$PUBLIC_HOST" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  echo "Invalid public host: $PUBLIC_HOST" >&2
  exit 2
fi
if [[ -z "$PUBLIC_HOST" ]]; then
  PUBLIC_HOST="$(hostname -f 2>/dev/null || hostname)"
fi
if [[ "$PUBLIC_HOST" =~ ^[0-9]+(\.[0-9]+){3}$ || "$PUBLIC_HOST" == *:* ]]; then
  PUBLIC_SAN="IP:${PUBLIC_HOST}"
else
  PUBLIC_SAN="DNS:${PUBLIC_HOST}"
fi

generate_cert() {
  local cert="$1" key="$2" common_name="$3"
  if [[ ! -s "$cert" || ! -s "$key" ]]; then
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 \
      -keyout "$key" -out "$cert" -subj "/CN=${common_name}" \
      -addext "subjectAltName=${PUBLIC_SAN},DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
  fi
  chmod 644 "$cert"
  chmod 640 "$key"
}

generate_cert "$CONFIG_DIR/admin.crt" "$CONFIG_DIR/admin.key" "$PUBLIC_HOST"
chown root:root "$CONFIG_DIR/admin.crt" "$CONFIG_DIR/admin.key"
generate_cert "$INSTALL_DIR/runtime/gateway.crt" "$INSTALL_DIR/runtime/gateway.key" "$PUBLIC_HOST"
chown autobook:autobook "$INSTALL_DIR/runtime/gateway.crt" "$INSTALL_DIR/runtime/gateway.key"

if [[ ! -f "$CONFIG_DIR/admin.env" ]]; then
  install -m 600 /dev/null "$CONFIG_DIR/admin.env"
  printf '%s\n' \
    "ADMIN_BIND=$ADMIN_BIND" \
    "ADMIN_PORT=$ADMIN_PORT" \
    "ADMIN_TLS_CERT=$CONFIG_DIR/admin.crt" \
    "ADMIN_TLS_KEY=$CONFIG_DIR/admin.key" \
    "ADMIN_STATE_FILE=$CONFIG_DIR/admin-state.json" \
    "ADMIN_GATEWAY_ENV=$CONFIG_DIR/gateway.env" \
    "ADMIN_WORKER_ENV=$CONFIG_DIR/worker.env" \
    "ADMIN_SESSION_SECONDS=28800" \
    "ADMIN_PUBLIC_HOST=$PUBLIC_HOST" > "$CONFIG_DIR/admin.env"
fi

SHARED_TOKEN=""
if [[ -f "$CONFIG_DIR/gateway.env" ]]; then
  SHARED_TOKEN="$(sed -n 's/^BAIDU_GATEWAY_TOKEN=//p' "$CONFIG_DIR/gateway.env" | tail -n1 | tr -d '"')"
fi
if [[ -z "$SHARED_TOKEN" && -f "$CONFIG_DIR/worker.env" ]]; then
  SHARED_TOKEN="$(sed -n 's/^BAIDU_GATEWAY_TOKEN=//p' "$CONFIG_DIR/worker.env" | tail -n1 | tr -d '"')"
fi
if [[ -z "$SHARED_TOKEN" ]]; then SHARED_TOKEN="$(openssl rand -hex 32)"; fi
if [[ ! -f "$CONFIG_DIR/gateway.env" ]]; then
  install -m 600 /dev/null "$CONFIG_DIR/gateway.env"
  printf '%s\n' \
    "BAIDU_GATEWAY_TOKEN=$SHARED_TOKEN" \
    "BAIDU_AUTH_FILE=$INSTALL_DIR/runtime/baidu_credentials.json" \
    "BAIDU_GROUP_NAME=读秀12群" \
    "BAIDU_GROUP_GID=498636198303058255" \
    "BAIDU_SAVE_DIR=/autobook_inbox" \
    "GATEWAY_BIND=0.0.0.0" \
    "GATEWAY_PORT=8765" \
    "GATEWAY_TLS_CERT=$INSTALL_DIR/runtime/gateway.crt" \
    "GATEWAY_TLS_KEY=$INSTALL_DIR/runtime/gateway.key" \
    "GATEWAY_CONCURRENCY=3" \
    "GATEWAY_CACHE_TTL_SECONDS=3600" \
    "GATEWAY_JOB_ROOT=$INSTALL_DIR/runtime/gateway/jobs" > "$CONFIG_DIR/gateway.env"
fi
if [[ ! -f "$CONFIG_DIR/worker.env" ]]; then
  install -m 600 /dev/null "$CONFIG_DIR/worker.env"
  printf '%s\n' \
    "SITE_BASE_URL=https://544544.xyz" \
    "WORKER_ID=$(hostname -s)-worker" \
    "WORKER_QUEUE=pdf" \
    "POLL_SECONDS=15" \
    "CONCURRENCY=3" \
    "LEASE_HEARTBEAT_SECONDS=60" \
    "BAIDU_GATEWAY_URL=https://127.0.0.1:8765" \
    "BAIDU_GATEWAY_TOKEN=$SHARED_TOKEN" \
    "BAIDU_GATEWAY_CA_FILE=$INSTALL_DIR/runtime/gateway.crt" \
    "WORK_ROOT=$INSTALL_DIR/runtime/work" \
    "DOWNLOAD_ROOT=$INSTALL_DIR/runtime/downloads" \
    "PASSWORD_DICT=$INSTALL_DIR/password.txt" \
    "DRIVE_BASE_URL=https://drive.netupdown.com" \
    "DRIVE_TARGET_DIR=transfer" \
    "DRIVE_EXPIRE_DAYS=7" \
    "PDG_DPI=200" > "$CONFIG_DIR/worker.env"
fi
chmod 600 "$CONFIG_DIR"/*.env

echo "[5/7] Installing systemd services..."
sed "s|/opt/autobook-linux|$INSTALL_DIR|g" "$INSTALL_DIR/deploy/autobook-admin.service" > /etc/systemd/system/autobook-admin.service
sed "s|/opt/autobook-linux|$INSTALL_DIR|g" "$INSTALL_DIR/deploy/autobook-gateway.service" > /etc/systemd/system/autobook-gateway.service
sed "s|/opt/autobook-linux|$INSTALL_DIR|g" "$INSTALL_DIR/deploy/autobook-worker.service" > /etc/systemd/system/autobook-worker.service
chmod 644 /etc/systemd/system/autobook-admin.service /etc/systemd/system/autobook-gateway.service /etc/systemd/system/autobook-worker.service
systemctl daemon-reload
systemctl enable --now autobook-admin.service
if systemctl is-active --quiet autobook-gateway.service; then systemctl restart autobook-gateway.service; fi
if systemctl is-active --quiet autobook-worker.service; then systemctl restart autobook-worker.service; fi

echo "[6/7] Running tests..."
cd "$INSTALL_DIR"
"$INSTALL_DIR/.venv/bin/python" -m unittest discover -s tests -q

echo "[7/7] Opening firewall port when UFW is active..."
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow "${ADMIN_PORT}/tcp" comment 'linux-autobook admin' >/dev/null
fi

if [[ "$PUBLIC_HOST" == *:* ]]; then
  PANEL_URL="https://[${PUBLIC_HOST}]:${ADMIN_PORT}/"
else
  PANEL_URL="https://${PUBLIC_HOST}:${ADMIN_PORT}/"
fi
echo
echo "============================================================"
echo " linux-autobook installation completed"
echo " Admin panel: $PANEL_URL"
echo " Default username: admin"
echo " Default password: admin"
echo " Change the default password immediately after first login."
echo " A browser warning is expected for the generated self-signed certificate."
echo " Gateway/Worker remain stopped until their required credentials are configured."
echo "============================================================"
