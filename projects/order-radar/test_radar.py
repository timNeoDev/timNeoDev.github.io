"""Проверка сбора, фильтров, сделок, финансов, аналитики и всех страниц админки. Запуск: python test_radar.py"""
import asyncio
import os
import tempfile
from datetime import date, datetime, timedelta

os.environ["ADMIN_PASSWORD"] = "тест-пароль"  # до импорта config; русские буквы — проверка исправления входа

import db  # noqa: E402
import sources  # noqa: E402
from sources import filter_order  # noqa: E402

# --- поиск слов: с начала слова, без учёта регистра и ё ---
assert sources.match("Нужен Telegram-бот для записи", ["бот", "telegram"]) == ["бот", "telegram"]
assert sources.match("Работа с документами", ["бот"]) == []      # «бот» внутри «работа» не считается
assert sources.clean_word("  Телеграм   Бот ") == "телеграм бот"

# --- разбор страницы Kwork (структура как на kwork.ru/projects) ---
HTML = ('<script>window.x={"cats":[{"CATID":"41","id":"41","name":"Скрипты, боты и mini apps"}],'
        '"wants":[{"id":"777","name":"Бот для записи","description":"Нужен <b>бот</b> [срочно] &mdash; сегодня",'
        '"priceLimit":"5000.00","possiblePriceLimit":15000,"views_dirty":"42","kwork_count":2,'
        '"date_create":"2025-01-01 10:00:00","date_active":"2026-11-19 21:42:40","category_id":"41"},'
        '{"id":"bad"}]}</script>')
orders = sources.parse_kwork(HTML)
assert len(orders) == 1                                            # битый заказ пропущен, а не роняет разбор
o = orders[0]
assert o["link"] == "https://kwork.ru/projects/777" and o["category"] == "Скрипты, боты и mini apps"
assert (o["budget"], o["budget_max"], o["views"], o["offers"]) == (5000, 15000, 42, 2)
assert o["description"] == "Нужен бот [срочно] — сегодня"          # теги и &mdash; убраны, скобки не ломают JSON
assert o["published_at"] == "2026-11-19T21:42:40"                  # дата последней публикации, а не создания

# --- «горячий» заказ ---
pub = datetime.fromisoformat(o["published_at"])
assert sources.is_hot(o, pub + timedelta(minutes=30))
assert not sources.is_hot(o, pub + timedelta(minutes=90))           # старше часа
assert not sources.is_hot({**o, "offers": 10}, pub)                 # слишком много откликов

# --- фильтры ---
assert filter_order(o, ["бот"], [], 0) == ["бот"]
assert filter_order(o, ["бот"], ["запис"], 0) == []                # минус-слово отбрасывает
assert filter_order(o, ["бот"], [], 10000) == []                   # бюджет ниже минимума
# случаи из реальных данных Kwork:
verstka = {**o, "title": "Верстка страниц сайта на WordPress", "description": "Сверстать по готовому дизайну",
           "category": "Создание сайта"}
assert filter_order(verstka, ["сайт"], ["дизайн"], 0) == ["сайт"]   # минус-слово только в описании — не мешает
excel = {**o, "title": "Макросы в Excel", "description": "Автоматизировать отчёт", "category": "Скрипты, боты и mini apps"}
assert filter_order(excel, ["бот", "mini app"], [], 0) == []        # совпадение только по категории не считается
tg_post = {**o, "source": "TG @chan", "title": "👔 Маркетолог", "description": "#вакансия, нужен опыт с ботами"}
assert filter_order(tg_post, ["бот"], ["вакансия"], 0) == []        # в Telegram минус-слово ищется по всему тексту

# --- база: заказы ---
db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
db.init_db()
db.init_db()                                                        # повторный запуск не ломает базу
assert "бот" in db.get_words("plus") and "дизайн" in db.get_words("minus")
assert {a["exchange"]: a["enabled"] for a in db.list_accounts()} == {"Kwork": 1, "Profi.ru": 0}
oid = db.upsert_order(o, ["бот"])
assert oid and db.upsert_order({**o, "views": 99, "offers": 5}, ["бот"]) is None
assert db.get_order(oid)["views"] == 99 and db.get_order(oid)["offers"] == 5

