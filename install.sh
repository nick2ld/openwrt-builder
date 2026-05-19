#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${REPO:-nick2ld/openwrt-builder}"
REF="${REF:-main}"
APP_USER="${APP_USER:-openwrtbuilder}"
APP_DIR="${APP_DIR:-/opt/openwrt-builder}"
DATA_DIR="${DATA_DIR:-/var/lib/openwrt-builder}"
PORT="${PORT:-8088}"
SERVICE_NAME="${SERVICE_NAME:-openwrt-builder}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
UPDATER_FILE="/usr/local/sbin/openwrt-builder-update"
SUDOERS_FILE="/etc/sudoers.d/openwrt-builder-update"
UPDATE_SCRIPT_FILE="/usr/local/sbin/openwrt-builder-update-run"
TMPDIR=""

log() {
  printf '[openwrt-builder] %s\n' "$*"
}

die() {
  printf '[openwrt-builder] ERROR: %s\n' "$*" >&2
  exit 1
}

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "run as root, for example: curl ... | sudo bash"
  fi
}

detect_os() {
  if [ ! -r /etc/os-release ]; then
    die "cannot read /etc/os-release"
  fi
  . /etc/os-release
  OS_ID="${ID:-unknown}"
  OS_LIKE="${ID_LIKE:-}"
  case " ${OS_ID} ${OS_LIKE} " in
    *" debian "*|*" ubuntu "*) ;;
    *) die "unsupported OS '${OS_ID}'. Use Debian or Ubuntu LXC." ;;
  esac
}

apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  log "updating apt metadata"
  apt-get update

  local packages=(
    bash ca-certificates curl wget git
    python3
    build-essential make gcc g++
    gawk gettext unzip rsync file time
    tar zstd xz-utils bzip2 patch perl
    libncurses-dev libssl-dev zlib1g-dev
    libelf-dev libtool autoconf automake
    flex bison
    sudo
  )

  log "installing build and runtime dependencies"
  apt-get install -y --no-install-recommends "${packages[@]}"

  local optional=(python3-distutils python3-setuptools)
  for pkg in "${optional[@]}"; do
    local candidate
    candidate="$(apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/ {print $2}')"
    if [ -n "$candidate" ] && [ "$candidate" != "(none)" ]; then
      apt-get install -y --no-install-recommends "$pkg"
    else
      log "optional package not available, skipping: $pkg"
    fi
  done
}

download_source() {
  local tmpdir="$1"
  local local_dir
  local_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$PWD/install.sh}")" >/dev/null 2>&1 && pwd || true)"

  if [ -n "$local_dir" ] && [ -f "$local_dir/app.py" ] && [ -f "$local_dir/openwrt-builder.service" ]; then
    log "using local source directory: $local_dir"
    SOURCE_DIR="$local_dir"
    return
  fi

  command -v curl >/dev/null 2>&1 || die "curl is required"

  local tarball="$tmpdir/source.tar.gz"
  local ref_json="$tmpdir/ref.json"
  local api_url="https://api.github.com/repos/${REPO}/tarball/${REF}"
  local ref_url="https://api.github.com/repos/${REPO}/commits/${REF}"
  local curl_args=(-fsSL --retry 3 --retry-delay 2)

  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl_args+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
  fi

  if curl "${curl_args[@]}" "$ref_url" -o "$ref_json"; then
    SOURCE_COMMIT="$(python3 - "$ref_json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.load(fh).get("sha", ""))
PY
)"
  fi

  log "downloading ${REPO}@${REF}"
  if ! curl "${curl_args[@]}" "$api_url" -o "$tarball"; then
    die "cannot download repository. For a private repo, pass GITHUB_TOKEN with Contents: read permission."
  fi

  mkdir -p "$tmpdir/src"
  tar -xzf "$tarball" -C "$tmpdir/src" --strip-components=1
  SOURCE_DIR="$tmpdir/src"
}

install_files() {
  [ -f "$SOURCE_DIR/app.py" ] || die "app.py not found in source"
  [ -f "$SOURCE_DIR/openwrt-builder.service" ] || die "openwrt-builder.service not found in source"

  log "creating user and directories"
  if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
  fi

  install -d -m 0755 "$APP_DIR"
  install -d -m 0750 "$DATA_DIR"
  install -d -m 0750 "$DATA_DIR/downloads" "$DATA_DIR/builders" "$DATA_DIR/firmware" "$DATA_DIR/logs"

  log "installing application into $APP_DIR"
  install -m 0755 "$SOURCE_DIR/app.py" "$APP_DIR/app.py"
  install -m 0644 "$SOURCE_DIR/README.md" "$APP_DIR/README.md" 2>/dev/null || true
  install -m 0644 "$SOURCE_DIR/example-config.json" "$APP_DIR/example-config.json" 2>/dev/null || true
  if [ -d "$SOURCE_DIR/locales" ]; then
    install -d -m 0755 "$APP_DIR/locales"
    install -m 0644 "$SOURCE_DIR/locales/"*.json "$APP_DIR/locales/" 2>/dev/null || true
  fi
  printf '%s\n' "${REF}" >"$APP_DIR/VERSION"
  if [ -d "$SOURCE_DIR/.git" ]; then
    git -C "$SOURCE_DIR" rev-parse HEAD >"$APP_DIR/COMMIT" 2>/dev/null || true
  elif [ -n "${SOURCE_COMMIT:-}" ]; then
    printf '%s\n' "$SOURCE_COMMIT" >"$APP_DIR/COMMIT"
  fi

  log "installing systemd service"
  install -m 0644 "$SOURCE_DIR/openwrt-builder.service" "$SERVICE_FILE"
  sed -i \
    -e "s#^User=.*#User=${APP_USER}#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=${APP_DIR}#" \
    -e "s#^Environment=OWB_DATA=.*#Environment=OWB_DATA=${DATA_DIR}#" \
    -e "s#^ExecStart=.*#ExecStart=/usr/bin/python3 ${APP_DIR}/app.py#" \
    -e "s#^NoNewPrivileges=.*#NoNewPrivileges=false#" \
    "$SERVICE_FILE"

  chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
}

