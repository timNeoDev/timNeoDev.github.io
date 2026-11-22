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

# --- FL.ru: свежая лента fl.ru/projects (RSS кешируется до часа) ---
FL_HTML = ('<div class="x" id="project-item111"><h2><a id="prj_name_111" class="a" href="/projects/111/bot.html">Telegram-бот '
           '&amp; парсер</a></h2><div class="d-flex b-post__price p-0"><span>от 15&nbsp;000&nbsp;₽ – 30 000 ₽</span></div>'
           '<div class="b-post__body"><div class="b-post__txt text-5 ">Нужен бот для записи,...</div></div>'
           '<span class="b-post__bold b-layout__txt_inline-block text-7 text-dark mr-4">Заказ</span>'
           '<span class="text-gray-opacity-4 text-7 mr-16">1 час 5 минут назад</span>'
           '<span title="Количество просмотров" class="d"><svg></svg><span class="text-7">больше 300&#160;</span></span>'
           '<a href="/projects/111/bot.html">12 ответов</a></div>'
           '<div class="x" id="project-item222"><h2><a id="prj_name_222" href="/projects/222/job.html">В штат</a></h2>'
           '<span class="b-post__bold b-layout__txt_inline-block text-7 text-dark mr-4">Вакансия&nbsp;</span></div>'
           '<div class="x" id="project-item333"><h2><a id="prj_name_333" href="/projects/333/site.html">Сайт</a></h2>'
           '<div class="b-post__price"><span>по договоренности</span></div>'
           '<span class="b-post__bold b-layout__txt_inline-block text-7 text-dark mr-4">Заказ</span>'
           '<span class="text-gray-opacity-4 text-7 mr-16">только что</span><a>Нет ответов</a></div>')
fl = sources.parse_fl_list(FL_HTML, datetime(2026, 11, 23, 12, 0))
assert [x["link"] for x in fl] == ["https://www.fl.ru/projects/111/bot.html", "https://www.fl.ru/projects/333/site.html"]
assert fl[0]["title"] == "Telegram-бот & парсер" and (fl[0]["budget"], fl[0]["budget_max"]) == (15000, 30000)
assert (fl[0]["views"], fl[0]["offers"], fl[0]["cut"]) == (300, 12, True)
assert fl[0]["published_at"] == "2026-11-23T10:55:00" and fl[1]["published_at"] == "2026-11-23T12:00:00"
assert (fl[1]["budget"], fl[1]["offers"]) == (0, 0)
assert sources.parse_ago("2 дня назад", datetime(2026, 11, 23)) == datetime(2026, 11, 21)
assert sources.parse_ago("вчера", datetime(2026, 11, 23)) is None

# --- Profi.ru: лента специалиста (структура ответа BoSearchBoardItems) и cookie сессии ---
import base64, json as _json, profi  # noqa: E401,E402
snippet = {"id": "96314683", "type": "SNIPPET", "title": "Создание сайтов",
           "description": "Корпоративный сайт · Контент есть · Нужен сайт для МФО", "lastUpdateDate": 1795373305,
           "price": {"prefix": "до", "value": "45 000 ₽"}, "geo": {"remote": {"prefix": "Дистанционно · Москва"}},
           "badges": [{"label": "Топ-заказ"}]}
feed = {"data": {"boSearchBoardItems": {"items": [
    {"id": "DIVIDER", "type": "DIVIDER"}, snippet,
    {"id": "CAROUSEL", "type": "CAROUSEL", "snippets": [{**snippet, "id": "96207094", "price": None}, snippet]},
    {"id": "STORIES", "type": "STORIES"}]}}}
p_orders = profi.parse_feed(feed)
assert [o["link"] for o in p_orders] == ["https://profi.ru/backoffice/n.php?o=96314683",
                                         "https://profi.ru/backoffice/n.php?o=96207094"]   # дубль из карусели убран