# --- аккаунты бирж: цена отклика, подключение, отключение ---
db.set_response_price("Kwork", 50)
db.connect_account("Profi.ru", "+79990000000", "cookie=secret")
assert db.get_account("Profi.ru")["enabled"] == 1 and db.get_account("Profi.ru")["secret"] == "cookie=secret"
db.disconnect_account("Profi.ru")
assert db.get_account("Profi.ru")["secret"] == "" and db.get_account("Profi.ru")["enabled"] == 0

# --- сделка из отклика: путь до оплаты, деньги, окупаемость ---
did = db.deal_from_order(oid)
assert db.deal_from_order(oid) == did                               # повторно не дублируется
d = db.get_deal(did)
assert (d["stage"], d["price"], d["response_cost"], d["via_response"]) == ("sent", 5000, 50, 1)
assert db.get_order(oid)["status"] == "applied"
try:
    db.deal_from_order(99999)
    raise AssertionError("несуществующий заказ должен давать ValueError")
except ValueError:
    pass
s = db.overview()
assert s["potential"] == 5000 and s["refund_possible"] == 50 and s["responses"] == 1
db.save_deal({**dict(d), "stage": "work", "client": "Анна",
              "deadline": (date.today() + timedelta(days=1)).isoformat()}, did)
assert [x["id"] for x in db.due_deals(2)] == [did]
db.add_payment(did, 2500, date.today().isoformat(), "предоплата")
s = db.overview()
assert s["income_month"] == 2500 and s["awaiting"] == 2500 and s["conversion"] == 100
assert s["response_profit"] == 2500 - 50 and s["roi"] == 50.0
assert s["tax_month"] == 100                                         # 4% от 2500
assert s["forecast_month"] == 5000                                   # 2500 получено + 2500 с дедлайном в этом месяце
assert db.clients()[0]["client"] == "Анна" and db.clients()[0]["revenue"] == 2500
assert db.responses_by_source()[0]["profit"] == 2450
try:
    db.add_payment(did, 0, date.today().isoformat())
    raise AssertionError("нулевая оплата должна отклоняться")
except ValueError:
    pass

# --- «пора напомнить»: отклик с наступившей датой напоминания ---
fid = db.save_deal({"title": "Лендинг", "client": "Олег", "stage": "dialog", "price": 9000,
                    "follow_up": date.today().isoformat()})
assert fid in [x["id"] for x in db.follow_ups()] and db.overview()["in_dialog"] == 1
try:
    db.save_deal({"title": "Плохая дата", "stage": "sent", "deadline": "завтра"})
    raise AssertionError("неверная дата должна отклоняться")
except ValueError:
    pass

# --- поиск ---
r = db.search("Анна")
assert r["clients"] and r["deals"]

# --- Telegram: адреса каналов, бюджет, разбор t.me/s, сообщения чатов ---
for raw in ("@digitaltender", "digitaltender", "https://t.me/digitaltender", "t.me/s/digitaltender/"):
    assert sources.tg_username(raw) == "digitaltender", raw
assert sources.tg_username("https://t.me/+AbCdEf") == "" and sources.tg_username("кот") == ""
assert sources.extract_budget("Бюджет: 15 000 ₽, срок 3 дня") == 15000
assert sources.extract_budget("оплата 10к, тел +7 900 123-45-67") == 10000          # телефон не бюджет
assert sources.extract_budget("до 20 тыс. руб") == 20000 and sources.extract_budget("без цены") == 0
TG_HTML = ('<div data-post="testchan/10"><div class="tgme_widget_message_text js-message_text" dir="auto">'
           '​Нужен Telegram-бот<br/>Бюджет 5000 руб &amp; срок 2 дня</div>'
           '<span class="tgme_widget_message_views">1.2K</span><time datetime="2026-11-20T12:00:00+00:00"></time></div>'
           '<div data-post="testchan/11"><div class="tgme_widget_message_photo"></div></div>')  # пост без текста
