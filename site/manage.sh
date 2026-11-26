#!/usr/bin/env bash
# Управление сайтом из терминала (Ubuntu/Debian). Запускать из папки site:  sudo bash manage.sh <команда>
#
#   install          установка: Docker, настройки, запуск, пароль админа, фаервол
#   admin-password   сменить пароль администратора
#   admin-url        показать адрес админки
#   duckdns          подключить бесплатный адрес имя.duckdns.org
#   telegram         подключить Telegram-бота: только через него пополняется баланс
#   mail             настроить почту (код подтверждения при регистрации, сброс пароля)
#   mail-test АДРЕС  отправить пробное письмо
#   add-file ФАЙЛ    добавить exe/apk в раздачу для покупателей «Навсегда»
#   github-game РЕПО [ЦЕНА] [НАЗВАНИЕ]  добавить игру из GitHub (описание, скриншоты, файл релиза)
#   files            список файлов в раздаче
#   stats            сколько пользователей и оплат
#   config           заново пройти настройку (домен, цены, бот, почта) и перезапустить
#   restart | stop | start | status | logs
#   update           скачать новую версию из GitHub и перезапустить
#   backup           сохранить копию базы в ./backups
#   bot-logs         журнал Telegram-бота
set -euo pipefail
cd "$(dirname "$0")"

GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; BOLD=$'\e[1m'; NC=$'\e[0m'
say()  { echo "${GREEN}==>${NC} $*"; }
warn() { echo "${YELLOW}!${NC} $*"; }
die()  { echo "${RED}Ошибка:${NC} $*" >&2; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "запустите через sudo: sudo bash manage.sh $*"; }
dc() { docker compose "$@"; }

env_get() { local v; v=$(grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true); v="${v#\'}"; echo "${v%\'}"; }
env_set() {   # env_set KEY VALUE — пишет KEY='VALUE' (в кавычках docker не подставляет $ и пробелы)
  local key="$1" val="${2//\'/}" tmp
  val="${val//$'\n'/}"
  tmp=$(mktemp)
  if grep -qE "^${key}=" .env; then
    K="$key" V="'$val'" awk 'BEGIN{FS=OFS="="} $1==ENVIRON["K"] {print ENVIRON["K"]"="ENVIRON["V"]; next} {print}' .env > "$tmp"
  else
    cat .env > "$tmp"; echo "${key}='${val}'" >> "$tmp"
  fi
  cat "$tmp" > .env; rm -f "$tmp"
}
rand() { head -c 64 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c "$1"; }
ask_secret() {   # ask_secret KEY "Вопрос" — ввод не отображается; Enter оставляет сохранённое значение
  local key="$1" q="$2" cur; cur="$(env_get "$key")"
  read -r -s -p "$q${cur:+ [сохранён, Enter — оставить]}: " REPLY || true; echo
  [ -n "$REPLY" ] && env_set "$key" "$REPLY"
  return 0
}
ask() {   # ask "Вопрос" "по умолчанию" -> REPLY
  local q="$1" def="${2:-}"
  if [ -n "$def" ]; then read -r -p "$q [$def]: " REPLY || true; REPLY="${REPLY:-$def}"
  else read -r -p "$q: " REPLY || true; fi
}

install_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    say "Устанавливаю Docker"
    apt-get update -y
    apt-get install -y ca-certificates curl
    # на старых Ubuntu (20.04) скрипт падает на пакете docker-model-plugin — ставим без него
    curl -fsSL https://get.docker.com | sh || apt-get install -y \
      docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-plugin
}

firewall() {
  if ! command -v ufw >/dev/null 2>&1; then apt-get install -y ufw >/dev/null; fi
  local ssh_port
  ssh_port=$(ss -tlnp 2>/dev/null | awk '/sshd/ {split($4,a,":"); print a[length(a)]; exit}')
  ssh_port="${ssh_port:-22}"
  ufw allow "${ssh_port}/tcp" >/dev/null
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
  ufw --force enable >/dev/null
  say "Фаервол включён: открыты SSH (${ssh_port}), 80 и 443"
}

auto_updates() {
  apt-get install -y unattended-upgrades >/dev/null 2>&1 || true
  dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true
  say "Включены автоматические обновления безопасности Ubuntu"
}