install_updater() {
  log "installing root updater helper"
  cat >"$UPDATE_SCRIPT_FILE" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | REPO="${REPO}" REF="main" APP_USER="${APP_USER}" APP_DIR="${APP_DIR}" DATA_DIR="${DATA_DIR}" PORT="${PORT}" SERVICE_NAME="${SERVICE_NAME}" bash
EOF
  chmod 0755 "$UPDATE_SCRIPT_FILE"
  chown root:root "$UPDATE_SCRIPT_FILE"

  cat >"$UPDATER_FILE" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
mkdir -p "${DATA_DIR}/logs"
UNIT="openwrt-builder-self-update-\$(date +%s)"
echo "[\$(date -Is)] starting \$UNIT" >>"${DATA_DIR}/logs/self-update.log"
exec systemd-run \
  --unit="\$UNIT" \
  --collect \
  --property=Type=oneshot \
  --property=WorkingDirectory=/tmp \
  --property=StandardOutput=append:${DATA_DIR}/logs/self-update.log \
  --property=StandardError=append:${DATA_DIR}/logs/self-update.log \
  ${UPDATE_SCRIPT_FILE}
EOF
  chmod 0755 "$UPDATER_FILE"
  chown root:root "$UPDATER_FILE"

  cat >"$SUDOERS_FILE" <<EOF
${APP_USER} ALL=(root) NOPASSWD: ${UPDATER_FILE}
EOF
  chmod 0440 "$SUDOERS_FILE"
  visudo -cf "$SUDOERS_FILE" >/dev/null
}

configure_defaults() {
  local config="$DATA_DIR/config.json"
  if [ -f "$config" ]; then
    log "keeping existing config: $config"
    return
  fi

  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="127.0.0.1"

  log "creating initial config"
  cat >"$config" <<EOF
{
  "listen_host": "0.0.0.0",
  "listen_port": ${PORT},
  "public_base_url": "http://${ip}:${PORT}",
  "release_branch_prefix": "25.",
  "check_interval_minutes": 360,
  "build_threads": 1,
  "keep_builders": 2,
  "allow_untrusted_apk": true,
  "routers": [],
  "package_sources": []
}
EOF
  chown "$APP_USER:$APP_USER" "$config"
  chmod 0640 "$config"
}

start_service() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found. Use a systemd-based Debian/Ubuntu LXC."

  log "starting ${SERVICE_NAME}"
  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME"
}

stop_service_if_exists() {
  command -v systemctl >/dev/null 2>&1 || return 0
  if systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      log "stopping ${SERVICE_NAME}"
      systemctl stop "$SERVICE_NAME"
    fi
  fi
}

backup_current_install() {
  if [ ! -d "$APP_DIR" ]; then
    return
  fi
  local stamp backup_dir
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup_dir="${DATA_DIR}/backups/app-${stamp}"
  log "backing up current app to ${backup_dir}"
  install -d -m 0750 "$(dirname "$backup_dir")"
  mkdir -p "$backup_dir"
  cp -a "$APP_DIR/." "$backup_dir/"
  chown -R "$APP_USER:$APP_USER" "$(dirname "$backup_dir")" 2>/dev/null || true
}

print_summary() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="127.0.0.1"

  cat <<EOF

Installed OpenWrt Custom Local Builder.

Web UI:
  http://${ip}:${PORT}

Service:
  systemctl status ${SERVICE_NAME}
  journalctl -u ${SERVICE_NAME} -f

Data:
  ${DATA_DIR}

EOF
}

main() {
  need_root
  detect_os
  TMPDIR="$(mktemp -d)"
  trap 'if [ -n "${TMPDIR:-}" ]; then rm -rf "$TMPDIR"; fi' EXIT

  apt_install
  download_source "$TMPDIR"
  stop_service_if_exists
  backup_current_install
  install_files
  install_updater
  configure_defaults
  start_service
  print_summary
}

main "$@"