posts = sources.parse_tg_channel(TG_HTML, "testchan")
assert len(posts) == 1
p0 = posts[0]
assert p0["title"] == "Нужен Telegram-бот" and p0["budget"] == 5000 and p0["views"] == 1200
assert p0["link"] == "https://t.me/testchan/10" and p0["source"] == "TG @testchan"
assert "& срок" in p0["description"] and p0["published_at"] == "2026-11-20T15:00:00"   # UTC → Москва
assert sources.looks_like_order("Ищу разработчика бота", ["ищу"]) and not sources.looks_like_order("Всем привет", ["ищу"])


# --- опрос: новый источник не рассылает старые посты; болтовня из чатов отсекается ---
async def check_poll():
    import main
    sent = []

    async def fake_notify(bot, order_id):
        sent.append(db.get_order(order_id)["title"])

    def batch(title, n, chat=False, text=None):
        return (f"TG @{title}", [{**p0, "link": f"https://t.me/{title}/{i}", "source": f"TG @{title}",
                                  "title": f"{title} {i}", "description": text or p0["description"]} for i in n], chat)

    real_collect, real_notify = main.collect, main.tg.notify
    main.tg.notify = fake_notify
    plan = [[batch("newchan", [1, 2])], [batch("newchan", [3])],
            [batch("chatx", [1])], [batch("chatx", [2], chat=True, text="Telegram-бот у меня сломался, лол")]]
    for step in plan:
        async def fake_collect(session, tg_client, step=step, full=True):
            return step, []
        main.collect = fake_collect
        await main.poll_once(None, bot=object())
    main.collect, main.tg.notify = real_collect, real_notify   # вернуть настоящие функции для следующих проверок
    assert sent == ["newchan 3"], sent                       # 1 и 2 — первый проход, 3 — новый пост
    assert db.get_order(db.list_orders("all", q="лол")[0]["id"])["matched"] == ""   # болтовня не заказ

asyncio.run(check_poll())


# --- быстрая проверка: первая страница Kwork; ошибка источника — пауза ---
async def check_fast_poll():
    import main
    pages_asked = []

    async def fake_kwork(session, pages):
        pages_asked.append(pages)
        return []                                         # «нет данных» — источник уходит на паузу

    real_sources, main.sources.SOURCES = main.sources.SOURCES, [("Kwork", fake_kwork)]
    main._paused_until.clear()
    try:
        await main.collect(None, None, full=False)
        await main.collect(None, None, full=True)        # Kwork на паузе — не запрашивается
        assert pages_asked == [1], pages_asked
        main._paused_until.clear()
        await main.collect(None, None, full=True)
        assert pages_asked == [1, 3], pages_asked         # полная проверка — все страницы
    finally:
        main.sources.SOURCES = real_sources
        main._paused_until.clear()

for t in db.list_tg_sources():
    db.set_tg_source(t["username"], enabled=0)           # в тесте не ходим в сеть за каналами
asyncio.run(check_fast_poll())
assert db.int_setting("poll_seconds") == 30
assert "digitaltender" in [s["username"] for s in db.list_tg_sources()]

# --- смена фильтров перепроверяет «Новые» заказы ---
rid = db.upsert_order({**p0, "link": "https://t.me/x/500", "title": "Монтажёр, пишите в telegram",
                       "description": "Монтажёр, пишите в telegram"}, ["telegram"])
db.add_word("plus", "telegram"); db.rematch_new_orders()
assert db.get_order(rid)["matched"] == "telegram"
db.delete_word("plus", "telegram"); db.rematch_new_orders()
assert db.get_order(rid)["matched"] == ""                                   # мусор ушёл из «Новых»
assert "telegram" not in db.get_words("plus")                               # и не входит в начальные слова

