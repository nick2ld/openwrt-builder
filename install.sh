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
  local api_url="https://api.github.com/repos/${REPO}/tarball/${REF}"
  local curl_args=(-fsSL --retry 3 --retry-delay 2)

  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl_args+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
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

  log "installing systemd service"
  install -m 0644 "$SOURCE_DIR/openwrt-builder.service" "$SERVICE_FILE"
  sed -i \
    -e "s#^User=.*#User=${APP_USER}#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=${APP_DIR}#" \
    -e "s#^Environment=OWB_DATA=.*#Environment=OWB_DATA=${DATA_DIR}#" \
    -e "s#^ExecStart=.*#ExecStart=/usr/bin/python3 ${APP_DIR}/app.py#" \
    "$SERVICE_FILE"

  chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
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

print_summary() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] || ip="127.0.0.1"

  cat <<EOF

Installed OpenWrt Builder.

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
  install_files
  configure_defaults
  start_service
  print_summary
}

main "$@"
