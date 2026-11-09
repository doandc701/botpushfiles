#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu VPS: swap (e2-micro has only 1 GB RAM), Docker, then start the bot.
set -euo pipefail
cd "$(dirname "$0")"

if ! swapon --show | grep -q .; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi

sudo docker compose up -d --build
sudo docker compose ps
echo "Xong. Xem log: sudo docker compose logs -f bot"