# --- бот: тексты сообщений собираются без ошибок ---
import bot  # noqa: E402
kb_buttons = [b for row in bot.order_kb(db.get_order(oid)).inline_keyboard for b in row]
assert kb_buttons[0].text == "🔗 Перейти к заказу" and kb_buttons[0].url == db.get_order(oid)["link"]
assert "Бот для записи" in bot.format_order(db.get_order(oid)) and "шанс:" in bot.format_order(db.get_order(oid))

# --- шанс получить заказ ---
fresh = {**o, "offers": 2, "budget": 8000, "published_at": "2026-11-20T12:00:00"}
t0 = datetime.fromisoformat("2026-11-20T12:10:00")
assert sources.chance(fresh, t0) == 100 and sources.chance_label(100) == "высокий"     # свежий, мало откликов, бюджет
crowded = {**fresh, "offers": 60, "budget": 500, "published_at": "2026-11-19T12:00:00"}
assert sources.chance(crowded, t0) == 10 and sources.chance_label(10) == "низкий"       # 60 откликов, сутки, 500 ₽
assert sources.chance({**fresh, "published_at": None}, t0) == 100 - 4 - 30 + 10          # без даты — как сутки


# --- Telegram-аккаунт: вход телефон → код → пароль (Telethon подменён) ---
import tg_user  # noqa: E402
from telethon.errors import SessionPasswordNeededError  # noqa: E402

tg_user.SESSION_PATH = os.path.join(tempfile.mkdtemp(), "test_session")  # тест не трогает настоящую сессию


class FakeClient:
    def __init__(self, need_password=False):
        self.need_password, self.calls = need_password, []

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        self.calls.append((code, password))
        if code and self.need_password:
            raise SessionPasswordNeededError(request=None)

    async def get_me(self):
        return type("Me", (), {"username": "radar_parser", "first_name": "Радар"})()

    async def disconnect(self):
        pass

    async def log_out(self):
        self.calls.append("log_out")


async def check_tg_login():
    tg_user._pending.update(client=FakeClient(need_password=True), phone="+79990000000", hash="h")
    assert tg_user.login_step() == "code"
    assert await tg_user.verify_code("12345") is False and tg_user.login_step() == "password"
    await tg_user.verify_password("secret")
    assert tg_user.login_step() == "" and await tg_user.account_name() == "@radar_parser"
    client = tg_user.CLIENT
    await tg_user.logout()
    assert tg_user.CLIENT is None and "log_out" in client.calls       # сессия завершена на серверах Telegram
    try:
        await tg_user.send_code("abc", "hash", "+7999")
        raise AssertionError("нечисловой api_id должен отклоняться")
    except ValueError:
        pass

asyncio.run(check_tg_login())
assert "Пора напомнить" in bot.reminder_text() and "Сводка" in bot.digest_text()

# --- XSS: название заказа с HTML-кодом должно выводиться как текст ---
xss_id = db.upsert_order({**o, "link": "https://kwork.ru/projects/1", "title": "<script>alert(1)</script>"}, ["бот"])


