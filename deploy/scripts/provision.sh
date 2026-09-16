#!/usr/bin/env bash
set -euo pipefail

# Provision and harden a fresh Ubuntu 22.04.5 LTS host for the tts-worker stack.
#
# Run as root on the VPS (see DEPLOYMENT.md section 2):
#   sudo ADMIN_USER=deploy ./provision.sh
#
# Covers:
#   - base packages + docker-ce, docker-compose-plugin, caddy, ufw, fail2ban,
#     unattended-upgrades, curl, jq, htop
#   - time sync (systemd-timesyncd, falling back to chrony)
#   - non-root admin user with sudo + docker access and root's authorized_keys
#   - SSH hardening (key-only, no root login, no password auth)
#   - ufw allowing only 22/80/443
#   - Docker log rotation
#   - /opt/tts-worker on-disk layout, 2 GB swap, and a root-owned 640 .env
#
# The cloud provider firewall must mirror ufw (22/80/443) and cannot be changed here.

ADMIN_USER="${ADMIN_USER:-deploy}"
SSH_PORT="${SSH_PORT:-22}"
APP_DIR="${APP_DIR:-/opt/tts-worker}"
SWAP_SIZE="${SWAP_SIZE:-2G}"
PUBLIC_DOMAIN="${PUBLIC_DOMAIN:-tts-api.aitransclips.com}"

log() { printf '\n==> %s\n' "$*"; }

require_root() {
	if [[ "${EUID}" -ne 0 ]]; then
		echo "This script must run as root." >&2
		exit 1
	fi
}

require_root

log "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
	ca-certificates curl gnupg lsb-release jq htop ufw fail2ban unattended-upgrades

log "Installing Docker CE from the official repository"
install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
	curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
	chmod a+r /etc/apt/keyrings/docker.gpg
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
	> /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

log "Installing Caddy from the official repository"
if [[ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]]; then
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
		| gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
		> /etc/apt/sources.list.d/caddy-stable.list
fi
apt-get update
apt-get install -y caddy

log "Ensuring time sync is healthy"
if ! systemctl is-active --quiet systemd-timesyncd; then
	apt-get install -y chrony
	systemctl enable --now chrony
fi
timedatectl status || true

log "Creating admin user '${ADMIN_USER}'"
if ! id -u "${ADMIN_USER}" >/dev/null 2>&1; then
	useradd --create-home --shell /bin/bash "${ADMIN_USER}"
fi
usermod -aG sudo,docker "${ADMIN_USER}"
if [[ -f /root/.ssh/authorized_keys ]]; then
	install -d -m 700 -o "${ADMIN_USER}" -g "${ADMIN_USER}" "/home/${ADMIN_USER}/.ssh"
	install -m 600 -o "${ADMIN_USER}" -g "${ADMIN_USER}" \
		/root/.ssh/authorized_keys "/home/${ADMIN_USER}/.ssh/authorized_keys"
fi

log "Granting ${ADMIN_USER} passwordless sudo"
# The admin user is key-only (no password), so sudo must not prompt for one.
echo "${ADMIN_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-${ADMIN_USER}.tmp"
visudo -cf "/etc/sudoers.d/90-${ADMIN_USER}.tmp"
chmod 440 "/etc/sudoers.d/90-${ADMIN_USER}.tmp"
mv "/etc/sudoers.d/90-${ADMIN_USER}.tmp" "/etc/sudoers.d/90-${ADMIN_USER}"

log "Hardening SSH"
# Refuse to disable password auth unless the admin user can already log in with a key.
if [[ ! -s "/home/${ADMIN_USER}/.ssh/authorized_keys" ]]; then
	echo "No SSH key found for ${ADMIN_USER}. Install one (or /root/.ssh/authorized_keys) and re-run." >&2
	exit 1
fi
install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-tts-worker-hardening.conf <<EOF
Port ${SSH_PORT}
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd

log "Configuring ufw (22/80/443 only)"
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp"
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose

log "Configuring Docker log rotation"
install -d -m 755 /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
systemctl restart docker

log "Creating ${APP_DIR} layout"
install -d -m 750 "${APP_DIR}" "${APP_DIR}/releases" "${APP_DIR}/scripts"
install -d -m 750 "${APP_DIR}/data" "${APP_DIR}/data/hf-cache" "${APP_DIR}/data/output"

# Copy the deploy assets from this repository checkout into APP_DIR.
DEPLOY_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${DEPLOY_SRC}/docker-compose.yml" && "${DEPLOY_SRC}" != "${APP_DIR}" ]]; then
	log "Copying deploy assets from ${DEPLOY_SRC}"
	install -m 644 "${DEPLOY_SRC}/docker-compose.yml" "${APP_DIR}/docker-compose.yml"
	install -m 644 "${DEPLOY_SRC}/Caddyfile" "${APP_DIR}/Caddyfile"
	install -m 640 "${DEPLOY_SRC}/.env.example" "${APP_DIR}/.env.example"
	install -m 755 "${DEPLOY_SRC}"/scripts/*.sh "${APP_DIR}/scripts/"
fi

chown -R "${ADMIN_USER}:${ADMIN_USER}" "${APP_DIR}"

if [[ ! -f "${APP_DIR}/.env" && -f "${APP_DIR}/.env.example" ]]; then
	install -m 640 -o root -g "${ADMIN_USER}" "${APP_DIR}/.env.example" "${APP_DIR}/.env"
	echo "Created ${APP_DIR}/.env from .env.example — fill in real secrets before deploying."
fi
if [[ -f "${APP_DIR}/.env" ]]; then
	chown root:"${ADMIN_USER}" "${APP_DIR}/.env"
	chmod 640 "${APP_DIR}/.env"
fi

log "Installing Caddyfile and enabling Caddy"
install -d -m 755 /var/log/caddy
chown caddy:caddy /var/log/caddy
if [[ -f "${APP_DIR}/Caddyfile" ]]; then
	install -m 644 "${APP_DIR}/Caddyfile" /etc/caddy/Caddyfile
fi
systemctl enable caddy
systemctl restart caddy

log "Enabling fail2ban and unattended-upgrades"
cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
backend = systemd
EOF
systemctl enable fail2ban
systemctl restart fail2ban
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades

log "Configuring ${SWAP_SIZE} swap (safety net only)"
if ! swapon --show | grep -q .; then
	if ! fallocate -l "${SWAP_SIZE}" /swapfile 2>/dev/null; then
		dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
	fi
	chmod 600 /swapfile
	mkswap /swapfile
	swapon /swapfile
	grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "Provisioning complete"
cat <<EOF

Next steps:
  1. Confirm the provider firewall allows 22/80/443 and nothing else.
  2. Fill in ${APP_DIR}/.env with real HMAC keys and set IMAGE_TAG.
  3. Point the PA Vietnam DNS A record for ${PUBLIC_DOMAIN} at this host (T10).
  4. Log in as ${ADMIN_USER} (key-only) and verify: bash ${APP_DIR}/scripts/healthcheck.sh
EOF
