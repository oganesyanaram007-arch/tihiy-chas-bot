#!/usr/bin/env bash
# Разворачивает «Тихий Час» на свежем VPS (Ubuntu 22.04) с нуля до
# работающих сервисов: бот + API за nginx с HTTPS.
#
# Запуск на сервере (после того как код уже загружен в /root/tihiy-bot):
#   cd /root/tihiy-bot && bash deploy/setup.sh api.ваш-домен.ru
#
# Проще всего — попросить Claude Code на сервере выполнить это, он сам
# разберётся с недостающими шагами и подскажет, если что-то пойдёт не так.
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "Использование: bash deploy/setup.sh api.ваш-домен.ru"
  exit 1
fi

echo "== 1. Системные пакеты =="
apt update
apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx

echo "== 2. Виртуальное окружение и зависимости =="
cd /root/tihiy-bot
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "== 3. .env =="
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Файл .env создан из примера — впишите BOT_TOKEN и ADMIN_IDS вручную:"
  echo "  nano /root/tihiy-bot/.env"
fi

echo "== 4. systemd-юниты =="
sed "s/DOMAIN_PLACEHOLDER/${DOMAIN}/" deploy/nginx.conf > /tmp/tihiyapi.nginx
cp /tmp/tihiyapi.nginx /etc/nginx/sites-available/tihiyapi
ln -sf /etc/nginx/sites-available/tihiyapi /etc/nginx/sites-enabled/tihiyapi
cp deploy/tihiybot.service /etc/systemd/system/
cp deploy/tihiyapi.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tihiybot
systemctl enable --now tihiyapi

echo "== 5. nginx + HTTPS =="
nginx -t
systemctl reload nginx
echo "Выпускаем сертификат для ${DOMAIN} — домен уже должен указывать на этот сервер"
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m admin@"${DOMAIN}" || \
  echo "certbot не смог выпустить сертификат — проверьте, что DNS A-запись ${DOMAIN} уже указывает на этот IP, и запустите вручную: certbot --nginx -d ${DOMAIN}"

echo ""
echo "== Готово =="
echo "Бот:    systemctl status tihiybot"
echo "API:    systemctl status tihiyapi   ·   curl https://${DOMAIN}/api/health"
echo "Логи:   journalctl -u tihiybot -f   /   journalctl -u tihiyapi -f"