configure() {
  [ -f .env ] || cp env.example .env
  chmod 600 .env
  [ -n "$(env_get SECRET_KEY)" ] || env_set SECRET_KEY "$(rand 64)"
  [ -n "$(env_get ADMIN_PATH)" ] || env_set ADMIN_PATH "admin-$(rand 20)"

  echo; echo "${BOLD}Настройка сайта${NC} (Enter — оставить как есть)"
  local dom; dom="$(env_get DOMAIN)"; case "$dom" in example.ru|:80) dom="" ;; esac
  ask "Домен сайта (например games.ru; пусто — работать по IP сервера)" "$dom"
  [ -z "$REPLY" ] && REPLY=":80"
  if [[ "$REPLY" == *.duckdns.org ]] && [ -n "$(env_get DUCKDNS_TOKEN)" ]; then
    env_set DOMAIN "$REPLY"; env_set BASE_URL "https://$REPLY"
  elif [ "$REPLY" = ":80" ]; then
    local ip; ip=$(curl -fsS4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
    env_set DOMAIN ":80"; env_set BASE_URL "http://${ip}"
    warn "Сайт будет работать по адресу http://${ip} — без HTTPS (сертификат на голый IP не выдают)."
    warn "Пароли и данные идут без шифрования. Когда появится домен: sudo bash manage.sh config (или duckdns)."
  else
    REPLY="${REPLY#http://}"; REPLY="${REPLY#https://}"; REPLY="${REPLY%%/*}"
    env_set DOMAIN "$REPLY"; env_set BASE_URL "https://$REPLY"
  fi

  ask "Почта поддержки (видна на сайте, можно пусто)" "$(env_get SUPPORT_EMAIL)"; env_set SUPPORT_EMAIL "$REPLY"
  ask "Цена «Навсегда», ₽" "$(env_get PRICE_FOREVER)"; env_set PRICE_FOREVER "${REPLY//[^0-9]/}"
  ask "Цена «Месяц онлайн», ₽ (0 — убрать тариф)" "$(env_get PRICE_MONTH)"; env_set PRICE_MONTH "${REPLY//[^0-9]/}"

  telegram_questions
  mail_questions
  chmod 600 .env
}

mail_questions() {
  echo; echo "${BOLD}Почта сайта${NC} — с неё приходят код подтверждения при регистрации и ссылка смены пароля."
  echo "  Нужен «пароль приложения» (не обычный пароль от ящика):"
  echo "   Яндекс: id.yandex.ru → Безопасность → Пароли приложений → Почта"
  echo "   Mail.ru: Настройки → Безопасность → Пароли для внешних приложений"
  echo "   Gmail:   myaccount.google.com → Безопасность → Пароли приложений (нужна двухэтапная проверка)"
  echo "  1) Яндекс  2) Mail.ru  3) Gmail  4) другой сервер  Enter — пропустить (регистрация без кода)"
  read -r -p "Выбор: " REPLY || true
  case "$REPLY" in
    1) env_set SMTP_HOST smtp.yandex.ru; env_set SMTP_PORT 465 ;;
    2) env_set SMTP_HOST smtp.mail.ru;   env_set SMTP_PORT 465 ;;
    3) env_set SMTP_HOST smtp.gmail.com; env_set SMTP_PORT 465 ;;
    4) ask "SMTP-сервер" "$(env_get SMTP_HOST)"; env_set SMTP_HOST "$REPLY"
       ask "Порт (465 — SSL, 587 — STARTTLS)" "$(env_get SMTP_PORT)"; env_set SMTP_PORT "${REPLY//[^0-9]/}" ;;
    *) return 0 ;;
  esac
  ask "Адрес ящика (логин)" "$(env_get SMTP_USER)"; env_set SMTP_USER "$REPLY"; env_set SMTP_FROM "$REPLY"
  ask_secret SMTP_PASSWORD "Пароль приложения"
  env_set EMAIL_VERIFY 1
  return 0
}

