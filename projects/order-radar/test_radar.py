"""Проверка сбора, фильтров, сделок, финансов, аналитики и всех страниц админки. Запуск: python test_radar.py"""
import asyncio
import os
import tempfile
from datetime import date, datetime, timedelta

os.environ["ADMIN_PASSWORD"] = "тест-пароль"  # до импорта config; русские буквы — проверка исправления входа

import db  # noqa: E402
import sources  # noqa: E402
from main import filter_order  # noqa: E402

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

# --- бот: тексты сообщений собираются без ошибок ---
import bot  # noqa: E402
assert "Бот для записи" in bot.format_order(db.get_order(oid))
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

asyncio.run(check_web())
print("all OK")
