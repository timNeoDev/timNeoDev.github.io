"""Проверка разбора ленты, поиска ключевых слов и базы. Запуск: python test_radar.py"""
import os
import tempfile

import db
import feeds

# --- поиск ключевых слов: с начала слова, без учёта регистра и ё ---
kw = ["бот", "лендинг", "парс", "telegram"]
assert feeds.match("Нужен Telegram-бот для записи", kw) == ["бот", "telegram"]
assert feeds.match("Работа с документами", kw) == []          # «бот» внутри «работа» не считается
assert feeds.match("Сделать ЛЕНДИНГ и парсер", kw) == ["лендинг", "парс"]
assert feeds.clean_keyword("  Телеграм   Бот ") == "телеграм бот"

# --- разбор RSS ---
SAMPLE = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<item><title><![CDATA[Бот для магазина]]></title><link>https://www.fl.ru/projects/1/a.html</link>
<description><![CDATA[Нужен <b>бот</b> на aiogram]]></description><category>Программирование / Боты</category>
<pubDate>Thu, 19 Nov 2026 15:38:16 GMT</pubDate></item>
<item><title>Без ссылки</title></item>
</channel></rss>"""
orders = feeds.parse_rss(SAMPLE, "FL.ru")
assert len(orders) == 1                                         # заказ без ссылки пропущен
o = orders[0]
assert o["description"] == "Нужен бот на aiogram"               # HTML-теги убраны
assert o["published_at"].startswith("2026-11-19T15:38:16")

# --- база: дубликаты, статусы, ключевые слова ---
db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
db.init_db()
assert db.is_empty() and "бот" in db.get_keywords()
order_id = db.add_order(o, ["бот"])
assert order_id and db.add_order(o, ["бот"]) is None             # повтор той же ссылки не сохраняется
assert [r["id"] for r in db.list_orders("new")] == [order_id]
db.set_status(order_id, "applied")
assert db.list_orders("new") == [] and db.stats()["applied"] == 1
try:
    db.set_status(order_id, "deleted")
    raise AssertionError("неизвестный статус должен отклоняться")
except ValueError:
    pass
db.add_keyword("парсер"); db.delete_keyword("бот")
assert "парсер" in db.get_keywords() and "бот" not in db.get_keywords()

print("all OK")
