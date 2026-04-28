#!/usr/bin/env bash
# Bootstrap a fresh Hetzner CPX21 (Ubuntu 22.04) for QideDAM v2 production.
#
# Run as root on the server, after first SSH:
#     curl -sSL https://raw.githubusercontent.com/<your-gh>/qide-dam-v2/main/scripts/bootstrap_hetzner.sh | bash
#
# What it does:
#   1. apt update + install Docker + compose plugin + Nginx + Certbot + ufw
#   2. Create `dam` user (no shell login, just for compose)
#   3. Open ports 22 / 80 / 443 only
#   4. Optionally install Cloudflare Tunnel (skips Nginx if you go this route)
#   5. Print next-step instructions
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}==> 1. apt upgrade${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

echo -e "${GREEN}==> 2. Install Docker + compose plugin${NC}"
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi
apt-get install -y -qq docker-compose-plugin nginx certbot python3-certbot-nginx ufw fail2ban git

echo -e "${GREEN}==> 3. Create app user${NC}"
if ! id -u dam >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin dam || true
fi
usermod -aG docker dam || true

echo -e "${GREEN}==> 4. Firewall${NC}"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo -e "${GREEN}==> 5. fail2ban defaults${NC}"
systemctl enable fail2ban
systemctl start fail2ban

echo -e "${GREEN}==> 6. App directory${NC}"
mkdir -p /opt/qide-dam
chown root:docker /opt/qide-dam

echo -e "${GREEN}==> 7. (Optional) Cloudflare Tunnel${NC}"
echo "If you want Cloudflare Tunnel instead of Nginx + Certbot, run:"
echo "  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb"
echo "  dpkg -i /tmp/cloudflared.deb"
echo "  cloudflared tunnel login"
echo "  cloudflared tunnel create qide-dam"
echo "  cloudflared tunnel route dns qide-dam dam-api.qide.com"
echo "  cloudflared service install <token>"

echo -e "${YELLOW}==> Next steps:${NC}"
cat <<'EOF'

  1. Clone the repo:
       cd /opt/qide-dam
       git clone https://github.com/<you>/qide-dam-v2.git .

  2. Create .env.production (copy .env.example, fill secrets):
       cp .env.example .env.production
       # Generate SECRET_KEY:    openssl rand -hex 32
       # Generate POSTGRES_PASSWORD: openssl rand -base64 24
       # Cloudflare R2 settings:
       #   S3_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
       #   S3_BUCKET=qidedam-prod
       #   S3_ACCESS_KEY=<R2 token's Access Key>
       #   S3_SECRET_KEY=<R2 token's Secret>
       #   S3_PUBLIC_BASE_URL=https://cdn.qide.com    (optional CDN)
       #   S3_REGION=auto
       #   S3_USE_SSL=true
       nano .env.production

  3. Boot the stack:
       docker compose -f docker-compose.prod.yml up -d --build

  4. Verify migrations ran + DB is reachable:
       docker compose -f docker-compose.prod.yml exec api python -m scripts.init_db \
           --email admin@qide.com --password 'CHANGE_ME' --tenant-slug qide

  5. Reverse-proxy with Nginx (or Cloudflare Tunnel):
       # Nginx route: dam-api.qide.com → http://127.0.0.1:8000
       certbot --nginx -d dam-api.qide.com

  6. Smoke test:
       BASE=https://dam-api.qide.com EMAIL=admin@qide.com PASSWORD='CHANGE_ME' \
           bash scripts/smoke_test.sh
EOF