p = p_orders[0]
assert (p["source"], p["budget"], p["offers"]) == ("Profi.ru", 45000, 0)
assert p["description"] == "Корпоративный сайт\nКонтент есть\nНужен сайт для МФО"
assert p["category"] == "Дистанционно · Москва · Топ-заказ"
assert p["published_at"] == "2026-11-22T21:48:25"                                     # unix → московское время
assert p_orders[1]["budget"] == 0
assert profi.parse_feed({"errors": [{"message": "x"}]}) == []
jwt = "h." + base64.urlsafe_b64encode(_json.dumps({"exp": 1000}).encode()).decode().rstrip("=") + ".s"
pairs = profi.parse_cookie(f"cookie: a=1; prfr_bo_tkn={jwt}; b=x=y")
assert pairs == {"a": "1", "prfr_bo_tkn": jwt, "b": "x=y"} and profi.build_cookie(pairs).startswith("a=1; prfr")
assert profi.token_left(pairs, now=400) == 600 and profi.token_left({}, now=0) == float("-inf")
assert "Profi.ru".startswith(sources.UNKNOWN_OFFERS)                                 # число откликов неизвестно
# вход по СМС: номер в формате сайта, маска для админки, ошибки шага
assert profi.normalize_phone("8 (912) 345-67-89") == profi.normalize_phone("+79123456789") == "+7 912 345 67 89"
assert profi.normalize_phone("9123456789") == "+7 912 345 67 89" and profi.mask_phone("+7 912 345 67 89") == "+7 *** *** ** 89"
for bad in ("12345", "+7 495 123-45-67", ""):
    try:
        profi.normalize_phone(bad)
        raise AssertionError(bad)
    except profi.ProfiError:
        pass
assert profi._result_error({"__typename": "AuthStrategyResultRetry",
                            "answer": {"errors": [{"message": "Неверный код"}]}}) == "Неверный код"
