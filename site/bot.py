"""Платёжный Telegram-бот сайта «Кольца и Орбиты».

Пополняет баланс аккаунта звёздами Telegram (валюта XTR): покупатель покупает звёзды в Telegram
за рубли и платит ими в боте, а подписку потом оплачивает балансом на сайте.
Счета на покупки, выставленные раньше, по-прежнему засчитываются.
Проверка оплаты — на сервере: деньги засчитываются только по событию successful_payment,
которое приходит от Telegram по защищённому соединению; сумма, валюта и заказ сверяются с базой.

Запуск: python bot.py   (в Docker это отдельный сервис bot)
Настройки (.env): TG_BOT_TOKEN, TG_STAR_RUB, TG_TOPUP_PACKS, TG_ADMIN_ID.
"""
import json
import logging
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request

import app as site
from app import (BASE_URL, PRODUCTS, SITE_NAME, SUPPORT_EMAIL, TG_ADMIN_ID, TG_BOT_TOKEN, TG_STAR_RUB,
                 TG_TOPUP_PACKS, change_balance, complete_order, db_connect, fmt_date, fmt_rub, new_order, note_attempt,
                 now, set_setting, stars_price, too_many)

API = (site.env('TG_API_BASE') or 'https://api.telegram.org').rstrip('/')
log = logging.getLogger('bot')
_TLS = ssl.create_default_context()
_TLS.minimum_version = ssl.TLSVersion.TLSv1_2

TITLES = {'month': 'Месяц онлайн', 'forever': 'Навсегда'}
DESCR = {'month': '30 дней игры в «Кольца» и «Орбиты» в браузере. Без автопродления.',
         'forever': 'Игра онлайн без срока и скачивание exe/apk.'}


class TgError(Exception):
    pass


