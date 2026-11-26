"""Сайт игр «Кольца» и «Орбиты».

Тарифы (разовые покупки, без автосписаний):
  * пробный период — 3 дня игры онлайн сразу после регистрации, без карты;
  * «Месяц онлайн»  — PRICE_MONTH ₽ (329), 30 дней игры в браузере;
  * «Навсегда»      — PRICE_FOREVER ₽ (5000), игра онлайн без срока + скачивание exe/apk.

Оплата: ЮMoney (кошелёк физлица, форма Quickpay; факт оплаты сервер проверяет сам через API ЮMoney),
перевод на карту (подтверждает администратор), баланс аккаунта.

Админка: один администратор, вход по паролю на секретном адресе /<ADMIN_PATH>.

Команды:  gunicorn app:app                 — сайт
          python app.py admin-password     — задать пароль администратора
          python app.py stats              — сводка
          python app.py add-file ПУТЬ      — положить exe/apk в раздачу
          python app.py worker             — фоновая проверка оплат ЮMoney через API
          python app.py yoomoney-auth      — подключить API ЮMoney (получить токен)
          python app.py yoomoney-check     — проверить связь с ЮMoney и неоплаченные заказы
"""
import getpass
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import sys
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (Blueprint, Flask, abort, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------- настройки
HERE = Path(__file__).resolve().parent
env = os.environ.get

SECRET_KEY = env('SECRET_KEY') or ''
BASE_URL = (env('BASE_URL') or 'http://localhost:8000').rstrip('/')
DATA_DIR = Path(env('DATA_DIR') or HERE / 'data')
DOWNLOADS_DIR = DATA_DIR / 'downloads'
COVERS_DIR = DATA_DIR / 'covers'
COVER_EXT = {b'\x89PNG': '.png', b'\xff\xd8\xff': '.jpg', b'RIFF': '.webp'}
COVER_LIMIT = 5 * 1024 * 1024
GAMES_DIR = Path(env('GAMES_DIR') or HERE.parent / 'web')
SITE_NAME = env('SITE_NAME') or 'Kolca & Orbity'
SUPPORT_EMAIL = env('SUPPORT_EMAIL') or ''
TZ = ZoneInfo(env('TIMEZONE') or 'Europe/Moscow')
TRIAL_DAYS = int(env('TRIAL_DAYS') or 3)
ADMIN_PATH = (env('ADMIN_PATH') or '').strip('/')

PRODUCTS = {
    'month': {'price': int(env('PRICE_MONTH') or 329), 'days': 30},
    'forever': {'price': int(env('PRICE_FOREVER') or 5000), 'days': None},
}
PRODUCTS = {k: v for k, v in PRODUCTS.items() if v['price'] > 0}

YOOMONEY_WALLET = re.sub(r'\D', '', env('YOOMONEY_WALLET') or '')
YOOMONEY_SECRET = env('YOOMONEY_SECRET') or ''            # секрет HTTP-уведомлений (необязательно, ускоряет проверку)
YOOMONEY_CLIENT_ID = env('YOOMONEY_CLIENT_ID') or ''      # приложение ЮMoney для доступа к истории операций
YOOMONEY_CLIENT_SECRET = env('YOOMONEY_CLIENT_SECRET') or ''
YOOMONEY_REDIRECT_URI = env('YOOMONEY_REDIRECT_URI') or ''   # по умолчанию BASE_URL/yoomoney/oauth
YOOMONEY_API = env('YOOMONEY_API_BASE') or 'https://yoomoney.ru'
YOOMONEY_MIN_SHARE = float(env('YOOMONEY_MIN_SHARE') or 0.95)   # какая доля цены должна дойти до кошелька (комиссия ЮMoney)
CARD_NUMBER = re.sub(r'\D', '', env('CARD_NUMBER') or '')
CARD_HOLDER = env('CARD_HOLDER') or ''
CARD_BANK = env('CARD_BANK') or ''

# Telegram-бот: оплата звёздами Telegram (покупатель платит рублями, покупая звёзды в Telegram)
TG_BOT_TOKEN = env('TG_BOT_TOKEN') or ''
TG_STAR_RUB = float(env('TG_STAR_RUB') or 1.5)        # сколько рублей засчитывается за 1 звезду
TG_ADMIN_ID = int(re.sub(r'\D', '', env('TG_ADMIN_ID') or '') or 0)   # ваш Telegram ID — для уведомлений об оплатах
TG_TOPUP_PACKS = [int(x) for x in re.findall(r'\d+', env('TG_TOPUP_PACKS') or '100,250,500,1000') if 0 < int(x) <= 100000]

SMTP_HOST = env('SMTP_HOST') or ''
SMTP_PORT = int(env('SMTP_PORT') or 587)
SMTP_USER = env('SMTP_USER') or ''
SMTP_PASSWORD = env('SMTP_PASSWORD') or ''
SMTP_FROM = env('SMTP_FROM') or SMTP_USER
# Подтверждение почты кодом при регистрации: включено, если настроена почта (SMTP); EMAIL_VERIFY=0 — выключить
EMAIL_VERIFY = bool(SMTP_HOST) and (env('EMAIL_VERIFY') or '1') != '0'

GAMES = ('kolca', 'orbity')
DOWNLOAD_EXT = {'.exe', '.apk', '.zip'}
SMALL_REQUEST = 64 * 1024            # обычные формы
UPLOAD_LIMIT = 300 * 1024 * 1024     # загрузка файлов в админке

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('site')

I18N = json.loads((HERE / 'i18n.json').read_text(encoding='utf-8'))
LANGS = list(I18N['_languages'].keys())


def yoomoney_token(con=None):
    con = con or db()
    return setting(con, 'yoomoney_token') or ''


def stars_price(rub):
    """Цена в звёздах Telegram для суммы в рублях (округление вверх)."""
    return max(1, -int(-rub // TG_STAR_RUB))


def tg_bot_username(con=None):
    return setting(con or db(), 'tg_bot_username') or '' if TG_BOT_TOKEN else ''


def methods_available():
    """Внешних способов оплаты на сайте нет: покупки оплачиваются балансом,
    а баланс пополняется звёздами в Telegram-боте. Код ЮMoney и перевода на карту оставлен только
    для заказов, созданных раньше (их ещё можно подтвердить в админке)."""
    return []


# ---------------------------------------------------------------- база данных
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    lang TEXT,
    created_at INTEGER NOT NULL,
    trial_end INTEGER,
    paid_until INTEGER,
    lifetime INTEGER NOT NULL DEFAULT 0,
    balance REAL NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    ban_reason TEXT,
    session_ver INTEGER NOT NULL DEFAULT 0,
    reset_token TEXT,
    reset_expires INTEGER
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    user_id INTEGER,
    product TEXT NOT NULL,
    amount REAL NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    payer_note TEXT,
    operation_id TEXT UNIQUE,
    created_at INTEGER NOT NULL,
    paid_at INTEGER
);
CREATE TABLE IF NOT EXISTS balance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, delta REAL NOT NULL, reason TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS admin_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, target_email TEXT, details TEXT, ip TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS attempts (key TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_key ON attempts(key, ts);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'soon',
    url TEXT, cover TEXT, hue INTEGER NOT NULL DEFAULT 200, sort INTEGER NOT NULL DEFAULT 100, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_games (
    user_id INTEGER NOT NULL, game_id INTEGER NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (user_id, game_id));
CREATE TABLE IF NOT EXISTS pending_regs (
    email TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, lang TEXT, code_hash TEXT NOT NULL,
    expires INTEGER NOT NULL, tries INTEGER NOT NULL DEFAULT 0, sent_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tg_links (
    code TEXT PRIMARY KEY, user_id INTEGER NOT NULL, product TEXT, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tg_payments (
    charge_id TEXT PRIMARY KEY, user_id INTEGER, tg_id INTEGER, stars INTEGER NOT NULL, kind TEXT NOT NULL,
    rub REAL, order_id TEXT, applied INTEGER NOT NULL DEFAULT 0, refunded INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
"""


def db_connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA_DIR / 'site.db', timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute('PRAGMA table_info(users)')}
    if 'tg_id' not in cols:     # обновление старой базы
        try:
            con.execute('ALTER TABLE users ADD COLUMN tg_id INTEGER')
        except sqlite3.OperationalError:
            pass                # другой процесс уже добавил
    con.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_tg ON users(tg_id) WHERE tg_id IS NOT NULL')
    gcols = {r[1] for r in con.execute('PRAGMA table_info(games)')}
    for col, ddl in (('price', 'REAL'), ('file', 'TEXT'), ('shots', 'TEXT')):
        if col not in gcols:
            try:
                con.execute(f'ALTER TABLE games ADD COLUMN {col} {ddl}')
            except sqlite3.OperationalError:
                pass
    con.commit()
    return con


def db():
    if 'db' not in g:
        g.db = db_connect()
    return g.db


def now():
    return int(time.time())


def setting(con, key, default=None):
    row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(con, key, value):
    con.execute('INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
    con.commit()


# --- доступ
# Все игры сайта бесплатны: играть и скачивать может любой посетитель, даже без аккаунта.
# Заблокированный администратором аккаунт по-прежнему ничего не получает.
def can_play(u):
    return not (u and u['banned'])


def can_download(u):
    return not (u and u['banned'])


def sale_game(con, gid):
    """Игра из каталога, которая продаётся на сайте (статус «Продаётся» и цена больше нуля)."""
    return con.execute("SELECT * FROM games WHERE id=? AND status='paid' AND price>0", (gid,)).fetchone()


def product_price(con, product):
    """Цена товара: 'month', 'forever' или 'game:<id>'. None — такого товара нет."""
    if product in PRODUCTS:
        return PRODUCTS[product]['price']
    m = re.fullmatch(r'game:(\d{1,9})', product or '')
    row = sale_game(con, int(m.group(1))) if m else None
    return float(row['price']) if row else None


def owns_game(con, user, gid):
    """Игры каталога бесплатны для всех."""
    return not (user and user['banned'])


def owned_games(con, user):
    if not user:
        return []
    if user['lifetime'] and not user['banned']:     # «Навсегда» — все продаваемые игры каталога
        return con.execute("SELECT * FROM games WHERE status='paid' ORDER BY sort, id").fetchall()
    return con.execute('SELECT g.* FROM games g JOIN user_games ug ON ug.game_id=g.id WHERE ug.user_id=? ORDER BY g.sort, g.id',
                       (user['id'],)).fetchall()


def product_title(product):
    """Название товара для страниц и админки."""
    if product and product.startswith('game:'):
        row = db().execute('SELECT title FROM games WHERE id=?', (int(product[5:]) if product[5:].isdigit() else 0,)).fetchone()
        return row['title'] if row else t('game_deleted')
    return t('plan_' + (product or ''))


def grant(con, user_id, product=None, days=None):
    """Выдать доступ: товар ('month' / 'forever') или N дней."""
    u = con.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if product and product.startswith('game:'):      # разовая покупка игры из каталога
        con.execute('INSERT OR IGNORE INTO user_games (user_id, game_id, created_at) VALUES (?,?,?)',
                    (user_id, int(product[5:]), now()))
    elif product == 'forever':
        con.execute('UPDATE users SET lifetime=1 WHERE id=?', (user_id,))
    else:
        days = days if days is not None else PRODUCTS['month']['days']
        start = max(now(), u['paid_until'] or 0)
        con.execute('UPDATE users SET paid_until=? WHERE id=?', (start + days * 86400, user_id))
    con.commit()


def change_balance(con, user_id, delta, reason):
    con.execute('UPDATE users SET balance=ROUND(balance+?, 2) WHERE id=?', (delta, user_id))
    con.execute('INSERT INTO balance_log (user_id, delta, reason, created_at) VALUES (?,?,?,?)', (user_id, delta, reason, now()))
    con.commit()


# --- ограничение числа попыток (общее для всех процессов сайта)
def too_many(con, key, limit, window):
    con.execute('DELETE FROM attempts WHERE ts<?', (now() - 86400,))
    n = con.execute('SELECT COUNT(*) FROM attempts WHERE key=? AND ts>?', (key, now() - window)).fetchone()[0]
    return n >= limit


def note_attempt(con, key):
    con.execute('INSERT INTO attempts (key, ts) VALUES (?,?)', (key, now()))
    con.commit()


def fmt_rub(v):
    v = float(v or 0)
    text = f'{v:,.2f}'.replace(',', ' ').replace('.', ',')
    return (text[:-3] if text.endswith(',00') else text) + ' ₽'


def fmt_date(ts, with_time=False):
    if not ts:
        return '—'
    return datetime.fromtimestamp(ts, TZ).strftime('%d.%m.%Y %H:%M' if with_time else '%d.%m.%Y')


def fmt_card(num):
    return ' '.join(num[i:i + 4] for i in range(0, len(num), 4))


# ---------------------------------------------------------------- приложение
app = Flask(__name__, template_folder=str(HERE / 'templates'), static_folder=str(HERE / 'static'))
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=SECRET_KEY or secrets.token_hex(32),
    SESSION_COOKIE_NAME='s',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=BASE_URL.startswith('https://'),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    MAX_CONTENT_LENGTH=UPLOAD_LIMIT,
)
if not SECRET_KEY:
    log.warning('SECRET_KEY не задан — сессии сбросятся при перезапуске. Запустите manage.sh install.')


@app.teardown_appcontext
def close_db(exc):
    con = g.pop('db', None)
    if con is not None:
        con.close()


def pick_lang():
    lang = request.args.get('lang')
    if lang in LANGS:
        session['lang'] = lang
        return lang
    return session.get('lang') if session.get('lang') in LANGS else 'ru'


# Профили (аккаунты посетителей) убраны с сайта. Код и база оставлены — вернуть можно настройкой PROFILES_ENABLED=1.
PROFILES_ENABLED = (env('PROFILES_ENABLED') or '0') == '1'
PROFILE_ENDPOINTS = {'register', 'verify', 'login', 'logout', 'forgot', 'reset', 'account', 'change_password',
                     'account_telegram', 'buy', 'pay', 'pay_sent', 'pay_check'}


@app.before_request
def setup_request():
    g.lang = pick_lang()
    g.user = None
    # большие запросы разрешены только для загрузки файлов в админке
    if (request.content_length or 0) > SMALL_REQUEST and request.endpoint not in ('admin.downloads', 'admin.games'):
        abort(413)
    if not PROFILES_ENABLED:
        # профилей на сайте нет: страницы аккаунта, входа и покупок не существуют, вход не запоминается
        if request.endpoint in PROFILE_ENDPOINTS:
            abort(404)
        for k in ('uid', 'sv', 'reg_email'):
            if k in session:
                session.pop(k)
    uid = session.get('uid') if PROFILES_ENABLED else None
    if uid:
        u = db().execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if u and not u['banned'] and session.get('sv') == u['session_ver']:
            g.user = u
        else:
            for k in ('uid', 'sv'):
                session.pop(k, None)
            if u and u['banned'] and request.endpoint not in ('login', 'static'):
                return redirect(url_for('login', banned=1))
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(24)


CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' data: blob:; "
       "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' data: blob:; "
       "font-src 'self' data:; frame-ancestors 'self'; base-uri 'self'; object-src 'none'; "
       "form-action 'self' https://yoomoney.ru https://t.me")


@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault('Content-Security-Policy', CSP)
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('X-Frame-Options', 'SAMEORIGIN')
    h.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    h.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()')
    h.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    if request.blueprint == 'admin' or request.endpoint in ('account', 'pay', 'login', 'register'):
        h['Cache-Control'] = 'no-store'
    return resp


def t(key, **kw):
    s = I18N.get(g.get('lang', 'ru'), {}).get(key) or I18N['ru'].get(key) or key
    return s.format(**kw) if kw else s


@app.context_processor
def inject():
    return dict(t=t, lang=g.get('lang', 'ru'), langs=I18N['_languages'], user=g.get('user'), csrf=session.get('csrf'),
                trial_days=TRIAL_DAYS, site_name=SITE_NAME, support_email=SUPPORT_EMAIL, games=GAMES,
                products=PRODUCTS, methods=methods_available(), fmt_rub=fmt_rub, fmt_date=fmt_date,
                can_play=can_play, can_download=can_download, now=now(),
                tg_bot=tg_bot_username(), ptitle=product_title, registration_open=REGISTRATION_OPEN)


def check_csrf():
    sent = request.form.get('csrf') or ''
    if not sent or not hmac.compare_digest(sent, session.get('csrf') or ''):
        abort(400)


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for('login', next=request.path))
        return f(*a, **kw)
    return wrapper


def client_ip():
    return request.remote_addr or '-'


def start_session(u):
    lang = g.lang
    session.clear()
    session.update(uid=u['id'], sv=u['session_ver'], lang=lang, csrf=secrets.token_urlsafe(24))
    session.permanent = True


# ---------------------------------------------------------------- страницы
@app.route('/')
def index():
    return render_template('index.html', extra=catalog(db())[:6])


def catalog(con):
    """Игры каталога, добавленные в админке (кроме «Колец» и «Орбит», которые встроены)."""
    return con.execute('SELECT * FROM games ORDER BY sort, id').fetchall()


@app.route('/games')
def games_page():
    return render_template('games.html', extra=catalog(db()))


@app.route('/games/<int:gid>')
def game_page(gid):
    con = db()
    row = con.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone()
    if not row:
        abort(404)
    shots = [n for n in json.loads(row['shots'] or '[]') if isinstance(n, str)]
    return render_template('game.html', g_=row, shots=shots,
                           owned=row['status'] == 'paid' and owns_game(con, g.user, gid))


@app.route('/covers/<name>')
def cover_file(name):
    if name != secure_filename(name) or Path(name).suffix.lower() not in set(COVER_EXT.values()):
        abort(404)
    resp = send_from_directory(COVERS_DIR, name)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/pricing')
def pricing():
    return redirect(url_for('support'), 301)      # платных тарифов больше нет


DONATE_MIN = 10
DONATE_MAX_STARS = int(env('TG_DONATE_MAX_STARS') or 10000)
DONATE_PRESETS = (100, 300, 500, 1000)


@app.route('/support')
def support():
    """Поддержать разработчика: любая сумма, оплата звёздами в Telegram-боте."""
    con = db()
    bot = tg_bot_username(con)
    max_rub = int(DONATE_MAX_STARS * TG_STAR_RUB)
    raw = (request.args.get('amount') or '').replace(' ', '').replace(',', '.')
    error = None
    if raw:
        try:
            amount = int(float(raw))
        except ValueError:
            amount = 0
        if not bot:
            error = t('support_unavailable')
        elif not DONATE_MIN <= amount <= max_rub:
            error = t('support_range', min=fmt_rub(DONATE_MIN), max=fmt_rub(max_rub))
        else:
            # Не перекидываем сразу: на компьютере без приложения Telegram ссылка tg:// никуда не ведёт.
            # Даём три пути: приложение, Telegram Web и ручной вариант (написать боту сумму).
            tg_addr = urllib.parse.quote(f'tg://resolve?domain={bot}&start=d{amount}', safe='')
            return render_template('support_go.html', bot=bot, amount=amount, stars=stars_price(amount),
                                   app_url=f'https://t.me/{bot}?start=d{amount}',
                                   web_url=f'https://web.telegram.org/k/#?tgaddr={tg_addr}')
    total = con.execute("SELECT COUNT(*), COALESCE(SUM(rub),0) FROM tg_payments WHERE kind='donate' AND refunded=0").fetchone()
    return render_template('support.html', bot=bot, error=error, presets=DONATE_PRESETS, min_rub=DONATE_MIN, max_rub=max_rub,
                           star_rub=TG_STAR_RUB, donors=total[0], value=raw[:12])


@app.route('/terms')
def terms():
    return render_template('legal.html', kind='terms')


@app.route('/privacy')
def privacy():
    return render_template('legal.html', kind='privacy')


CODE_TTL = 15 * 60          # код действует 15 минут
CODE_RESEND = 60            # новый код — не чаще раза в минуту
CODE_TRIES = 5              # попыток ввода на один код


def code_hash(email, code):
    return hmac.new(app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key,
                    f'{email}|{code}'.encode(), hashlib.sha256).hexdigest()


def create_user(con, email, pw_hash, lang):
    """Создать аккаунт с пробным периодом. None — почта уже занята."""
    try:
        cur = con.execute('INSERT INTO users (email, pw_hash, lang, created_at, trial_end) VALUES (?,?,?,?,?)',
                          (email, pw_hash, lang, now(), now() + TRIAL_DAYS * 86400))
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback()
        return None
    return con.execute('SELECT * FROM users WHERE id=?', (cur.lastrowid,)).fetchone()


def send_reg_code(con, email):
    """Выслать новый код на почту. Возвращает текст ошибки или None."""
    keys = ('regmail|' + client_ip(), 'regmail|' + email)
    if too_many(con, keys[0], 10, 3600) or too_many(con, keys[1], 5, 3600):
        return t('err_too_many')
    code = f'{secrets.randbelow(10 ** 6):06d}'
    for k in keys:
        note_attempt(con, k)
    if not send_mail(email, t('code_mail_subject', site=SITE_NAME), t('code_mail_body', code=code, site=SITE_NAME)):
        return t('err_mail_send')
    con.execute('UPDATE pending_regs SET code_hash=?, expires=?, tries=0, sent_at=? WHERE email=?',
                (code_hash(email, code), now() + CODE_TTL, now(), email))
    con.commit()
    return None


# Регистрация скрыта: аккаунт для игр не нужен. Код оставлен — вернуть можно настройкой REGISTRATION_OPEN=1 в .env.
REGISTRATION_OPEN = (env('REGISTRATION_OPEN') or '0') == '1'


@app.route('/register', methods=['GET', 'POST'])
def register():
    if not REGISTRATION_OPEN:
        return redirect(url_for('index'))
    if g.user:
        return redirect(url_for('account'))
    error, email = None, ''
    if request.method == 'POST':
        check_csrf()
        con = db()
        email = (request.form.get('email') or '').strip().lower()
        pw = request.form.get('password') or ''
        if too_many(con, 'reg|' + client_ip(), 5, 3600):
            error = t('err_too_many')
        elif not re.fullmatch(r'[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}', email) or len(email) > 200:
            error = t('err_email')
        elif len(pw) < 8 or len(pw) > 200:
            error = t('err_password_short')
        elif request.form.get('agree') != '1':
            error = t('err_agree')
        elif con.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
            error = t('err_email_taken')
        elif not EMAIL_VERIFY:
            u = create_user(con, email, generate_password_hash(pw), g.lang)
            if not u:
                error = t('err_email_taken')
            else:
                note_attempt(con, 'reg|' + client_ip())
                start_session(u)
                return redirect(url_for('account', welcome=1))
        else:
            # аккаунт появится только после ввода кода из письма
            con.execute('DELETE FROM pending_regs WHERE expires<?', (now() - 86400,))
            old = con.execute('SELECT * FROM pending_regs WHERE email=?', (email,)).fetchone()
            con.execute('INSERT OR REPLACE INTO pending_regs (email, pw_hash, lang, code_hash, expires, tries, sent_at) '
                        'VALUES (?,?,?,?,?,?,?)', (email, generate_password_hash(pw), g.lang,
                                                   old['code_hash'] if old else '-', old['expires'] if old else 0,
                                                   old['tries'] if old else CODE_TRIES, old['sent_at'] if old else 0))
            con.commit()
            fresh = old and old['sent_at'] > now() - CODE_RESEND and old['expires'] > now()
            error = None if fresh else send_reg_code(con, email)
            if not error:
                session['reg_email'] = email
                return redirect(url_for('verify'))
    return render_template('auth.html', mode='register', error=error, email=email)


@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if g.user:
        return redirect(url_for('account'))
    if not REGISTRATION_OPEN:
        return redirect(url_for('index'))
    email = session.get('reg_email') or ''
    con = db()
    row = con.execute('SELECT * FROM pending_regs WHERE email=?', (email,)).fetchone() if email else None
    if not row:
        return redirect(url_for('register'))
    error, notice = None, None
    if request.method == 'POST':
        check_csrf()
        if request.form.get('action') == 'resend':
            if row['sent_at'] > now() - CODE_RESEND:
                error = t('verify_wait')
            else:
                error = send_reg_code(con, email)
                notice = None if error else t('verify_resent')
        else:
            code = re.sub(r'\D', '', request.form.get('code') or '')
            if row['tries'] >= CODE_TRIES:
                error = t('err_code_tries')
            elif row['expires'] < now():
                error = t('err_code_expired')
            else:
                con.execute('UPDATE pending_regs SET tries=tries+1 WHERE email=?', (email,))
                con.commit()
                if len(code) == 6 and hmac.compare_digest(code_hash(email, code), row['code_hash']):
                    con.execute('DELETE FROM pending_regs WHERE email=?', (email,))
                    con.commit()
                    session.pop('reg_email', None)
                    u = create_user(con, email, row['pw_hash'], row['lang'] or g.lang)
                    if not u:
                        return render_template('auth.html', mode='register', error=t('err_email_taken'), email=email)
                    note_attempt(con, 'reg|' + client_ip())
                    start_session(u)
                    return redirect(url_for('account', welcome=1))
                error = t('err_code') if row['tries'] + 1 < CODE_TRIES else t('err_code_tries')
    return render_template('auth.html', mode='verify', error=error, notice=notice, email=email)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if g.user:
        return redirect(url_for('account'))
    error, email = (t('err_banned') if request.args.get('banned') else None), ''
    if request.method == 'POST':
        check_csrf()
        con = db()
        email = (request.form.get('email') or '').strip().lower()
        keys = ('login|' + client_ip(), 'login|' + email)
        if too_many(con, keys[0], 20, 900) or too_many(con, keys[1], 8, 900):
            error = t('err_too_many')
        else:
            row = con.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            pw_ok = check_password_hash(row['pw_hash'] if row else DUMMY_HASH, request.form.get('password') or '')
            if row and pw_ok and row['banned']:
                error = t('err_banned')
            elif row and pw_ok:
                start_session(row)
                nxt = request.args.get('next') or ''
                return redirect(nxt if re.fullmatch(r'/[A-Za-z0-9/_\-]*', nxt) else url_for('account'))
            else:
                for k in keys:
                    note_attempt(con, k)
                error = t('err_login')
    return render_template('auth.html', mode='login', error=error, email=email)


DUMMY_HASH = generate_password_hash('dummy-password-for-timing')


@app.route('/logout', methods=['POST'])
def logout():
    check_csrf()
    lang = g.lang
    session.clear()
    session['lang'] = lang
    return redirect(url_for('index'))


def send_mail(to, subject, body):
    if not SMTP_HOST:
        return False
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = SMTP_FROM, to, subject
    msg.set_content(body)
    try:
        s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) if SMTP_PORT == 465 else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        if SMTP_PORT != 465:
            s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
        s.quit()
        return True
    except Exception:
        log.exception('SMTP error')
        return False


@app.route('/forgot', methods=['GET', 'POST'])
def forgot():
    sent = False
    if request.method == 'POST':
        check_csrf()
        con = db()
        email = (request.form.get('email') or '').strip().lower()
        if not too_many(con, 'forgot|' + client_ip(), 5, 3600):
            note_attempt(con, 'forgot|' + client_ip())
            row = con.execute('SELECT * FROM users WHERE email=? AND banned=0', (email,)).fetchone()
            if row:
                token = secrets.token_urlsafe(32)
                con.execute('UPDATE users SET reset_token=?, reset_expires=? WHERE id=?',
                            (hashlib.sha256(token.encode()).hexdigest(), now() + 3600, row['id']))
                con.commit()
                link = f'{BASE_URL}/reset/{token}'
                if not send_mail(email, t('reset_mail_subject'), t('reset_mail_body', link=link)):
                    log.warning('Письмо не отправлено (SMTP не настроен). Ссылка сброса для %s: %s', email, link)
        sent = True
    return render_template('auth.html', mode='forgot', sent=sent, error=None, email='')


@app.route('/reset/<token>', methods=['GET', 'POST'])
def reset(token):
    con = db()
    row = con.execute('SELECT * FROM users WHERE reset_token=? AND reset_expires>? AND banned=0',
                      (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone()
    if not row:
        return render_template('auth.html', mode='reset', invalid=True, error=None, email='')
    error = None
    if request.method == 'POST':
        check_csrf()
        pw = request.form.get('password') or ''
        if len(pw) < 8 or len(pw) > 200:
            error = t('err_password_short')
        else:
            con.execute('UPDATE users SET pw_hash=?, reset_token=NULL, reset_expires=NULL, session_ver=session_ver+1 WHERE id=?',
                        (generate_password_hash(pw), row['id']))
            con.commit()
            start_session(con.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone())
            return redirect(url_for('account', pwchanged=1))
    return render_template('auth.html', mode='reset', invalid=False, error=error, email='')


@app.route('/account')
@login_required
def account():
    con = db()
    oid = request.args.get('o') or ''
    if oid and request.args.get('paid'):
        # вернулись со страницы ЮMoney — сразу проверяем оплату на сервере (не чаще 6 раз в минуту)
        order = con.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (oid, g.user['id'])).fetchone()
        if order and order['status'] == 'pending' and not too_many(con, 'check|' + str(g.user['id']), 6, 60):
            note_attempt(con, 'check|' + str(g.user['id']))
            try:
                if verify_order(con, order):
                    g.user = con.execute('SELECT * FROM users WHERE id=?', (g.user['id'],)).fetchone()
                    return render_template('account.html', orders=account_orders(con), confirmed=True)
            except YooMoneyError:
                pass
            return redirect(url_for('account', o=oid, notyet=1))
    return render_template('account.html', orders=account_orders(con), confirmed=False)


def account_orders(con):
    return con.execute("SELECT * FROM orders WHERE user_id=? AND (status IN ('review','paid','rejected') "
                       "OR (status='pending' AND method LIKE 'yoomoney%' AND created_at>?)) "
                       "ORDER BY created_at DESC LIMIT 10", (g.user['id'], now() - 2 * 86400)).fetchall()


@app.route('/account/password', methods=['POST'])
@login_required
def change_password():
    check_csrf()
    if not check_password_hash(g.user['pw_hash'], request.form.get('old') or ''):
        return redirect(url_for('account', pwerr=1))
    pw = request.form.get('new') or ''
    if len(pw) < 8 or len(pw) > 200:
        return redirect(url_for('account', pwerr=2))
    con = db()
    con.execute('UPDATE users SET pw_hash=?, session_ver=session_ver+1 WHERE id=?', (generate_password_hash(pw), g.user['id']))
    con.commit()
    start_session(con.execute('SELECT * FROM users WHERE id=?', (g.user['id'],)).fetchone())   # другие устройства выйдут
    return redirect(url_for('account', pwchanged=1))


def tg_link_url(con, user_id, product=None):
    """Одноразовая ссылка t.me/бот?start=КОД (15 минут) — привязывает Telegram к аккаунту."""
    con.execute('DELETE FROM tg_links WHERE expires<? OR user_id=?', (now(), user_id))
    code = secrets.token_urlsafe(18)
    con.execute('INSERT INTO tg_links (code, user_id, product, expires) VALUES (?,?,?,?)',
                (code, user_id, product, now() + 900))
    con.commit()
    return f'https://t.me/{tg_bot_username(con)}?start={code}'


@app.route('/account/telegram', methods=['POST'])
@login_required
def account_telegram():
    check_csrf()
    con = db()
    if request.form.get('action') == 'unlink':
        con.execute('UPDATE users SET tg_id=NULL WHERE id=?', (g.user['id'],))
        con.commit()
        return redirect(url_for('account'))
    if not tg_bot_username(con):
        abort(404)
    if too_many(con, 'tg|' + str(g.user['id']), 20, 3600):
        return redirect(url_for('account', wait=1))
    note_attempt(con, 'tg|' + str(g.user['id']))
    return redirect(tg_link_url(con, g.user['id']))


# ---------------------------------------------------------------- игры и скачивание
@app.route('/play/<game>')
def play(game):
    if game not in GAMES:
        abort(404)
    if not can_play(g.user):
        abort(403)
    return render_template('play.html', game=game)


@app.route('/games/<game>/')
def game_file(game):
    if game not in GAMES or not can_play(g.user):
        abort(403)
    resp = send_from_directory(GAMES_DIR / game, 'index.html')
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


def list_downloads():
    if not DOWNLOADS_DIR.is_dir():
        return []
    files = [p for p in DOWNLOADS_DIR.iterdir() if p.is_file() and p.suffix.lower() in DOWNLOAD_EXT]
    return sorted(({'name': p.name, 'size': p.stat().st_size, 'mtime': int(p.stat().st_mtime)} for p in files),
                  key=lambda f: f['name'].lower())


def game_files(con):
    """Файлы, привязанные к играм каталога: они выдаются только купившим эту игру."""
    return {r['file']: r['id'] for r in con.execute("SELECT id, file FROM games WHERE file IS NOT NULL AND file!=''")}


@app.route('/downloads')
def downloads():
    con = db()
    ok = can_download(g.user)
    taken = game_files(con)
    files = [f for f in list_downloads() if f['name'] not in taken] if ok else []
    mine = con.execute("SELECT * FROM games WHERE file IS NOT NULL AND file!='' ORDER BY sort, id").fetchall() if ok else []
    return render_template('downloads.html', files=files, access=ok, mine=mine)


@app.route('/download/<name>')
def download_file(name):
    if name != secure_filename(name) or Path(name).suffix.lower() not in DOWNLOAD_EXT or not (DOWNLOADS_DIR / name).is_file():
        abort(404)
    con = db()
    gid = game_files(con).get(name)
    allowed = owns_game(con, g.user, gid) if gid else can_download(g.user)
    if not allowed:
        return redirect(url_for('game_page', gid=gid) if gid else url_for('downloads'))
    resp = send_from_directory(DOWNLOADS_DIR, name, as_attachment=True)
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


# ---------------------------------------------------------------- покупки
def new_order(con, user_id, product, method, status='pending'):
    oid = secrets.token_hex(8)
    con.execute('INSERT INTO orders (id, user_id, product, amount, method, status, created_at) VALUES (?,?,?,?,?,?,?)',
                (oid, user_id, product, product_price(con, product), method, status, now()))
    con.commit()
    return oid


def complete_order(con, order, operation_id=None):
    """Отметить заказ оплаченным и выдать доступ (повторный вызов ничего не делает)."""
    cur = con.execute("UPDATE orders SET status='paid', paid_at=?, operation_id=COALESCE(?, operation_id) "
                      "WHERE id=? AND status IN ('pending','review')", (now(), operation_id, order['id']))
    con.commit()
    if cur.rowcount:
        grant(con, order['user_id'], product=order['product'])
        log.info('Заказ %s оплачен (%s, %s ₽, user %s)', order['id'], order['product'], order['amount'], order['user_id'])
        return True
    return False


@app.route('/buy', methods=['POST'])
@login_required
def buy():
    return redirect(url_for('support'))      # всё бесплатно: покупок больше нет


def _buy_disabled():
    check_csrf()
    product = request.form.get('product') or ''
    method = request.form.get('method')
    con = db()
    price = product_price(con, product)
    if price is None:
        abort(400)
    gid = int(product[5:]) if product.startswith('game:') else None
    back = (lambda **kw: url_for('game_page', gid=gid, **kw)) if gid else (lambda **kw: url_for('pricing', **kw))
    if (product == 'forever' and g.user['lifetime']) or (gid and owns_game(con, g.user, gid)):
        return redirect(url_for('account'))
    if too_many(con, 'buy|' + str(g.user['id']), 20, 3600):
        return redirect(back(toomany=1))
    note_attempt(con, 'buy|' + str(g.user['id']))

    if method == 'balance':
        cur = con.execute('UPDATE users SET balance=ROUND(balance-?, 2) WHERE id=? AND balance>=?', (price, g.user['id'], price))
        con.commit()
        if not cur.rowcount:
            return redirect(back(lowbalance=1))
        con.execute('INSERT INTO balance_log (user_id, delta, reason, created_at) VALUES (?,?,?,?)',
                    (g.user['id'], -price, f'Покупка: {product_title(product)}', now()))
        oid = new_order(con, g.user['id'], product, 'balance')
        complete_order(con, con.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone())
        return redirect(url_for('account', paid=1))

    if method not in methods_available():
        return redirect(back(unavailable=1))
    oid = new_order(con, g.user['id'], product, method)
    return redirect(url_for('pay', oid=oid))


@app.route('/pay/<oid>')
@login_required
def pay(oid):
    order = db().execute('SELECT * FROM orders WHERE id=? AND user_id=?', (oid, g.user['id'])).fetchone()
    if not order:
        abort(404)
    if order['status'] in ('paid', 'review', 'rejected') or order['method'] == 'balance':
        return redirect(url_for('account'))
    if order['method'].startswith('yoomoney') and order['status'] == 'pending':
        form = {'receiver': YOOMONEY_WALLET, 'quickpay-form': 'button', 'sum': f"{order['amount']:.2f}",
                'label': order['id'], 'paymentType': 'AC' if order['method'] == 'yoomoney_card' else 'PC',
                'successURL': f'{BASE_URL}/account?paid=1&o={order["id"]}',   # это лишь возврат на сайт: оплату проверяет сервер
                'targets': f'{SITE_NAME}: {product_title(order["product"])}'}
        return render_template('pay_yoomoney.html', form=form, order=order)
    return render_template('pay_card.html', order=order, card=fmt_card(CARD_NUMBER), holder=CARD_HOLDER, bank=CARD_BANK,
                           code=order['id'][:6].upper())


@app.route('/pay/<oid>/sent', methods=['POST'])
@login_required
def pay_sent(oid):
    check_csrf()
    con = db()
    note = (request.form.get('note') or '').strip()[:200]
    con.execute("UPDATE orders SET status='review', payer_note=? WHERE id=? AND user_id=? AND method='card' AND status='pending'",
                (note, oid, g.user['id']))
    con.commit()
    return redirect(url_for('account', review=1))


# ---------------------------------------------------------------- проверка оплат ЮMoney на сервере
# Сайт не верит ни браузеру, ни содержимому уведомления: факт оплаты сервер сам запрашивает
# у ЮMoney по HTTPS (проверка сертификата, токен OAuth в заголовке) в истории операций кошелька.
_TLS = ssl.create_default_context()
_TLS.minimum_version = ssl.TLSVersion.TLSv1_2


class YooMoneyError(Exception):
    pass


def ym_api(method, params, token=None):
    """Защищённый запрос к API ЮMoney. Возвращает JSON."""
    if not token:
        raise YooMoneyError('нет токена ЮMoney')
    req = urllib.request.Request(f'{YOOMONEY_API}/api/{method}', data=urllib.parse.urlencode(params).encode(),
                                 headers={'Authorization': f'Bearer {token}',
                                          'Content-Type': 'application/x-www-form-urlencoded',
                                          'User-Agent': 'kolca-orbity-site'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15, context=_TLS if YOOMONEY_API.startswith('https') else None) as r:
            data = json.loads(r.read(1_000_000).decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            log.error('ЮMoney: токен недействителен — выполните  sudo bash manage.sh yoomoney-auth')
        raise YooMoneyError(f'HTTP {e.code}') from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise YooMoneyError(str(e)) from None
    if isinstance(data, dict) and data.get('error'):
        raise YooMoneyError(data['error'])
    return data


def verify_order(con, order):
    """Спросить у ЮMoney, пришли ли деньги по заказу. True — заказ оплачен (сейчас или раньше)."""
    if order['status'] == 'paid':
        return True
    if not order['method'].startswith('yoomoney') or order['status'] != 'pending':
        return False
    data = ym_api('operation-history', {'type': 'deposition', 'label': order['id'], 'records': 10}, token=yoomoney_token(con))
    for op in data.get('operations') or []:
        if op.get('label') != order['id'] or op.get('direction') != 'in' or op.get('status') != 'success':
            continue
        credited = float(op.get('amount') or 0)
        if credited + 0.01 < order['amount'] * YOOMONEY_MIN_SHARE:
            con.execute('UPDATE orders SET payer_note=? WHERE id=?',
                        (f"Недоплата: пришло {credited:.2f} ₽ (операция {op.get('operation_id')})", order['id']))
            con.commit()
            log.warning('ЮMoney: по заказу %s пришло %.2f ₽ при цене %.2f ₽', order['id'], credited, order['amount'])
            continue
        try:
            complete_order(con, order, operation_id=str(op.get('operation_id')))
        except sqlite3.IntegrityError:
            con.rollback()        # не держим блокировку базы
            log.warning('ЮMoney: операция %s уже засчитана другому заказу', op.get('operation_id'))
            continue
        return True
    return False


def check_pending_orders(con, max_checks=25):
    """Фоновая проверка неоплаченных заказов ЮMoney за последние 2 суток."""
    if not yoomoney_token(con):
        return 0
    rows = con.execute("SELECT * FROM orders WHERE status='pending' AND method LIKE 'yoomoney%' AND created_at>? "
                       "ORDER BY created_at DESC LIMIT ?", (now() - 2 * 86400, max_checks)).fetchall()
    done = 0
    for o in rows:
        try:
            done += verify_order(con, o)
        except YooMoneyError as e:
            log.warning('ЮMoney: проверка заказа %s не удалась: %s', o['id'], e)
            break
    return done


def yoomoney_signature_ok(f):
    fields = ('notification_type', 'operation_id', 'amount', 'currency', 'datetime', 'sender', 'codepro')
    base = '&'.join(f.get(k, '') for k in fields) + '&' + YOOMONEY_SECRET + '&' + f.get('label', '')
    expected = hashlib.sha1(base.encode('utf-8')).hexdigest()
    return hmac.compare_digest(expected, (f.get('sha1_hash') or '').lower())


@app.route('/webhooks/yoomoney', methods=['POST'])
def yoomoney_webhook():
    """Уведомление ЮMoney — только сигнал «проверь заказ». Сами данные уведомления не используются:
    оплату подтверждает сервер запросом к API ЮMoney."""
    if not YOOMONEY_SECRET:
        abort(404)
    f = request.form.to_dict()
    if not yoomoney_signature_ok(f):
        log.warning('ЮMoney: неверная подпись уведомления от %s', client_ip())
        abort(400)
    con = db()
    order = con.execute('SELECT * FROM orders WHERE id=?', (f.get('label') or '',)).fetchone()
    if order:
        try:
            verify_order(con, order)
        except YooMoneyError as e:
            log.warning('ЮMoney: не удалось проверить заказ %s сразу (%s) — проверит фоновая задача', order['id'], e)
    return 'ok'


@app.route('/pay/<oid>/check', methods=['POST'])
@login_required
def pay_check(oid):
    """Кнопка «Проверить оплату»: сервер сам спрашивает ЮMoney."""
    check_csrf()
    con = db()
    order = con.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (oid, g.user['id'])).fetchone()
    if not order:
        abort(404)
    if too_many(con, 'check|' + str(g.user['id']), 6, 60):
        return redirect(url_for('account', o=oid, wait=1))
    note_attempt(con, 'check|' + str(g.user['id']))
    try:
        paid = verify_order(con, order)
    except YooMoneyError:
        paid = False
    return redirect(url_for('account', paid=1) if paid else url_for('account', o=oid, notyet=1))


@app.route('/yoomoney/oauth')
def yoomoney_oauth_code():
    """Сюда ЮMoney возвращает код подтверждения — его нужно вставить в терминал (manage.sh yoomoney-auth)."""
    code = request.args.get('code') or ''
    error = request.args.get('error') or ''
    return render_template('oauth_code.html', code=code if re.fullmatch(r'[A-Za-z0-9._\-]{10,600}', code) else '', error=error)


def run_worker():
    log.info('Фоновая проверка оплат запущена')
    while True:
        con = db_connect()
        try:
            n = check_pending_orders(con)
            if n:
                log.info('Фоновая проверка: оплачено заказов — %s', n)
        except Exception:
            log.exception('worker')
        finally:
            con.close()
        time.sleep(int(env('WORKER_INTERVAL') or 60))


# ---------------------------------------------------------------- админка
admin = Blueprint('admin', __name__)


def admin_pw_hash(con):
    return setting(con, 'admin_pw_hash')


def admin_logged_in():
    a = session.get('adm')
    if not a:
        return False
    con = db()
    h = admin_pw_hash(con)
    if (not h or a.get('v') != hashlib.sha256(h.encode()).hexdigest()[:16]
            or a.get('ip') != client_ip() or now() - a.get('t', 0) > 2 * 3600):
        session.pop('adm', None)
        return False
    a['t'] = now()
    session['adm'] = a
    return True


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not admin_logged_in():
            return redirect(url_for('admin.login'))
        return f(*a, **kw)
    return wrapper


def admin_log(con, action, target='', details=''):
    con.execute('INSERT INTO admin_log (action, target_email, details, ip, created_at) VALUES (?,?,?,?,?)',
                (action, target, details, client_ip(), now()))
    con.commit()


@admin.route('/login', methods=['GET', 'POST'])
def login():
    con = db()
    error = None
    if not admin_pw_hash(con):
        return render_template('admin_login.html', no_password=True, error=None)
    if request.method == 'POST':
        check_csrf()
        ip_key, all_key = 'admin|' + client_ip(), 'admin|*'
        if too_many(con, ip_key, 5, 900) or too_many(con, all_key, 30, 900):
            error = 'Слишком много попыток. Подождите 15 минут.'
        elif check_password_hash(admin_pw_hash(con), request.form.get('password') or ''):
            lang = g.lang
            uid, sv = session.get('uid'), session.get('sv')
            session.clear()
            session.update(lang=lang, csrf=secrets.token_urlsafe(24),
                           adm={'t': now(), 'ip': client_ip(), 'v': hashlib.sha256(admin_pw_hash(con).encode()).hexdigest()[:16]})
            if uid:
                session.update(uid=uid, sv=sv)
            admin_log(con, 'Вход в админку')
            return redirect(url_for('admin.users'))
        else:
            note_attempt(con, ip_key)
            note_attempt(con, all_key)
            log.warning('Неудачный вход в админку с %s', client_ip())
            time.sleep(1)
            error = 'Неверный пароль.'
    return render_template('admin_login.html', no_password=False, error=error)


@admin.route('/logout', methods=['POST'])
def logout():
    check_csrf()
    session.pop('adm', None)
    return redirect(url_for('index'))


@admin.route('/')
@admin_required
def users():
    con = db()
    q = (request.args.get('q') or '').strip().lower()[:100]
    flt = request.args.get('f') or ''
    page = max(1, min(10_000, int(request.args.get('page') or 1) if (request.args.get('page') or '1').isdigit() else 1))
    where, args = ['1=1'], []
    if q:
        where.append('email LIKE ?')
        args.append(f'%{q}%')
    if flt == 'forever':
        where.append('lifetime=1')
    elif flt == 'active':
        where.append('(lifetime=1 OR paid_until>? OR trial_end>?)')
        args += [now(), now()]
    elif flt == 'banned':
        where.append('banned=1')
    elif flt == 'balance':
        where.append('balance>0')
    w = ' AND '.join(where)
    total = con.execute(f'SELECT COUNT(*) FROM users WHERE {w}', args).fetchone()[0]
    rows = con.execute(f'SELECT * FROM users WHERE {w} ORDER BY id DESC LIMIT 50 OFFSET ?', (*args, (page - 1) * 50)).fetchall()
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    stats = dict(
        users=one('SELECT COUNT(*) FROM users'),
        forever=one('SELECT COUNT(*) FROM users WHERE lifetime=1'),
        month=one('SELECT COUNT(*) FROM users WHERE lifetime=0 AND paid_until>?', now()),
        review=one("SELECT COUNT(*) FROM orders WHERE status='review'"),
        revenue_month=one("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND method!='balance' AND paid_at>?", now() - 30 * 86400),
        revenue_total=one("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND method!='balance'"),
    )
    stats['ym'] = True        # ЮMoney отключён — предупреждение в админке не нужно
    d = con.execute("SELECT COUNT(*), COALESCE(SUM(rub),0), COALESCE(SUM(stars),0) FROM tg_payments WHERE kind='donate' AND refunded=0").fetchone()
    d30 = con.execute("SELECT COALESCE(SUM(rub),0) FROM tg_payments WHERE kind='donate' AND refunded=0 AND created_at>?", (now() - 30 * 86400,)).fetchone()[0]
    stats.update(donations=d[0], donated=d[1], donated_stars=d[2], donated_30=d30)
    stats['tg'] = bool(tg_bot_username(con))
    return render_template('admin_users.html', rows=rows, stats=stats, q=q, f=flt, page=page,
                           pages=max(1, (total + 49) // 50), total=total)


@admin.route('/user/<int:uid>')
@admin_required
def user(uid):
    con = db()
    u = con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        abort(404)
    return render_template('admin_user.html', u=u, msg=request.args.get('msg'),
                           sale_games=con.execute("SELECT * FROM games WHERE status='paid' ORDER BY sort, id").fetchall(),
                           mine={r['game_id'] for r in con.execute('SELECT game_id FROM user_games WHERE user_id=?', (uid,))},
                           orders=con.execute('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 30', (uid,)).fetchall(),
                           balance_log=con.execute('SELECT * FROM balance_log WHERE user_id=? ORDER BY id DESC LIMIT 30', (uid,)).fetchall())


def parse_amount(s):
    try:
        v = round(float((s or '').replace(',', '.').replace(' ', '')), 2)
    except ValueError:
        return None
    return v if 0 < v <= 10_000_000 else None


def delete_user(con, u):
    uid = u['id']
    con.execute('DELETE FROM balance_log WHERE user_id=?', (uid,))
    con.execute('DELETE FROM tg_links WHERE user_id=?', (uid,))
    con.execute('DELETE FROM user_games WHERE user_id=?', (uid,))
    con.execute('UPDATE orders SET user_id=NULL WHERE user_id=?', (uid,))     # оплаты остаются в истории
    con.execute('DELETE FROM users WHERE id=?', (uid,))
    con.commit()
    admin_log(con, 'Удалил аккаунт', u['email'])


@admin.route('/users/delete', methods=['POST'])
@admin_required
def users_delete():
    """Удаление прямо из списка: один аккаунт (кнопка в строке) или отмеченные галочками."""
    check_csrf()
    con = db()
    if request.form.get('confirm') != 'yes':
        return redirect(url_for('admin.users', msg='need_confirm'))
    ids = [int(x) for x in request.form.getlist('ids') if x.isdigit()][:100]
    n = 0
    for uid in ids:
        u = con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if u:
            delete_user(con, u)
            n += 1
    return redirect(url_for('admin.users', msg='deleted', n=n))


@admin.route('/user/<int:uid>/<action>', methods=['POST'])
@admin_required
def user_action(uid, action):
    check_csrf()
    con = db()
    u = con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        abort(404)
    if action == 'add_days':
        try:
            days = int(request.form.get('days') or 0)
        except ValueError:
            days = 0
        if not 1 <= days <= 3650:
            return redirect(url_for('admin.user', uid=uid, msg='bad_days'))
        grant(con, uid, days=days)
        admin_log(con, 'Добавил дни', u['email'], f'{days} дн.')
        msg = 'granted'
    elif action == 'give_forever':
        grant(con, uid, product='forever')
        admin_log(con, 'Выдал «Навсегда»', u['email'])
        msg = 'forever'
    elif action == 'revoke':
        con.execute('UPDATE users SET lifetime=0, paid_until=NULL, trial_end=? WHERE id=?', (now() - 1, uid))
        con.commit()
        admin_log(con, 'Снял подписку', u['email'])
        msg = 'revoked'
    elif action in ('balance_add', 'balance_sub'):
        amount = parse_amount(request.form.get('amount'))
        if amount is None:
            return redirect(url_for('admin.user', uid=uid, msg='bad_amount'))
        reason = (request.form.get('reason') or '').strip()[:200] or ('Начисление' if action == 'balance_add' else 'Списание')
        delta = amount if action == 'balance_add' else -min(amount, max(0.0, u['balance']))
        change_balance(con, uid, delta, reason)
        admin_log(con, 'Баланс', u['email'], f'{delta:+.2f} ₽ — {reason}')
        msg = 'balance'
    elif action == 'ban':
        reason = (request.form.get('reason') or '').strip()[:200]
        con.execute('UPDATE users SET banned=1, ban_reason=?, session_ver=session_ver+1 WHERE id=?', (reason, uid))
        con.commit()
        admin_log(con, 'Заблокировал', u['email'], reason)
        msg = 'banned'
    elif action == 'unban':
        con.execute('UPDATE users SET banned=0, ban_reason=NULL WHERE id=?', (uid,))
        con.commit()
        admin_log(con, 'Разблокировал', u['email'])
        msg = 'unbanned'
    elif action in ('give_game', 'revoke_game'):
        gid = int(request.form.get('game') or 0) if (request.form.get('game') or '').isdigit() else 0
        row = con.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone()
        if not row:
            abort(400)
        if action == 'give_game':
            grant(con, uid, product=f'game:{gid}')
            admin_log(con, 'Выдал игру', u['email'], row['title'])
        else:
            con.execute('DELETE FROM user_games WHERE user_id=? AND game_id=?', (uid, gid))
            con.commit()
            admin_log(con, 'Забрал игру', u['email'], row['title'])
        msg = 'game'
    elif action == 'logout_all':
        con.execute('UPDATE users SET session_ver=session_ver+1 WHERE id=?', (uid,))
        con.commit()
        admin_log(con, 'Завершил сеансы', u['email'])
        msg = 'sessions'
    elif action == 'delete':
        if (request.form.get('confirm') or '').strip().lower() != u['email']:
            return redirect(url_for('admin.user', uid=uid, msg='bad_confirm'))
        delete_user(con, u)
        return redirect(url_for('admin.users', msg='deleted'))
    else:
        abort(400)
    return redirect(url_for('admin.user', uid=uid, msg=msg))


@admin.route('/orders')
@admin_required
def orders():
    con = db()
    rows = con.execute("SELECT o.*, u.email FROM orders o LEFT JOIN users u ON u.id=o.user_id "
                       "WHERE o.status!='pending' OR o.created_at>? ORDER BY (o.status='review') DESC, o.created_at DESC LIMIT 200",
                       (now() - 86400,)).fetchall()
    return render_template('admin_orders.html', rows=rows, msg=request.args.get('msg'))


@admin.route('/orders/<oid>/<action>', methods=['POST'])
@admin_required
def order_action(oid, action):
    check_csrf()
    con = db()
    o = con.execute('SELECT o.*, u.email FROM orders o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=?', (oid,)).fetchone()
    if not o or not o['user_id']:
        abort(404)
    if action == 'confirm':
        complete_order(con, o)
        admin_log(con, 'Подтвердил оплату', o['email'], f"{product_title(o['product'])}, {fmt_rub(o['amount'])}")
    elif action == 'reject':
        con.execute("UPDATE orders SET status='rejected' WHERE id=? AND status IN ('pending','review')", (oid,))
        con.commit()
        admin_log(con, 'Отклонил оплату', o['email'], f"{product_title(o['product'])}, {fmt_rub(o['amount'])}")
    else:
        abort(400)
    return redirect(url_for('admin.orders', msg=action))


def save_cover(f, gid):
    """Сохранить обложку игры (PNG/JPG/WebP до 5 МБ, тип проверяется по содержимому)."""
    return save_image_bytes(f.read(COVER_LIMIT + 1), gid)


def save_image_bytes(data, gid):
    if not data or len(data) > COVER_LIMIT:
        return None
    ext = next((e for magic, e in COVER_EXT.items() if data.startswith(magic)), None)
    if not ext or (ext == '.webp' and data[8:12] != b'WEBP'):
        return None
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    name = f'g{gid}-{secrets.token_hex(4)}{ext}'
    (COVERS_DIR / name).write_bytes(data)
    return name


def drop_cover(name):
    if name and name == secure_filename(name) and (COVERS_DIR / name).is_file():
        (COVERS_DIR / name).unlink()


@admin.route('/games', methods=['GET', 'POST'])
@admin_required
def games():
    con = db()
    msg = request.args.get('msg')
    if request.method == 'POST':
        check_csrf()
        act = request.form.get('action')
        gid = int(request.form.get('id') or 0) if (request.form.get('id') or '0').isdigit() else 0
        row = con.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone() if gid else None
        if act == 'delete' and row:
            drop_cover(row['cover'])
            for name in json.loads(row['shots'] or '[]'):
                drop_cover(name)
            con.execute('DELETE FROM games WHERE id=?', (gid,))
            con.execute('DELETE FROM user_games WHERE game_id=?', (gid,))
            con.commit()
            admin_log(con, 'Удалил игру', '', row['title'])
            return redirect(url_for('admin.games', msg='deleted'))
        if act == 'save':
            title = (request.form.get('title') or '').strip()[:60]
            desc = (request.form.get('description') or '').strip()[:300]
            status = request.form.get('status') if request.form.get('status') in ('soon', 'live', 'paid') else 'soon'
            price = parse_amount(request.form.get('price')) if status == 'paid' else None
            gfile = secure_filename(request.form.get('file') or '') or None
            if status == 'paid' and not price:
                return redirect(url_for('admin.games', msg='bad_price', edit=gid or ''))
            if gfile and not (DOWNLOADS_DIR / gfile).is_file():
                gfile = None
            url = (request.form.get('url') or '').strip()[:300]
            if url and not re.fullmatch(r'https?://[^\s<>"]+', url):
                return redirect(url_for('admin.games', msg='bad_url'))
            try:
                hue = int(request.form.get('hue') or 200) % 360
                sort = max(0, min(9999, int(request.form.get('sort') or 100)))
            except ValueError:
                hue, sort = 200, 100
            if not title:
                return redirect(url_for('admin.games', msg='no_title'))
            if row:
                con.execute('UPDATE games SET title=?, description=?, status=?, url=?, hue=?, sort=?, price=?, file=? WHERE id=?',
                            (title, desc, status, url or None, hue, sort, price, gfile, gid))
            else:
                gid = con.execute('INSERT INTO games (title, description, status, url, hue, sort, price, file, created_at) '
                                  'VALUES (?,?,?,?,?,?,?,?,?)', (title, desc, status, url or None, hue, sort, price, gfile, now())).lastrowid
            con.commit()
            f = request.files.get('cover')
            if f and f.filename:
                name = save_cover(f, gid)
                if not name:
                    return redirect(url_for('admin.games', msg='bad_cover'))
                old = con.execute('SELECT cover FROM games WHERE id=?', (gid,)).fetchone()['cover']
                con.execute('UPDATE games SET cover=? WHERE id=?', (name, gid))
                con.commit()
                drop_cover(old)
            elif request.form.get('drop_cover') and row:
                drop_cover(row['cover'])
                con.execute('UPDATE games SET cover=NULL WHERE id=?', (gid,))
                con.commit()
            admin_log(con, 'Изменил игру' if row else 'Добавил игру', '', title)
            return redirect(url_for('admin.games', msg='saved'))
        abort(400)
    edit = request.args.get('edit', '')
    cur = con.execute('SELECT * FROM games WHERE id=?', (int(edit),)).fetchone() if edit.isdigit() else None
    sold = {r['game_id']: r['n'] for r in con.execute('SELECT game_id, COUNT(*) n FROM user_games GROUP BY game_id')}
    return render_template('admin_games.html', games=catalog(con), cur=cur, msg=msg, files=list_downloads(), sold=sold)


@admin.route('/downloads', methods=['GET', 'POST'])
@admin_required
def downloads():
    msg = None
    if request.method == 'POST':
        check_csrf()
        con = db()
        if request.form.get('delete'):
            name = secure_filename(request.form['delete'])
            p = DOWNLOADS_DIR / name
            if name and p.is_file():
                p.unlink()
                admin_log(con, 'Удалил файл', '', name)
                msg = 'deleted'
        else:
            DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
            saved = []
            for f in request.files.getlist('files'):
                name = secure_filename(f.filename or '')
                if name and Path(name).suffix.lower() in DOWNLOAD_EXT:
                    f.save(DOWNLOADS_DIR / name)
                    saved.append(name)
            if saved:
                admin_log(con, 'Загрузил файлы', '', ', '.join(saved))
            msg = 'uploaded' if saved else 'bad_file'
    return render_template('admin_downloads.html', files=list_downloads(), msg=msg, exts=', '.join(sorted(DOWNLOAD_EXT)))


@admin.route('/log')
@admin_required
def journal():
    return render_template('admin_log.html', rows=db().execute('SELECT * FROM admin_log ORDER BY id DESC LIMIT 300').fetchall())


if ADMIN_PATH and re.fullmatch(r'[A-Za-z0-9_\-]{8,64}', ADMIN_PATH):
    app.register_blueprint(admin, url_prefix='/' + ADMIN_PATH)
else:
    log.warning('ADMIN_PATH не задан или слишком короткий (нужно 8+ символов) — админка выключена')


# ---------------------------------------------------------------- служебное
@app.route('/healthz')
def healthz():
    db().execute('SELECT 1')
    return 'ok'


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def http_error(e):
    return render_template('error.html', code=e.code), e.code


def cli_admin_password():
    pw = None
    if not sys.stdin.isatty():
        pw = sys.stdin.readline().rstrip('\r\n')       # из manage.sh пароль приходит через stdin
    if not pw:
        pw = getpass.getpass('Новый пароль администратора (от 12 символов): ')
        if pw != getpass.getpass('Повторите пароль: '):
            sys.exit('Пароли не совпадают')
    if len(pw) < 12:
        sys.exit('Слишком короткий пароль: нужно хотя бы 12 символов')
    con = db_connect()
    set_setting(con, 'admin_pw_hash', generate_password_hash(pw))
    con.execute("INSERT INTO admin_log (action, created_at, ip) VALUES ('Сменён пароль администратора (из терминала)', ?, 'console')", (now(),))
    con.commit()
    print('Пароль администратора сохранён. Все прежние входы в админку завершены.')


def cli_stats():
    con = db_connect()
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    print(f"Пользователей: {one('SELECT COUNT(*) FROM users')}")
    print(f"«Навсегда»:    {one('SELECT COUNT(*) FROM users WHERE lifetime=1')}")
    print(f"Месяц онлайн:  {one('SELECT COUNT(*) FROM users WHERE lifetime=0 AND paid_until>?', now())}")
    review = one("SELECT COUNT(*) FROM orders WHERE status='review'")
    total = one("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid' AND method!='balance'")
    print(f'Ждут проверки переводов: {review}')
    print(f'Оплачено всего: {fmt_rub(total)}')


def cli_yoomoney_auth():
    """Получить токен ЮMoney (права: история и детали операций) — всё в терминале."""
    if not YOOMONEY_CLIENT_ID:
        sys.exit('Сначала впишите YOOMONEY_CLIENT_ID (sudo bash manage.sh config)')
    redirect_uri = YOOMONEY_REDIRECT_URI or f'{BASE_URL}/yoomoney/oauth'
    q = urllib.parse.urlencode({'client_id': YOOMONEY_CLIENT_ID, 'response_type': 'code',
                                'redirect_uri': redirect_uri, 'scope': 'operation-history operation-details'})
    print('\n1) Откройте ссылку в браузере (можно на телефоне), войдите в ЮMoney и нажмите «Разрешить»:\n')
    print(f'   {YOOMONEY_API}/oauth/authorize?{q}\n')
    print('2) Откроется страница с кодом. Скопируйте код — или, если страница не открылась,')
    print('   скопируйте целиком адрес из строки браузера (в нём есть code=...) — и вставьте сюда.\n')
    code = (sys.stdin.readline() if not sys.stdin.isatty() else input('Код или адрес: ')).strip()
    if 'code=' in code:
        code = (urllib.parse.parse_qs(urllib.parse.urlsplit(code).query).get('code') or [''])[0]
    if not code:
        sys.exit('Код не введён')
    params = {'code': code, 'client_id': YOOMONEY_CLIENT_ID, 'grant_type': 'authorization_code', 'redirect_uri': redirect_uri}
    if YOOMONEY_CLIENT_SECRET:
        params['client_secret'] = YOOMONEY_CLIENT_SECRET
    req = urllib.request.Request(f'{YOOMONEY_API}/oauth/token', data=urllib.parse.urlencode(params).encode(),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20, context=_TLS if YOOMONEY_API.startswith('https') else None) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        sys.exit(f'Не удалось получить токен: {e}')
    token = data.get('access_token')
    if not token:
        sys.exit(f"ЮMoney отказал: {data.get('error') or data}")
    info = ym_api('account-info', {}, token=token)
    if YOOMONEY_WALLET and str(info.get('account')) != YOOMONEY_WALLET:
        sys.exit(f"Токен от другого кошелька ({info.get('account')}), а в настройках {YOOMONEY_WALLET}. Войдите в нужный кошелёк.")
    con = db_connect()
    set_setting(con, 'yoomoney_token', token)
    con.execute("INSERT INTO admin_log (action, details, ip, created_at) VALUES ('Подключён API ЮMoney', ?, 'console', ?)",
                (f"кошелёк {info.get('account')}", now()))
    con.commit()
    print(f"Готово: кошелёк {info.get('account')} подключён, оплаты проверяются через API.")


def cli_yoomoney_check():
    con = db_connect()
    token = yoomoney_token(con)
    if not token:
        sys.exit('API ЮMoney не подключён: sudo bash manage.sh yoomoney-auth')
    info = ym_api('account-info', {}, token=token)
    print(f"Кошелёк {info.get('account')}: связь с API есть, баланс {info.get('balance')} ₽, статус {info.get('account_status')}")
    print(f'Проверено неоплаченных заказов, найдено оплат: {check_pending_orders(con)}')


def gh_get(url, limit=5_000_000, accept='application/vnd.github+json'):
    req = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'kolca-orbity-site'})
    with urllib.request.urlopen(req, timeout=60, context=_TLS) as r:
        return r.read(limit + 1)


def cli_github_game(repo, price=None, title=None):
    """Добавить (или обновить) игру в каталоге из открытого репозитория GitHub:
    описание и скриншоты — из репозитория, файл — из последнего релиза.
    Без цены игра бесплатная: кнопка ведёт на скачивание с GitHub. С ценой — файл копируется на сервер и продаётся."""
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        sys.exit('Укажите репозиторий как владелец/имя, например Anonymouslol411/otklik')
    api = f'https://api.github.com/repos/{repo}'
    try:
        info = json.loads(gh_get(api))
        rel = json.loads(gh_get(api + '/releases/latest'))
        tree = json.loads(gh_get(f"{api}/git/trees/{info['default_branch']}?recursive=1"))
    except urllib.error.HTTPError as e:
        sys.exit(f'GitHub ответил {e.code}: репозиторий или релиз не найден (репозиторий должен быть открытым)')
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f'Нет связи с GitHub: {e}')
    assets = [a for a in rel.get('assets', []) if Path(a['name']).suffix.lower() in DOWNLOAD_EXT]
    if not assets:
        sys.exit('В последнем релизе нет файлов .exe / .zip / .apk')
    main = max(assets, key=lambda a: a['size'])          # основной файл игры, а не маленький загрузчик
    title = (title or info.get('name') or repo.split('/')[1]).strip()[:60]
    desc = (info.get('description') or '').strip()[:300]
    con = db_connect()
    row = con.execute('SELECT * FROM games WHERE title=?', (title,)).fetchone()
    gid = row['id'] if row else con.execute(
        "INSERT INTO games (title, description, status, hue, sort, created_at) VALUES (?, ?, 'soon', 20, 50, ?)",
        (title, desc, now())).lastrowid
    con.commit()
    if price:
        if main['size'] > UPLOAD_LIMIT * 5:
            sys.exit('Файл слишком большой')
        name = secure_filename(main['name'])
        print(f'Скачиваю {main["name"]} ({main["size"] / 1048576:.1f} МБ)…')
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        data = gh_get(main['browser_download_url'], limit=main['size'] + 1024, accept='application/octet-stream')
        (DOWNLOADS_DIR / name).write_bytes(data)
        status, url, file = 'paid', info.get('html_url'), name
    else:
        status, url, file = 'live', main['browser_download_url'], None
    # скриншоты из папки screenshots/ (или любые картинки репозитория): обложка — banner, если есть
    imgs = [x['path'] for x in tree.get('tree', []) if x['type'] == 'blob' and x.get('size', 0) <= COVER_LIMIT
            and re.search(r'\.(jpe?g|png|webp)$', x['path'], re.I)]
    imgs.sort(key=lambda p: (not p.lower().startswith('screenshots/'), 'banner' not in p.lower(), p))
    raw = f"https://raw.githubusercontent.com/{repo}/{info['default_branch']}/"
    saved = []
    for path in imgs[:9]:
        try:
            name = save_image_bytes(gh_get(raw + urllib.parse.quote(path), limit=COVER_LIMIT), gid)
        except (urllib.error.URLError, TimeoutError):
            name = None
        if name:
            saved.append((path, name))
    cover = next((n for p, n in saved if 'banner' in p.lower()), saved[0][1] if saved else None)
    shots = [n for p, n in saved if n != cover][:8]
    old = con.execute('SELECT cover, shots FROM games WHERE id=?', (gid,)).fetchone()
    for n in [old['cover'], *json.loads(old['shots'] or '[]')]:
        if n and n != cover and n not in shots:
            drop_cover(n)
    con.execute('UPDATE games SET title=?, description=?, status=?, url=?, price=?, file=?, cover=?, shots=? WHERE id=?',
                (title, desc, status, url, float(price) if price else None, file, cover, json.dumps(shots), gid))
    con.commit()
    print(f'Готово: «{title}» — {"продаётся за " + fmt_rub(float(price)) if price else "бесплатно, скачивание с GitHub"}, '
          f'скриншотов: {len(shots)}{", есть обложка" if cover else ""}. Страница: {BASE_URL}/games/{gid}')


def cli_add_file(path):
    src = Path(path)
    if not src.is_file() or src.suffix.lower() not in DOWNLOAD_EXT:
        sys.exit(f'Нужен файл {", ".join(sorted(DOWNLOAD_EXT))}: {path}')
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, DOWNLOADS_DIR / secure_filename(src.name))
    print('Добавлен файл для скачивания:', secure_filename(src.name))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'dev'
    if cmd == 'admin-password':
        cli_admin_password()
    elif cmd == 'stats':
        cli_stats()
    elif cmd == 'worker':
        run_worker()
    elif cmd == 'yoomoney-auth':
        cli_yoomoney_auth()
    elif cmd == 'yoomoney-check':
        cli_yoomoney_check()
    elif cmd == 'mail-test' and len(sys.argv) > 2:
        if not SMTP_HOST:
            sys.exit('Почта не настроена: sudo bash manage.sh mail')
        with app.test_request_context():
            ok = send_mail(sys.argv[2], f'{SITE_NAME}: проверка почты', 'Если вы читаете это письмо — почта сайта настроена правильно.')
        print('Письмо отправлено — проверьте ящик (и «Спам»).' if ok else 'Не удалось отправить — ошибка выше.')
        sys.exit(0 if ok else 1)
    elif cmd == 'github-game' and len(sys.argv) > 2:
        args = sys.argv[2:]
        price = next((a for a in args[1:] if re.fullmatch(r'\d+(\.\d+)?', a)), None)
        title = next((a for a in args[1:] if a != price), None)
        with app.test_request_context():
            cli_github_game(args[0], price, title)
    elif cmd == 'add-file' and len(sys.argv) > 2:
        cli_add_file(sys.argv[2])
    elif cmd == 'dev':
        app.run(host='127.0.0.1', port=int(env('PORT') or 8000))
    else:
        sys.exit(__doc__)