assert profi._result_error({"__typename": "AuthStrategyResultSuccess"}) is None
assert not profi.login_pending()

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
assert {a["exchange"]: a["enabled"] for a in db.list_accounts()} == {"Kwork": 1, "FL.ru": 1, "Profi.ru": 0}
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
        return (f"TG @{title}", [{**p0, "published_at": sources.msk_now().isoformat(),  # свежий пост
                                  "link": f"https://t.me/{title}/{i}", "source": f"TG @{title}",
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

# --- FL.ru RSS и обрезка текста в уведомлении ---
FL = """<?xml version="1.0" encoding="utf-8"?><rss><channel><item><title><![CDATA[Telegram-бот для магазина (Бюджет: 25 000 &#8381;)]]></title>
<link>https://www.fl.ru/projects/1/bot.html</link><description><![CDATA[Нужен <b>бот</b>, бюджет 20 000 руб]]></description>
<category>Программирование / Боты</category><pubDate>Sat, 22 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
fl = sources.parse_fl_rss(FL)[0]
assert (fl["source"], fl["budget"], fl["published_at"]) == ("FL.ru", 25000, "2026-11-22T13:00:00")
assert "₽" in fl["title"]
assert fl["description"] == "Нужен бот, бюджет 20 000 руб"
import bot as _bot  # noqa: E402
long = _bot.shorten("слово " * 500, 1500)
assert len(long) < 1560 and long.endswith("на странице заказа)") and "слов…" not in long   # по границе слова
assert _bot.shorten("коротко", 1500) == "коротко"

# --- Telegram: вакансии, старые посты и слово глубоко в тексте не считаются заказами (примеры со скриншота) ---
now_iso = sources.msk_now().isoformat()
def tg(title, desc, published=None):
    return {**p0, "source": "TG @chan", "title": title, "description": desc, "budget": 0, "offers": 0,
            "published_at": published or now_iso}
KW = ["бот", "скрипт", "автоматиз", "сайт"]
vacancies = [
    tg("Менеджер по продажам в IT компанию (b2b)", "Менеджер по продажам в IT компанию. Предоставляем услуги заказной "
       "разработки и автоматизацию бизнес-процессов. Ищем менеджера в нашу команду. Оклад + %. Обязанности: звонки."),
    tg("Сейчас мы в поисках Продуктового маркетолога", "Сейчас мы в поисках Продуктового маркетолога. Требования: опыт. "
       "Мы предлагаем: удалёнку, скрипт продаж и обучение."),
]
for v in vacancies:
    assert sources.apply_filters(v, KW, [], 0, False, []) == [], v["title"]
deep = tg("👔 Специалист по закупке рекламы", "Тип работы: разовая. " + "Задача: закупка рекламы. " * 20 + "Нужен опыт с ботами")
assert sources.apply_filters(deep, KW, [], 0, False, []) == []              # «бот» только глубоко в тексте
order = tg("Нужен Telegram-бот для записи клиентов", "Нужен бот на aiogram, бюджет 15 000 ₽, срок неделя")
assert sources.apply_filters(order, KW, [], 0, False, []) == ["бот"]         # настоящий заказ проходит
old = tg("Нужен бот", "Нужен бот, бюджет 5000 руб", published="2026-10-01T10:00:00")
assert sources.apply_filters(old, KW, [], 0, False, []) == []              # пост старше 3 дней
# шанс: 22-дневный пост из Telegram без бюджета — низкий, а не высокий
assert sources.chance_label(sources.chance({**order, "budget": 0, "published_at": "2026-10-31T10:00:00"},
                                           sources.msk_now())) == "низкий"
# Kwork эти правила не задевают: заказ со словом «требования» в ТЗ проходит
kw = {**o, "source": "Kwork", "title": "Доработать бота", "description": "Требования: aiogram. Обязанности бота: запись"}
assert sources.apply_filters(kw, ["бот"], [], 0, False, []) == ["бот"]


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

# --- нейросеть: разбор ответа, учёт оценки в шансе, вывод в боте ---
import ai  # noqa: E402
parsed = ai.parse_answer('<think>хм</think>```json\n{"score": "87", "hot": true, "summary": "Бот записи", '
                         '"price": "10 тыс. ₽", "days": "3 дня", "risks": "", "reply": "Здравствуйте!"}\n```')
assert parsed["ai_score"] == 87 and parsed["ai_hot"] == 1 and parsed["ai_reply"] == "Здравствуйте!"
assert ai.parse_answer('{"score": 500, "hot": "нет"}')["ai_score"] == 100           # оценка в пределах 0–100
assert ai.parse_answer('{"score": 7}')["ai_score"] == 70                              # модель отвечает из 10
assert ai.parse_answer('{"score": "8.5"}')["ai_score"] == 85
assert [sources.ten(x) for x in (0, 44, 45, 87, 100, None)] == [0, 4, 4, 9, 10, 0]   # показ «из 10»
try:
    ai.parse_answer("не JSON")
    raise AssertionError("ответ без JSON должен давать AIError")
except ai.AIError:
    pass
db.save_ai(oid, {**parsed, "ai_hot": 1})
db.set_delay(oid, 17)
text = bot.format_order(db.get_order(oid))
assert "🤖 <b>ИИ: 9/10</b>" in text and "дошёл за <b>17 сек</b>" in text and "<blockquote expandable>" in text
assert "ГОРЯЧИЙ" in text or db.get_order(oid)["published_at"] < (sources.msk_now() - timedelta(days=1)).isoformat()
assert any(b.callback_data == f"ai:{oid}" for row in bot.order_kb(db.get_order(oid)).inline_keyboard for b in row)
assert bot.fmt_delay(75) == "1 мин 15 сек" and 0 <= db.avg_delay() <= 17   # в опросе выше тоже засечена доставка

# --- FL.ru: полный текст со страницы заказа ---
FL_PAGE = '<div id="projectp5530123" class="text-5">  Нужен бот.<br /> <br />Бюджет &laquo;30 тыс&raquo;  </div><div>x</div>'
assert sources.parse_fl_page(FL_PAGE) == "Нужен бот.\n\nБюджет «30 тыс»" and sources.parse_fl_page("<p>") == ""
assert sources.parse_fl_rss(FL.replace("20 000 руб", "20 000 руб..."))[0]["cut"] is True

# --- шанс получить заказ ---
fresh = {**o, "offers": 2, "budget": 8000, "published_at": "2026-11-20T12:00:00"}
t0 = datetime.fromisoformat("2026-11-20T12:10:00")
assert sources.chance(fresh, t0) == 100 and sources.chance_label(100) == "высокий"     # свежий, мало откликов, бюджет
crowded = {**fresh, "offers": 60, "budget": 500, "published_at": "2026-11-19T12:00:00"}
assert sources.chance(crowded, t0) == 10 and sources.chance_label(10) == "низкий"       # 60 откликов, сутки, 500 ₽
assert sources.chance({**fresh, "published_at": None}, t0) == 100 - 4 - 30 + 10          # без даты — как сутки
assert sources.chance({**fresh, "ai_score": 40}, t0) == 70                                # ИИ: (100 + 40) / 2


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
        assert "Откликнуться на бирже" in page and "Копировать" in page                     # страница заказа
        assert "🤖 Разбор нейросетью" in page and "Здравствуйте!" in page                  # разбор ИИ на странице
        assert "🤖 9/10" in await (await client.get("/orders?view=all")).text()
        db.set_setting("ai_key", "test-key-123")
        assert "💡 Получить совет" in await (await client.get(f"/orders/{oid}")).text()   # блок совета ИИ
        db.save_ai(oid, {"ai_advice": "🎯 Брать: да"})
        assert "🎯 Брать: да" in await (await client.get(f"/orders/{oid}")).text()
        assert (await client.post("/orders/99999/advice", allow_redirects=False)).status == 404
        # ответ заказчику и «короче/подробнее» на странице заказа
        page = await (await client.get(f"/orders/{oid}")).text()
        assert "💬 Ответ заказчику" in page and "✂️ Короче" in page and "📝 Подробнее" in page
        db.save_ai(oid, {"ai_question": "Сколько стоит?", "ai_answer": "От 10 тыс. ₽"})
        assert "От 10 тыс. ₽" in await (await client.get(f"/orders/{oid}")).text()
        for path in ("rewrite", "answer"):
            assert (await client.post(f"/orders/99999/{path}", allow_redirects=False)).status == 404
        db.set_setting("ai_key", "")
        # настройки уведомлений и маркеры подвоха
        await client.post("/settings/general", data={"ai_min_score": "7", "quiet_from": "23", "quiet_to": "8",
                                                     "nudge_hours": "3", "scam_words": "оплата после,\nбартер, бартер"})
        assert (db.int_setting("ai_min_score"), db.int_setting("quiet_from"), db.int_setting("nudge_hours")) == (7, 23, 3)
        assert db.scam_words() == ["оплата после", "бартер"]
        page = await (await client.get("/settings")).text()
        assert "Тихие часы" in page and "оплата после, бартер" in page
        db.set_setting("quiet_from", "0"); db.set_setting("quiet_to", "0"); db.set_setting("ai_min_score", "0")
        # ИИ-статистика в аналитике
        assert "Доверять ли оценке ИИ" in await (await client.get("/analytics")).text()
        resp = await client.post("/orders/99999/ai", allow_redirects=False)
        assert resp.status == 404
        resp = await client.post("/settings/ai", data={"model": ai.DEFAULT_MODEL, "about": "Тим", "enabled": "1"},
                                 allow_redirects=False)
        assert resp.status == 302 and db.get_setting("ai_enabled") == "1" and not ai.enabled()  # без ключа — выкл
        resp = await client.post("/settings/ai", data={"key": "board=1; sid=abc; prfr_bo_tkn=x", "model": ai.DEFAULT_MODEL,
                                                       "about": "Тим", "enabled": "1"}, allow_redirects=False)
        assert "ai_msg=" in resp.headers["Location"] and not db.get_setting("ai_key")  # cookie вместо ключа не сохранена
        assert ai.looks_like_key("sk-munito_AbC123xyz") and not ai.looks_like_key("a=1; b=2")
        resp = await client.post("/accounts", data={"exchange": "Profi.ru", "action": "connect", "secret": "sk-key123"})
        assert resp.status == 400 and not (db.get_account("Profi.ru")["secret"] or "")      # ключ вместо cookie Profi
        # вход в Profi по СМС из админки (сеть подменена: проверяем маршрут и шаблон)
        assert "Войти по СМС" in await (await client.get("/settings")).text()
        async def fake_start(phone):
            profi._login.update(pairs={}, strategy="s", step="t", phone=profi.normalize_phone(phone),
                                until=profi.time.monotonic() + 60)
            return profi.mask_phone(phone)
        async def fake_finish(code):
            if code != "1234":
                raise profi.ProfiError("Неверный код")
            profi.login_cancel()
            return "+7 *** *** ** 89"
        real = profi.login_start, profi.login_finish
        profi.login_start, profi.login_finish = fake_start, fake_finish
        resp = await client.post("/accounts/profi-login", data={"action": "send_code", "phone": "89123456789"})
        page = await resp.text()
        assert "Код из СМС на +7 *** *** ** 89" in page and "Код отправлен" in page
        assert "Неверный код" in await (await client.post("/accounts/profi-login",
                                                           data={"action": "code", "code": "0000"})).text()
        assert "cookie обновлена" in await (await client.post("/accounts/profi-login",
                                                               data={"action": "code", "code": "1234"})).text()
        profi.login_start, profi.login_finish = real
        assert "Код из СМС" not in await (await client.get("/settings")).text()
        assert "Нейросеть munito.ai" in await (await client.get("/settings")).text()
        assert (await client.get("/orders/99999")).status == 404
        resp = await client.post(f"/orders/{oid}/respond", allow_redirects=False)
        assert resp.headers["Location"].startswith("/deals/")                       # «Я откликнулся» — сделка; на биржу не ведёт
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

# --- тихие часы, маркеры подвоха, «заказчик ответил?», сводка за ночь, статистика ИИ ---
import main  # noqa: E402
import bot as tgbot  # noqa: E402
db.set_setting("quiet_from", "23"); db.set_setting("quiet_to", "8")
assert main.quiet_now(datetime(2026, 1, 1, 23)) and main.quiet_now(datetime(2026, 1, 1, 3))
assert not main.quiet_now(datetime(2026, 1, 1, 8)) and not main.quiet_now(datetime(2026, 1, 1, 15))
db.set_setting("quiet_from", "13"); db.set_setting("quiet_to", "15")
assert main.quiet_now(datetime(2026, 1, 1, 14)) and not main.quiet_now(datetime(2026, 1, 1, 15))
db.set_setting("quiet_from", "0"); db.set_setting("quiet_to", "0")
assert not main.quiet_now(datetime(2026, 1, 1, 3))                                  # одинаковые — выключены
assert sources.scam_flags({"title": "Сайт", "description": "Оплата ПОСЛЕ сдачи, бартер"}, ["оплата после", "бартер",
                                                                                     "за отзыв"]) == ["оплата после", "бартер"]
scam_id = db.upsert_order({"source": "Kwork", "link": "https://kwork.ru/projects/scam1", "title": "Бот, оплата после",
                           "description": "", "category": "", "budget": 1000, "budget_max": 1000, "views": 0,
                           "offers": 0, "published_at": None}, ["бот"])
assert "⚠️ <b>Осторожно:</b> «оплата после»" in tgbot.format_order(db.get_order(scam_id))
summary = tgbot.quiet_summary_text([scam_id, scam_id, 999999])
assert summary and "Пока вы спали — подходящих заказов: 1" in summary and tgbot.quiet_summary_text([999999]) is None
deal_id = db.deal_from_order(scam_id)
db.set_setting("nudge_hours", "2")
assert not db.nudge_due()                                                            # только что откликнулся
with db.closing(db._conn()) as conn, conn:
    conn.execute("UPDATE projects SET updated_at = datetime('now', 'localtime', '-3 hours') WHERE id = ?", (deal_id,))
assert [d["id"] for d in db.nudge_due()] == [deal_id] and "Заказчик молчит" in tgbot.nudge_text(db.nudge_due()[0])
db.set_nudged(deal_id)
assert not db.nudge_due()
db.save_ai(scam_id, {"ai_score": 80})
top = [r for r in db.ai_stats() if r["bucket"] == "8–10"]
assert top and top[0]["responses"] >= 1 and 0 <= top[0]["conversion"] <= 100
print("all OK")
