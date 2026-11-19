"""Проверка сбора, фильтров, CRM, аналитики и всех страниц админки. Запуск: python test_radar.py"""
import asyncio
import os
import tempfile
from datetime import date, timedelta

os.environ["ADMIN_PASSWORD"] = "test-password"  # до импорта config

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
from datetime import datetime  # noqa: E402
pub = datetime.fromisoformat(o["published_at"])
assert sources.is_hot(o, pub + timedelta(minutes=30))
assert not sources.is_hot(o, pub + timedelta(minutes=90))           # старше часа
assert not sources.is_hot({**o, "offers": 10}, pub)                 # слишком много откликов

# --- фильтры: ключевые слова, минус-слова, бюджет ---
assert filter_order(o, ["бот"], [], 0) == ["бот"]
assert filter_order(o, ["бот"], ["запис"], 0) == []                # минус-слово отбрасывает
assert filter_order(o, ["бот"], [], 10000) == []                   # бюджет ниже минимума

# --- база ---
db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
db.init_db()
assert db.is_empty() and "бот" in db.get_words("plus") and "дизайн" in db.get_words("minus")
oid = db.upsert_order(o, ["бот"])
assert oid and db.upsert_order({**o, "views": 99, "offers": 5}, ["бот"]) is None
assert db.get_order(oid)["views"] == 99 and db.get_order(oid)["offers"] == 5   # повтор обновляет статистику
assert [r["id"] for r in db.list_orders("new")] == [oid]
assert db.list_orders("new", q="записи") and not db.list_orders("new", q="нет такого")
try:
    db.set_order_status(oid, "deleted")
    raise AssertionError("неизвестный статус должен отклоняться")
except ValueError:
    pass

# --- CRM: проект из заказа, оплаты, дедлайны, аналитика ---
pid = db.project_from_order(oid)
assert db.project_from_order(oid) == pid                            # повторно не дублируется
assert db.get_order(oid)["status"] == "applied"
p = db.get_project(pid)
assert p["price"] == 5000 and p["stage"] == "lead" and p["paid"] == 0
db.save_project({**dict(p), "stage": "work", "deadline": (date.today() + timedelta(days=1)).isoformat()}, pid)
assert [x["id"] for x in db.due_projects(2)] == [pid]
db.add_payment(pid, 2500, date.today().isoformat(), "предоплата")
assert db.get_project(pid)["paid"] == 2500
try:
    db.add_payment(pid, 0, date.today().isoformat())
    raise AssertionError("нулевая оплата должна отклоняться")
except ValueError:
    pass
s = db.overview()
assert s["income_month"] == 2500 and s["receivable"] == 2500 and s["conversion"] == 100
assert db.income_by_month(6)[-1] == (date.today().strftime("%Y-%m"), 2500)
assert db.found_by_day(14)[-1][1] == 1
assert db.funnel()[0] == ("Найдено", 1)

# --- шаблоны ---
n = len(db.list_templates())
db.save_template("Тест", "Текст")
assert len(db.list_templates()) == n + 1

# --- бот: тексты сообщений собираются без ошибок ---
import bot  # noqa: E402
assert "Бот для записи" in bot.format_order(db.get_order(oid))
assert "Дедлайны" in bot.reminder_text() and "Сводка" in bot.digest_text()


# --- админка: вход и каждая страница отдаёт 200 ---
async def check_web():
    from aiohttp.test_utils import TestClient, TestServer
    from web import create_app
    async with TestClient(TestServer(create_app())) as client:
        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 302 and resp.headers["Location"] == "/login"          # без входа — на логин
        resp = await client.post("/login", data={"password": "wrong"}, allow_redirects=False)
        assert resp.status == 200                                                   # неверный пароль
        resp = await client.post("/login", data={"password": "test-password"}, allow_redirects=False)
        assert resp.status == 302
        for path in ("/", "/orders", "/orders?view=all&q=бот&budget=1000&hot=1", "/orders?view=applied",
                     "/projects", "/projects?stage=work", f"/projects/{pid}", "/projects/new",
                     "/finance", "/templates", "/settings", "/static/style.css"):
            resp = await client.get(path)
            assert resp.status == 200, (path, resp.status, (await resp.text())[:300])
        resp = await client.post("/settings/words", data={"kind": "minus", "words": "Логотип, баннер"})
        assert "баннер" in db.get_words("minus")
        resp = await client.post(f"/projects/{pid}/stage", data={"stage": "paid"})
        assert db.get_project(pid)["stage"] == "paid"
        page = await (await client.get("/orders?view=applied")).text()
        assert "&lt;b&gt;" not in page or "<b>бот</b>" not in page               # текст заказа экранирован

asyncio.run(check_web())
print("all OK")