telegram_questions() {
  echo; echo "${BOLD}Telegram-бот${NC} — оплата звёздами Telegram: покупатель покупает звёзды в Telegram за рубли и платит ими в боте."
  echo "  1) Напишите @BotFather → /newbot → придумайте имя → он пришлёт токен вида 123456:ABC..."
  echo "  2) Свой Telegram ID (для уведомлений об оплатах и возвратов) покажет бот @userinfobot."
  ask_secret TG_BOT_TOKEN "Токен бота (пусто — без бота)"
  if [ -n "$(env_get TG_BOT_TOKEN)" ]; then
    ask "Сколько рублей засчитывать за 1 звезду" "$(env_get TG_STAR_RUB || true)"
    [ -z "$REPLY" ] && REPLY="1.5"
    env_set TG_STAR_RUB "$(echo "${REPLY//,/.}" | tr -cd '0-9.')"
    ask "Ваш Telegram ID (пусто — без уведомлений)" "$(env_get TG_ADMIN_ID || true)"; env_set TG_ADMIN_ID "${REPLY//[^0-9]/}"
  fi
  return 0
}

admin_password() {
  echo; echo "${BOLD}Пароль администратора${NC} (от 12 символов, только для вас):"
  dc exec -T web true 2>/dev/null || dc up -d web
  local p1 p2
  read -r -s -p "Пароль: " p1; echo
  read -r -s -p "Ещё раз: " p2; echo
  [ "$p1" = "$p2" ] || die "пароли не совпадают"
  printf '%s\n' "$p1" | dc exec -T web python app.py admin-password
}

# ---------- бесплатный домен DuckDNS (имя.duckdns.org) ----------
duckdns_update() {   # обновить IP у DuckDNS; печатает OK/KO
  local name token
  name="$(env_get DUCKDNS_DOMAIN)"; token="$(env_get DUCKDNS_TOKEN)"
  [ -n "$name" ] && [ -n "$token" ] || return 1
  # пустой ip= — DuckDNS сам возьмёт внешний IP сервера; запрос идёт по HTTPS
  curl -fsS --max-time 20 --proto '=https' "https://www.duckdns.org/update?domains=${name}&token=${token}&ip=" 2>/dev/null
}

duckdns_setup() {
  echo; echo "${BOLD}Бесплатный адрес DuckDNS${NC}"
  echo "  1) Откройте https://www.duckdns.org и войдите через Google или GitHub (кнопка вверху)."
  echo "  2) В поле «sub domain» придумайте имя (латиница, цифры, дефис) и нажмите «add domain»."
  echo "  3) Скопируйте «token» — он показан вверху страницы."
  local name
  ask "Имя, которое вы добавили (без .duckdns.org; пусто — пропустить)" "$(env_get DUCKDNS_DOMAIN)"
  name="$(echo "${REPLY%.duckdns.org}" | tr 'A-Z' 'a-z')"
  [ -n "$name" ] || return 1
  [[ "$name" =~ ^[a-z0-9-]{1,63}$ ]] || { warn "Имя может содержать только латиницу, цифры и дефис"; return 1; }
  env_set DUCKDNS_DOMAIN "$name"
  ask_secret DUCKDNS_TOKEN "Токен DuckDNS"
  [ -n "$(env_get DUCKDNS_TOKEN)" ] || { warn "Токен не введён"; return 1; }
  local res; res="$(duckdns_update || true)"
  if [ "$res" != "OK" ]; then
    warn "DuckDNS не принял данные (ответ: ${res:-нет связи}). Проверьте имя и токен: sudo bash manage.sh duckdns"
    return 1
  fi
  # обновлять IP каждые 5 минут — на случай, если у сервера сменится адрес
  local self; self="$(cd "$(dirname "$0")" && pwd)/manage.sh"
  printf '%s\n' "*/5 * * * * root bash '$self' duckdns-update >/dev/null 2>&1" > /etc/cron.d/kolca-duckdns
  chmod 644 /etc/cron.d/kolca-duckdns
  say "Адрес ${name}.duckdns.org теперь ведёт на этот сервер (IP обновляется автоматически)"
  REPLY="${name}.duckdns.org"
  return 0
}

wait_dns() {   # подождать, пока домен начнёт указывать на сервер (до 2 минут)
  local dom="$1" ip my
  my=$(curl -fsS4 --max-time 10 https://api.ipify.org 2>/dev/null || true)
  for _ in $(seq 1 24); do
    ip=$(getent ahostsv4 "$dom" 2>/dev/null | awk 'NR==1{print $1}')
    if [ -n "$ip" ] && { [ -z "$my" ] || [ "$ip" = "$my" ]; }; then return 0; fi
    sleep 5
  done
  warn "Домен $dom пока не указывает на этот сервер — HTTPS-сертификат выпустится позже сам."
}

yoomoney_auth() {
  [ -n "$(env_get YOOMONEY_WALLET)" ] && [ -n "$(env_get YOOMONEY_CLIENT_ID)" ] || { warn "Сначала укажите кошелёк и client_id: sudo bash manage.sh config"; return 1; }
  dc exec -T web true 2>/dev/null || dc up -d web
  echo; echo "${BOLD}Подключение API ЮMoney${NC}"
  dc exec web python app.py yoomoney-auth
}

admin_url() {
  local base path; base="$(env_get BASE_URL)"; path="$(env_get ADMIN_PATH)"
  echo "Админка: ${BOLD}${base}/${path}/${NC}"
  echo "Никому не сообщайте этот адрес — без него страницу входа не найти."
}

cmd="${1:-help}"
case "$cmd" in
  install)
    need_root install
    install_docker
    configure
    firewall
    auto_updates
    dom="$(env_get DOMAIN)"; [ "$dom" != ":80" ] && wait_dns "$dom"
    say "Собираю и запускаю сайт (первый раз — пару минут)"
    dc up -d --build
    sleep 3
    admin_password
    [ -n "$(env_get TG_BOT_TOKEN)" ] || warn "Без Telegram-бота пополнить баланс нельзя: sudo bash manage.sh telegram"
    echo; say "Готово!"
    echo "Сайт:    ${BOLD}$(env_get BASE_URL)${NC}"
    admin_url
    echo "Файлы exe/apk для покупателей:  sudo bash manage.sh add-file /путь/Kolca.exe"
    ;;
  admin-password) need_root "$cmd"; admin_password ;;
  admin-url)      need_root "$cmd"; admin_url ;;
  yoomoney-auth)  need_root "$cmd"; yoomoney_auth ;;
  duckdns)
    need_root "$cmd"
    if duckdns_setup; then
      env_set DOMAIN "$REPLY"; env_set BASE_URL "https://$REPLY"
      wait_dns "$REPLY"
      dc up -d --force-recreate
      say "Сайт: https://$REPLY"
    fi
    ;;
  duckdns-update) duckdns_update >/dev/null || true ;;
  yoomoney-check) need_root "$cmd"; dc exec -T web python app.py yoomoney-check ;;
  telegram)
    need_root "$cmd"
    [ -f .env ] || die "сначала установите сайт: sudo bash manage.sh install"
    telegram_questions
    chmod 600 .env
    dc up -d --build --force-recreate
    sleep 5
    dc logs --tail=5 bot
    say "Готово. Откройте бота в Telegram и нажмите «Старт»; на сайте в «Аккаунте» появится «Привязать Telegram»."
    ;;
  bot-logs) need_root "$cmd"; dc logs --tail=200 -f bot ;;
  mail)
    need_root "$cmd"
    [ -f .env ] || die "сначала установите сайт: sudo bash manage.sh install"
    mail_questions; chmod 600 .env
    dc up -d --force-recreate
    sleep 3
    ask "Куда отправить пробное письмо (Enter — пропустить)"
    [ -n "$REPLY" ] && dc exec -T web python app.py mail-test "$REPLY"
    say "Готово. Теперь при регистрации на почту приходит код подтверждения."
    ;;
  mail-test) need_root "$cmd"; [ -n "${2:-}" ] || die "укажите адрес: sudo bash manage.sh mail-test you@mail.ru"
    dc exec -T web python app.py mail-test "$2" ;;
  add-file)
    need_root "$cmd"; f="${2:-}"; [ -f "$f" ] || die "укажите путь к файлу: sudo bash manage.sh add-file Kolca.exe"
    name="$(basename "$f")"
    [[ "$name" =~ ^[A-Za-z0-9._-]+\.(exe|apk|zip)$ ]] || die "имя файла — только латиница, цифры, . _ - и расширение .exe/.apk/.zip"
    dc exec -T web sh -c 'mkdir -p /data/downloads && cat > "/data/downloads/$1"' sh "$name" < "$f"
    say "Добавлен файл для скачивания: $name"
    ;;
  github-game)
    need_root "$cmd"; [ -n "${2:-}" ] || die "пример: sudo bash manage.sh github-game Anonymouslol411/otklik"
    dc exec -T web python app.py github-game "${@:2}" ;;
  files)   need_root "$cmd"; dc exec -T web ls -lh /data/downloads 2>/dev/null || echo "Файлов пока нет" ;;
  stats)   need_root "$cmd"; dc exec -T web python app.py stats ;;
  config)  need_root "$cmd"; configure; dc up -d --force-recreate; say "Настройки применены" ;;
  restart) need_root "$cmd"; dc up -d --force-recreate; say "Перезапущено" ;;
  start)   need_root "$cmd"; dc up -d ;;
  stop)    need_root "$cmd"; dc down ;;
  status)  need_root "$cmd"; dc ps ;;
  logs)    need_root "$cmd"; dc logs --tail=200 -f ;;
  update)
    need_root "$cmd"
    git -C .. pull --ff-only
    dc up -d --build
    say "Обновлено"
    ;;
  backup)
    need_root "$cmd"; mkdir -p backups
    dc exec -T web python -c "import sqlite3; s=sqlite3.connect('/data/site.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
    f="backups/site-$(date +%F-%H%M).db"
    dc exec -T web sh -c 'cat /data/backup.db && rm -f /data/backup.db' > "$f"; chmod 600 "$f"
    say "Копия базы: $f"
    ;;
  *)
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
