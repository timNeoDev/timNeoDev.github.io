#!/usr/bin/env bash
# Установка «Радара заказов» на Linux-сервер (Ubuntu/Debian) в отдельную папку.
# Ничего, кроме /opt/order-radar, пользователя «radar» и службы «order-radar», не создаёт и не меняет:
# другие боты, их папки, службы и настройки файрвола не затрагиваются.
#
# Запуск на сервере:
#   curl -fsSL https://raw.githubusercontent.com/timNeoDev/timNeoDev.github.io/main/projects/order-radar/deploy/install.sh -o install.sh
#   sudo bash install.sh
# Повторный запуск обновляет код и перезапускает радар; .env и база данных сохраняются.
set -euo pipefail

APP_DIR=/opt/order-radar
REPO=https://github.com/timNeoDev/timNeoDev.github.io.git
SERVICE=order-radar
APP_USER=radar

[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo: sudo bash install.sh"; exit 1; }
command -v python3 >/dev/null || { echo "Не найден python3 — установите: apt install python3"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Нужен Python 3.10 или новее, сейчас: $(python3 --version)"; exit 1; }
python3 -m venv -h >/dev/null 2>&1 || apt-get install -y python3-venv
command -v git >/dev/null || apt-get install -y git

if [ -f "/etc/systemd/system/$SERVICE.service" ] && [ ! -d "$APP_DIR" ]; then
  echo "Служба $SERVICE уже существует и не похожа на радар — остановлено, чтобы ничего не сломать."; exit 1
fi

id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

# Код: только папка радара из репозитория
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull -q
else
  git clone -q --depth 1 --filter=blob:none --sparse "$REPO" "$APP_DIR"
  git -C "$APP_DIR" sparse-checkout set projects/order-radar
fi
cd "$APP_DIR/projects/order-radar"

python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  PORT=8080
  while ss -ltn | awk '{print $4}' | grep -qE "[:.]$PORT\$"; do PORT=$((PORT + 1)); done  # занятые ботами порты пропускаем
  echo "Настройка радара (ответы сохранятся в $PWD/.env):"
  read -rp "  Токен бота радара от @BotFather (Enter — без бота): " BOT_TOKEN
  read -rp "  Ваш Telegram ID (Enter — без бота): " ADMIN_ID
  while :; do
    read -rsp "  Пароль для входа в админку: " ADMIN_PASSWORD; echo
    [ -n "$ADMIN_PASSWORD" ] && break
  done
  umask 077
  printf 'BOT_TOKEN=%s\nADMIN_ID=%s\nADMIN_PASSWORD=%s\nWEB_HOST=0.0.0.0\nWEB_PORT=%s\n' \
    "$BOT_TOKEN" "${ADMIN_ID:-0}" "$ADMIN_PASSWORD" "$PORT" > .env
fi
PORT=$(grep '^WEB_PORT=' .env | cut -d= -f2)
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Order Radar (радар заказов)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR/projects/order-radar
ExecStart=$APP_DIR/projects/order-radar/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=5 status "$SERVICE" || true

echo
echo "Готово. Админка: http://$(hostname -I | awk '{print $1}'):$PORT"
echo "Логи: journalctl -u $SERVICE -f    Остановить: systemctl stop $SERVICE"