async def check_web():
    from aiohttp.test_utils import TestClient, TestServer
    from web import create_app
    async with TestClient(TestServer(create_app())) as client:
        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 302 and resp.headers["Location"] == "/login"         # без входа — на логин
        resp = await client.post("/login", data={"password": "пароль"}, allow_redirects=False)
        assert resp.status == 200                                                  # неверный русский пароль — не 500
        resp = await client.post("/login", data={"password": "тест-пароль"}, allow_redirects=False)
        assert resp.status == 302                                                  # верный русский пароль пускает
        for path in ("/", "/orders", "/orders?view=all&q=бот&budget=1000&hot=1", "/orders?view=applied",
                     "/deals", "/deals?group=work", "/deals?group=closed&stage=paid", "/deals?client=Анна",
                     f"/deals/{did}", "/deals/new", "/analytics", "/clients", "/search?q=Анна", "/search",
                     "/templates", "/settings", "/static/style.css",
                     *(f"/finance?tab={t}" for t in ("forecast", "payments", "awaiting", "potential", "workload", "closed"))):
            resp = await client.get(path)
            assert resp.status == 200, (path, resp.status, (await resp.text())[:300])
        page = await (await client.get("/orders")).text()
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page
        page = await (await client.get(f"/orders/{oid}")).text()
        assert "Перейти к заказу" in page and "Откликнуться" in page and "Копировать" in page   # страница заказа
        assert (await client.get("/orders/99999")).status == 404
        resp = await client.post(f"/orders/{oid}/respond?go=order", allow_redirects=False)
        assert resp.headers["Location"] == db.get_order(oid)["link"]               # откликнуться — сразу на биржу
        resp = await client.post("/orders/99999/respond", allow_redirects=False)
        assert resp.status == 404                                                  # несуществующий заказ — не 500
        resp = await client.post(f"/orders/{xss_id}/respond", allow_redirects=False)
        assert resp.status == 302 and resp.headers["Location"].startswith("/deals/")
        await client.post("/settings/words", data={"kind": "minus", "words": "Логотип, баннер"})
        assert "баннер" in db.get_words("minus")
        await client.post(f"/deals/{did}/stage", data={"stage": "paid"})
        assert db.get_deal(did)["stage"] == "paid"
        await client.post("/accounts", data={"exchange": "Profi.ru", "action": "enable"})
        assert db.get_account("Profi.ru")["enabled"] == 1
        await client.post("/settings/general", data={"income_goal": "80000", "tax_rate": "6"})
        assert db.overview()["income_goal"] == 80000 and db.overview()["tax_rate"] == 6
        resp = await client.get(f"/projects/{did}", allow_redirects=False)
        assert resp.headers["Location"] == f"/deals/{did}"                       # старые ссылки бота работают
        await client.post("/tg", data={"action": "add", "username": "https://t.me/Some_Chat", "kind": "chat"})
        assert ("Some_Chat", "chat") in [(s["username"], s["kind"]) for s in db.list_tg_sources()]
        resp = await client.post("/tg", data={"action": "add", "username": "не канал"}, allow_redirects=False)
        assert "tg_error" in resp.headers["Location"]
        resp = await client.post("/tg", data={"action": "add", "username": "goodname", "kind": "wrong"})
        assert resp.status == 400
        assert (await client.get("/orders?sort=fresh")).status == 200
        page = await (await client.get("/orders")).text()
        assert "шанс:" in page
        resp = await client.post("/tg/account", data={"action": "send_code", "api_id": "abc", "api_hash": "x",
                                                      "phone": "+7999"}, allow_redirects=False)
        assert "tg_msg=" in resp.headers["Location"]                                  # ошибка показана, не 500
        tg_user._pending.update(client=FakeClient(), phone="+7999", hash="h")
        assert "Код из Telegram" in await (await client.get("/settings")).text()     # шаг ввода кода
        await client.post("/tg/account", data={"action": "code", "code": "11111"})
        assert "подключён: @radar_parser" in await (await client.get("/settings")).text()
        await client.post("/tg/account", data={"action": "logout"})
        assert tg_user.CLIENT is None and "Получить код" in await (await client.get("/settings")).text()
        page = await (await client.get("/settings")).text()
        assert "Один раз: ключи приложения" in page                               # ключей ещё нет — блок раскрыт
        db.set_setting("tg_api_id", "12345"); db.set_setting("tg_api_hash", "abc")
        page = await (await client.get("/settings")).text()
        assert "Ключи приложения сохранены" in page                                # дальше — только телефон и код
        await client.post("/tg", data={"action": "delete", "username": "Some_Chat"})
        assert "Some_Chat" not in [s["username"] for s in db.list_tg_sources()]
        assert (await client.get("/settings")).status == 200

asyncio.run(check_web())
print("all OK")