def api(method, **params):
    """Запрос к Bot API по HTTPS. Токен не попадает в логи."""
    req = urllib.request.Request(f'{API}/bot{TG_BOT_TOKEN}/{method}', data=json.dumps(params).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=int(params.get('timeout') or 0) + 20,
                                    context=_TLS if API.startswith('https') else None) as r:
            res = json.loads(r.read(5_000_000).decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read(100_000).decode('utf-8'))
        except Exception:
            raise TgError(f'HTTP {e.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TgError(f'нет связи с Telegram: {getattr(e, "reason", e)}') from None
    if not res.get('ok'):
        raise TgError(res.get('description') or 'ошибка Telegram')
    return res['result']


def send(chat_id, text, buttons=None):
    params = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    if buttons:
        params['reply_markup'] = {'inline_keyboard': buttons}
    try:
        return api('sendMessage', **params)
    except TgError as e:
        # Telegram может не принять кнопку-ссылку на голый IP — тогда шлём без ссылок
        if buttons and 'url' in json.dumps(buttons) and 'BUTTON' in str(e).upper():
            plain = [[b for b in row if 'url' not in b] for row in buttons]
            params['reply_markup'] = {'inline_keyboard': [r for r in plain if r]}
            return api('sendMessage', **params)
        raise


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def mask_email(e):
    name, _, dom = e.partition('@')
    return f'{name[:2]}***@{dom}'


def user_by_tg(con, tg_id):
    return con.execute('SELECT * FROM users WHERE tg_id=?', (tg_id,)).fetchone()


def site_button():
    return [{'text': '🌐 Сайт', 'url': BASE_URL}] if BASE_URL.startswith('http') else []


def notify_admin(text):
    if TG_ADMIN_ID:
        try:
            send(TG_ADMIN_ID, text)
        except TgError as e:
            log.warning('Не удалось уведомить администратора: %s', e)


# ---------------------------------------------------------------- меню
def show_menu(con, chat_id, u):
    if not u:
        send(chat_id, f'🎵 <b>{esc(SITE_NAME)}</b>\n\nЧтобы платить в боте, привяжите аккаунт сайта: войдите на сайте → '
                      '«Аккаунт» → «Привязать Telegram». Ссылка вернёт вас сюда.', [site_button()])
        return
    if u['lifetime']:
        access = '♾ Навсегда'
    elif (u['paid_until'] or 0) > now():
        access = f'🗓 Месяц онлайн до {fmt_date(u["paid_until"])}'
    elif (u['trial_end'] or 0) > now():
        access = f'🎁 Пробный период до {fmt_date(u["trial_end"], True)}'
    else:
        access = 'нет'
    if u['banned']:
        access = '⛔ аккаунт заблокирован'
    rows = []
    if not u['banned']:
        rows += [[{'text': f'{n} ⭐ → {fmt_rub(n * TG_STAR_RUB)}', 'callback_data': f'top:{n}'}] for n in TG_TOPUP_PACKS]
    rows.append([{'text': '🔄 Обновить', 'callback_data': 'menu'}] + site_button())
    send(chat_id, f'🎵 <b>{esc(SITE_NAME)}</b>\n\nАккаунт: {esc(mask_email(u["email"]))}\nДоступ: {access}\n'
                  f'Баланс: {fmt_rub(u["balance"])}\n\nЗдесь можно пополнить баланс звёздами Telegram ⭐ '
                  f'(1 ⭐ = {fmt_rub(TG_STAR_RUB)}). Подписку оплачивайте балансом на сайте в разделе «Цены».',
         rows)


def show_topup(chat_id):
    rows = [[{'text': f'{n} ⭐ → {fmt_rub(n * TG_STAR_RUB)}', 'callback_data': f'top:{n}'}] for n in TG_TOPUP_PACKS]
    rows.append([{'text': '← Назад', 'callback_data': 'menu'}])
    send(chat_id, f'Пополнение баланса. 1 ⭐ = {fmt_rub(TG_STAR_RUB)}.\nБалансом можно оплатить покупки на сайте.', rows)


# ---------------------------------------------------------------- счета
def send_invoice(chat_id, title, description, payload, stars):
    api('sendInvoice', chat_id=chat_id, title=title[:32], description=description[:255], payload=payload,
        currency='XTR', prices=[{'label': title[:32], 'amount': stars}])


def invoice_topup(con, chat_id, u, stars):
    if stars not in TG_TOPUP_PACKS or u['banned']:
        return
    send_invoice(chat_id, 'Пополнение баланса', f'{stars} ⭐ → {fmt_rub(stars * TG_STAR_RUB)} на баланс аккаунта.',
                 f't:{u["id"]}:{stars}', stars)


def check_payload(con, payload, tg_id, currency, amount):
    """Сверить счёт с базой. Возвращает (ошибка, данные)."""
    if currency != 'XTR':
        return 'Неверная валюта', None
    m = re.fullmatch(r'o:([0-9a-f]{16})', payload or '')
    if m:
        order = con.execute('SELECT * FROM orders WHERE id=?', (m.group(1),)).fetchone()
        if not order or order['method'] != 'telegram':
            return 'Счёт не найден — откройте меню и выберите покупку заново', None
        u = con.execute('SELECT * FROM users WHERE id=?', (order['user_id'],)).fetchone()
        if not u or u['banned'] or u['tg_id'] != tg_id:
            return 'Аккаунт недоступен для оплаты', None
        if amount != stars_price(order['amount']):
            return 'Цена изменилась — откройте меню и выберите покупку заново', None
        return None, {'kind': 'order', 'order': order, 'user': u, 'rub': order['amount']}
    m = re.fullmatch(r't:(\d+):(\d+)', payload or '')
    if m:
        u = con.execute('SELECT * FROM users WHERE id=?', (int(m.group(1)),)).fetchone()
        stars = int(m.group(2))
        if not u or u['banned'] or u['tg_id'] != tg_id:
            return 'Аккаунт недоступен для оплаты', None
        if amount != stars or stars <= 0:
            return 'Неверная сумма', None
        return None, {'kind': 'topup', 'user': u, 'rub': round(stars * TG_STAR_RUB, 2)}
    return 'Неизвестный счёт', None


def on_pre_checkout(con, q):
    err, data = check_payload(con, q.get('invoice_payload'), q['from']['id'], q.get('currency'), q.get('total_amount'))
    if not err and data['kind'] == 'order':
        if data['order']['status'] not in ('pending',):
            err = 'Этот счёт уже оплачен'
        elif data['order']['product'] == 'forever' and data['user']['lifetime']:
            err = 'У вас уже есть «Навсегда»'
    if err:
        api('answerPreCheckoutQuery', pre_checkout_query_id=q['id'], ok=False, error_message=err)
    else:
        api('answerPreCheckoutQuery', pre_checkout_query_id=q['id'], ok=True)


def on_paid(con, msg):
    sp = msg['successful_payment']
    tg_id = msg['from']['id']
    charge = sp.get('telegram_payment_charge_id') or ''
    stars = int(sp.get('total_amount') or 0)
    err, data = check_payload(con, sp.get('invoice_payload'), tg_id, sp.get('currency'), stars)
    if err:
        # деньги уже списаны, но счёт не сходится (например, аккаунт заблокировали) — разбирается администратор
        log.warning('Оплата %s ⭐ (%s) не засчитана: %s', stars, charge, err)
        notify_admin(f'⚠️ Оплата {stars} ⭐ от Telegram ID {tg_id} не засчитана: {esc(err)}\n'
                     f'Возврат: /refund {esc(charge)}')
        send(msg['chat']['id'], 'Оплата получена, но засчитать её автоматически не вышло. Администратор разберётся, '
                                'при необходимости звёзды вернутся. Вопросы: /paysupport')
        return
    u, rub = data['user'], data['rub']
    try:
        con.execute('INSERT INTO tg_payments (charge_id, user_id, tg_id, stars, kind, rub, order_id, created_at) '
                    'VALUES (?,?,?,?,?,?,?,?)', (charge, u['id'], tg_id, stars, data['kind'], rub,
                                                 data['order']['id'] if data['kind'] == 'order' else None, now()))
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback()
        if con.execute('SELECT applied FROM tg_payments WHERE charge_id=?', (charge,)).fetchone()['applied']:
            log.info('Повтор уведомления об оплате %s — уже засчитано', charge)
            return
    if data['kind'] == 'order':
        order = data['order']
        if complete_order(con, order, operation_id=f'tg:{charge}'):
            text = f'✅ Оплачено: «{TITLES[order["product"]]}». Доступ открыт — приятной игры!'
        elif con.execute('SELECT operation_id FROM orders WHERE id=?', (order['id'],)).fetchone()['operation_id'] == f'tg:{charge}':
            text = f'✅ Оплачено: «{TITLES[order["product"]]}». Доступ открыт — приятной игры!'
        else:   # счёт оплатили повторно — не теряем деньги, кладём на баланс
            change_balance(con, u['id'], rub, f'Telegram: повторная оплата {stars} ⭐')
            text = f'Этот счёт уже был оплачен, поэтому {fmt_rub(rub)} зачислены на баланс.'
        what = TITLES[order['product']]
    else:
        change_balance(con, u['id'], rub, f'Telegram: {stars} ⭐')
        text = f'✅ Баланс пополнен на {fmt_rub(rub)}.'
        what = 'пополнение баланса'
    con.execute('UPDATE tg_payments SET applied=1 WHERE charge_id=?', (charge,))
    con.commit()
    log.info('Оплата Telegram: %s ⭐, %s, user %s', stars, what, u['id'])
    send(msg['chat']['id'], text, [[{'text': 'Меню', 'callback_data': 'menu'}]])
    notify_admin(f'💰 {stars} ⭐ ({fmt_rub(rub)}): {esc(what)}\nАккаунт: {esc(u["email"])}\nВозврат: /refund {esc(charge)}')


def refund(con, chat_id, charge):
    p = con.execute('SELECT * FROM tg_payments WHERE charge_id=?', (charge,)).fetchone()
    if not p:
        send(chat_id, 'Платёж не найден.')
        return
    if p['refunded']:
        send(chat_id, 'Этот платёж уже возвращён.')
        return
    try:
        api('refundStarPayment', user_id=p['tg_id'], telegram_payment_charge_id=charge)
    except TgError as e:
        send(chat_id, f'Telegram не вернул звёзды: {esc(e)}')
        return
    con.execute('UPDATE tg_payments SET refunded=1 WHERE charge_id=?', (charge,))
    con.commit()
    if p['kind'] == 'topup':
        change_balance(con, p['user_id'], -p['rub'], f'Возврат {p["stars"]} ⭐ в Telegram')
    elif p['order_id']:
        order = con.execute('SELECT * FROM orders WHERE id=?', (p['order_id'],)).fetchone()
        con.execute("UPDATE orders SET status='rejected' WHERE id=?", (p['order_id'],))
        if order and order['product'] == 'forever':
            con.execute('UPDATE users SET lifetime=0 WHERE id=?', (p['user_id'],))
        elif order:
            con.execute('UPDATE users SET paid_until=MAX(?, COALESCE(paid_until,0)-?) WHERE id=?',
                        (now(), PRODUCTS['month']['days'] * 86400, p['user_id']))
        con.commit()
    con.execute("INSERT INTO admin_log (action, details, ip, created_at) VALUES ('Возврат звёзд', ?, 'telegram', ?)",
                (f'{p["stars"]} ⭐, user {p["user_id"]}', now()))
    con.commit()
    send(chat_id, f'Возвращено {p["stars"]} ⭐, покупка отменена.')
    try:
        send(p['tg_id'], f'↩️ Вам вернули {p["stars"]} ⭐, покупка отменена.')
    except TgError:
        pass


# ---------------------------------------------------------------- обработка сообщений
def link_account(con, chat_id, tg_id, code):
    row = con.execute('SELECT * FROM tg_links WHERE code=? AND expires>?', (code, now())).fetchone()
    con.execute('DELETE FROM tg_links WHERE code=?', (code,))
    con.commit()
    if not row:
        send(chat_id, 'Ссылка устарела. Нажмите «Привязать Telegram» на сайте ещё раз.', [site_button()])
        return None, None
    u = con.execute('SELECT * FROM users WHERE id=?', (row['user_id'],)).fetchone()
    if not u or u['banned']:
        send(chat_id, 'Этот аккаунт нельзя привязать.')
        return None, None
    con.execute('UPDATE users SET tg_id=NULL WHERE tg_id=? AND id!=?', (tg_id, u['id']))   # один Telegram — один аккаунт
    con.execute('UPDATE users SET tg_id=? WHERE id=?', (tg_id, u['id']))
    con.commit()
    send(chat_id, f'🔗 Telegram привязан к аккаунту {esc(mask_email(u["email"]))}.')
    return con.execute('SELECT * FROM users WHERE id=?', (u['id'],)).fetchone(), row['product']


def on_message(con, msg):
    if msg.get('chat', {}).get('type') != 'private':
        return
    if 'successful_payment' in msg:
        on_paid(con, msg)
        return
    chat_id, tg_id = msg['chat']['id'], msg['from']['id']
    text = (msg.get('text') or '').strip()
    if too_many(con, f'tgbot|{tg_id}', 30, 60):
        return
    note_attempt(con, f'tgbot|{tg_id}')
    cmd, _, arg = text.partition(' ')
    cmd = cmd.split('@')[0].lower()
    if cmd == '/start' and re.fullmatch(r'[A-Za-z0-9_\-]{10,64}', arg.strip()):
        u, _ = link_account(con, chat_id, tg_id, arg.strip())
        if u:
            show_menu(con, chat_id, u)
        return
    if cmd == '/paysupport':
        contact = f'на почту {esc(SUPPORT_EMAIL)}' if SUPPORT_EMAIL else 'администратору сайта'
        send(chat_id, f'Вопросы по оплате и возвратам — напишите {contact}. Укажите почту аккаунта и дату оплаты.')
        return
    if cmd == '/terms':
        send(chat_id, f'Условия: {esc(BASE_URL)}/terms\nПокупки — цифровой доступ к играм сайта, без автопродления. '
                      'Возврат возможен, если доступ не был использован: /paysupport')
        return
    if cmd == '/refund' and TG_ADMIN_ID and tg_id == TG_ADMIN_ID and arg.strip():
        refund(con, chat_id, arg.strip())
        return
    show_menu(con, chat_id, user_by_tg(con, tg_id))


def on_callback(con, cq):
    try:
        api('answerCallbackQuery', callback_query_id=cq['id'])
    except TgError:
        pass
    msg = cq.get('message') or {}
    if msg.get('chat', {}).get('type') != 'private':
        return
    chat_id, tg_id, data = msg['chat']['id'], cq['from']['id'], cq.get('data') or ''
    if too_many(con, f'tgbot|{tg_id}', 30, 60):
        return
    note_attempt(con, f'tgbot|{tg_id}')
    u = user_by_tg(con, tg_id)
    if not u or data == 'menu':
        show_menu(con, chat_id, u)
    elif data == 'topup' or data.startswith('buy:'):      # покупки в боте убраны — только пополнение
        show_topup(chat_id)
    elif re.fullmatch(r'top:\d{1,6}', data):
        invoice_topup(con, chat_id, u, int(data[4:]))


def handle(update):
    """Обработать обновление. False — сбой базы при оплате: обновление нужно повторить."""
    con = db_connect()
    try:
        if 'pre_checkout_query' in update:
            on_pre_checkout(con, update['pre_checkout_query'])
        elif 'message' in update:
            on_message(con, update['message'])
        elif 'callback_query' in update:
            on_callback(con, update['callback_query'])
    except TgError as e:
        log.warning('Telegram: %s', e)
    except Exception:
        log.exception('Ошибка обработки обновления')
        return 'successful_payment' not in (update.get('message') or {})
    finally:
        con.close()
    return True


def prepare():
    con = db_connect()
    me = api('getMe')
    set_setting(con, 'tg_bot_username', me['username'])
    con.close()
    api('deleteWebhook', drop_pending_updates=False)
    api('setMyCommands', commands=[{'command': 'start', 'description': 'Меню и оплата'},
                                   {'command': 'paysupport', 'description': 'Помощь с оплатой'},
                                   {'command': 'terms', 'description': 'Условия'}])
    log.info('Бот @%s запущен', me['username'])


def cleanup():
    """Удалить неоплаченные счета бота старше суток (чтобы не засоряли список заказов)."""
    con = db_connect()
    con.execute("DELETE FROM orders WHERE method='telegram' AND status='pending' AND created_at<?", (now() - 86400,))
    con.execute('DELETE FROM tg_links WHERE expires<?', (now(),))
    con.commit()
    con.close()


def main():
    if not TG_BOT_TOKEN:
        log.info('TG_BOT_TOKEN не задан — бот выключен (sudo bash manage.sh telegram)')
        con = db_connect()
        con.execute("DELETE FROM settings WHERE key='tg_bot_username'")
        con.commit()
        con.close()
        while True:
            time.sleep(3600)
    while True:
        try:
            prepare()
            break
        except TgError as e:
            log.error('Не удалось запустить бота: %s. Повтор через 30 с', e)
            time.sleep(30)
    offset, last_cleanup = 0, 0
    while True:
        if time.time() - last_cleanup > 3600:
            cleanup()
            last_cleanup = time.time()
        try:
            updates = api('getUpdates', offset=offset, timeout=50,
                          allowed_updates=['message', 'callback_query', 'pre_checkout_query'])
        except TgError as e:
            log.warning('getUpdates: %s', e)
            time.sleep(5)
            continue
        for upd in updates:
            for attempt in range(5):      # оплату нельзя потерять: при сбое повторяем
                if handle(upd):
                    break
                time.sleep(3)
            else:
                notify_admin(f'⚠️ Не удалось засчитать оплату (update {upd["update_id"]}), смотрите логи бота.')
            offset = max(offset, upd['update_id'] + 1)


if __name__ == '__main__':
    main()
