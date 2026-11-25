"""Сбор заказов с бирж и фильтрация по ключевым словам."""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import aiohttp

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from config import (FL_FEEDS, HIRING_WORDS, HOT_MAX_AGE_MIN, HOT_MAX_OFFERS, KWORK_PAGES, KWORK_URL,
                    TG_KEYWORD_ZONE, TG_MAX_AGE_DAYS, USD_RUB)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


# ---------- Текст и фильтры ----------

def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def clean_word(word: str) -> str:
    """Слово из админки или бота: нижний регистр, без лишних пробелов."""
    return normalize(" ".join(word.split()))[:50]


def match(text: str, words: list[str]) -> list[str]:
    """Слова, найденные в тексте. Ищутся с начала слова: «бот» найдётся в «боты», но не в «работа»."""
    text = normalize(text)
    return [w for w in words if re.search(r"\b" + re.escape(normalize(w)), text)]


def filter_order(o: dict, plus: list[str], minus: list[str], min_budget: int) -> list[str]:
    """Ключевые слова, по которым заказ подходит; пустой список — заказ не подходит.
    Минус-слова на биржах — только в названии: в описании «дизайн» или «вакансия» часто стоят и у нужных заказов
    («верстка по готовому дизайну»). У постов Telegram «названия» нет (первая строка — «👔 Маркетолог»),
    поэтому там минус-слова ищутся по всему тексту.
    Категорию не смотрим: «Скрипты, боты и mini apps» совпадает со «бот» у любого заказа.
    """
    text = f"{o['title']} {o['description']}"
    minus_scope = text if o["source"].startswith("TG ") else o["title"]
    if match(minus_scope, minus) or (min_budget and o["budget"] < min_budget):
        return []
    return match(text, plus)


def is_vacancy(text: str) -> bool:
    """Найм в штат, а не разовый заказ: два и больше признаков вакансии («в команду», «оклад», «обязанности»…)."""
    return len(match(text, HIRING_WORDS)) >= 2


def apply_filters(o: dict, plus: list[str], minus: list[str], min_budget: int, is_chat: bool,
                  intent_words: list[str]) -> list[str]:
    """filter_order + правила для Telegram, где кроме заказов много вакансий, рекламы и болтовни."""
    matched = filter_order(o, plus, minus, min_budget)
    if not matched or not o["source"].startswith("TG "):
        return matched
    text = f"{o['title']}\n{o['description']}"
    if is_vacancy(text):
        return []
    if o["published_at"] and (msk_now() - datetime.fromisoformat(o["published_at"])).days >= TG_MAX_AGE_DAYS:
        return []  # старый пост — заказ уже, скорее всего, взяли
    head = match(text[:TG_KEYWORD_ZONE], matched)  # слово должно быть в начале: там суть, а не история компании
    if not head or (is_chat and not looks_like_order(o["description"], intent_words)):
        return []
    return head


def chance(o, now: datetime) -> int:
    """Шанс получить заказ, 0–100: меньше откликов и свежее — выше; нормальный бюджет — плюс.
    shortcut: веса подобраны на глаз; когда наберётся статистика выигранных сделок, подобрать по ней."""
    age_h = (now - datetime.fromisoformat(o["published_at"])).total_seconds() / 3600 if o["published_at"] else 24
    # в Telegram и FL.ru число откликов неизвестно — считаем, что конкуренция средняя, а не нулевая
    offers_penalty = 20 if o["source"].startswith(UNKNOWN_OFFERS) else min((o["offers"] or 0) * 2, 50)
    score = 100 - offers_penalty - min(int(max(age_h, 0) * 5), 30)
    if o["budget"] >= 5000:
        score += 10
    elif 0 < o["budget"] < 1000:
        score -= 10
    if age_h > 72:
        score = min(score, 30)  # заказу больше трёх дней — шанс низкий, как бы мало откликов ни было
    score = max(0, min(score, 100))
    if (ai := field(o, "ai_score")) is not None:  # нейросеть оценила, подходит ли заказ, — учитываем наравне
        score = (score + ai) // 2
    return score


def field(o, key: str):
    """Значение поля заказа или None (у словаря из парсера нет колонок, которые появляются в базе позже)."""
    return o[key] if key in o.keys() else None


def scam_flags(o, words: list[str]) -> list[str]:
    """Слова-маркеры подвоха, найденные в названии и описании заказа (без учёта регистра и ё)."""
    text = normalize(f"{field(o, 'title') or ''} {field(o, 'description') or ''}")
    return [w for w in words if normalize(w) in text]


def ten(score) -> int:
    """Оценка 0–100 (так она хранится и считается) → баллы из 10 для показа."""
    return max(0, min(10, round((score or 0) / 10)))


def chance_label(score: int) -> str:
    return "высокий" if score >= 70 else "средний" if score >= 40 else "низкий"


def msk_now() -> datetime:
    """Текущее московское время без часового пояса — в том же виде, в каком Kwork отдаёт даты заказов."""
    return (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)


def is_hot(order, now: datetime) -> bool:
    if not order["published_at"]:
        return False
    age_min = (now - datetime.fromisoformat(order["published_at"])).total_seconds() / 60
    if field(order, "ai_hot") and age_min <= 24 * 60:  # нейросеть сказала «брать сейчас» — горячий сутки
        return True
    return age_min <= HOT_MAX_AGE_MIN and (order["offers"] or 0) <= HOT_MAX_OFFERS


# ---------- Kwork ----------

def _json_after(html: str, key: str):
    """JSON-значение, которое идёт в HTML сразу после "key": (Kwork встраивает данные страницы в скрипт)."""
    i = html.find(f'"{key}":')
    if i < 0:
        raise ValueError(f"на странице нет {key}")
    return json.JSONDecoder().raw_decode(html, i + len(key) + 3)[0]


def parse_kwork(html: str) -> list[dict]:
    categories = dict(re.findall(r'"CATID":"(\d+)","id":"\d+","name":"([^"]+)"', html))
    orders = []
    for w in _json_after(html, "wants"):
        try:
            budget = int(float(w.get("priceLimit") or 0))
            budget_max = int(float(w.get("possiblePriceLimit") or 0))
            orders.append({
                "source": "Kwork",
                "link": f"https://kwork.ru/projects/{int(w['id'])}",
                "title": str(w.get("name") or "").strip(),
                "description": unescape(re.sub(r"<[^>]+>", "", str(w.get("description") or ""))).strip(),
                "category": categories.get(str(w.get("category_id")), ""),
                "budget": budget,
                "budget_max": max(budget_max, budget),
                "views": int(w.get("views_dirty") or 0),
                "offers": int(w.get("kwork_count") or 0),
                # Время последней публикации (Kwork поднимает старые заказы), московское: «2026-11-19 21:42:40»
                "published_at": datetime.fromisoformat(published).isoformat()
                if (published := w.get("date_active") or w.get("date_create")) else None,
            })
        except (KeyError, TypeError, ValueError) as e:
            logging.warning("Kwork: пропускаю заказ %s: %s", w.get("id"), e)
    return orders


async def fetch_kwork(session: aiohttp.ClientSession, pages: int = KWORK_PAGES) -> list[dict]:
    """Свежие заказы Kwork. При ошибке сети или смене вёрстки — то, что успели собрать (без падения опроса).
    Частая проверка берёт только 1-ю страницу (новые заказы появляются там), полная — все."""
    orders = []
    for page in range(1, pages + 1):
        try:
            async with session.get(KWORK_URL.format(page=page), headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                orders += parse_kwork(await resp.text())
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            logging.warning("Kwork, страница %s: %s", page, e)
            break
    return orders


# ---------- FL.ru (RSS) ----------

def parse_fl_rss(xml_text: str) -> list[dict]:
    orders = []
    for item in ET.fromstring(xml_text).iter("item"):
        link = (item.findtext("link") or "").strip()
        title = unescape(item.findtext("title") or "").strip()  # FL кодирует ₽ как &#8381; внутри CDATA
        if not link or not title:
            continue
        description = unescape(re.sub(r"<[^>]+>", "", item.findtext("description") or "")).strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            published_at = (published.astimezone(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None).isoformat()
        except (TypeError, ValueError):
            published_at = None
        budget = extract_budget(f"{title} {description}")
        orders.append({"source": "FL.ru", "link": link, "title": title, "description": description,
                       "category": (item.findtext("category") or "").strip(), "budget": budget, "budget_max": budget,
                       "views": 0, "offers": 0, "published_at": published_at,
                       "cut": description.endswith("...")})  # в RSS описание обрезано — полное на странице заказа
    return orders


FL_TEXT_RE = re.compile(r'<div id="projectp\d+"[^>]*>(.*?)</div>', re.S)


def parse_fl_page(html: str) -> str:
    """Полное описание со страницы заказа FL.ru. '' — не нашли (сменилась вёрстка)."""
    m = FL_TEXT_RE.search(html)
    if not m:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", m.group(1))
    text = unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(line.strip() for line in text.strip().splitlines())


# Лента fl.ru/projects: свежая (no-store), сразу по всем разделам, с числом откликов и просмотров.
# RSS FL кешируется до часа (max-age=3600) — из-за этого заказы приходили с опозданием до 30+ минут.
FL_LIST_URL = "https://www.fl.ru/projects/"
FL_BLOCK_RE = re.compile(r'(?=<div[^>]+id="project-item\d+")')
AGO_RE = re.compile(r"(\d+)\s*(сек|мин|час|дн|ден)")


def parse_ago(text: str, now: datetime) -> datetime | None:
    """«1 час 59 минут назад», «10 минут назад», «только что» → момент публикации (по часам now)."""
    text = text.lower()
    if "только что" in text:
        return now
    delta, found = timedelta(), False
    for num, unit in AGO_RE.findall(text):
        found = True
        n = int(num)
        delta += {"сек": timedelta(seconds=n), "мин": timedelta(minutes=n), "час": timedelta(hours=n)}.get(
            unit, timedelta(days=n))
    return now - delta if found else None


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)).replace("\xa0", " ")).strip()


def parse_fl_list(html: str, now: datetime) -> list[dict]:
    """Заказы со страницы fl.ru/projects (вакансии пропускаем — это наём в штат)."""
    orders = []
    for block in FL_BLOCK_RE.split(html)[1:]:
        title_m = re.search(r'id="prj_name_(\d+)"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not title_m:
            continue
        kind = re.search(r'b-post__bold b-layout__txt_inline-block[^"]*">(.*?)</span>', block, re.S)
        if kind and _text(kind.group(1)).lower().startswith("ваканс"):
            continue
        price_m = re.search(r'b-post__price[^"]*">(.*?)</div>', block, re.S)
        amounts = [int(re.sub(r"\D", "", a)) for a in re.findall(r"\d[\d\s]*", _text(price_m.group(1)) if price_m else "")]
        desc_m = re.search(r'class="b-post__txt text-5 ?">(.*?)</div>', block, re.S)
        description = _text(desc_m.group(1)) if desc_m else ""
        when_m = re.search(r'text-gray-opacity-4 text-7 mr-16">(.*?)</span>', block, re.S)
        published = parse_ago(_text(when_m.group(1)), now) if when_m else None
        offers_m = re.search(r"(\d+)\s+ответ", block)
        views_m = re.search(r'title="Количество просмотров".*?<span class="text-7">(.*?)</span>', block, re.S)
        views = re.sub(r"\D", "", _text(views_m.group(1))) if views_m else ""
        orders.append({
            "source": "FL.ru", "link": "https://www.fl.ru" + title_m.group(2), "title": _text(title_m.group(3)),
            "description": description, "category": "",
            "budget": amounts[0] if amounts else 0, "budget_max": max(amounts) if amounts else 0,
            "views": int(views or 0), "offers": int(offers_m.group(1)) if offers_m else 0,
            "published_at": published.replace(microsecond=0).isoformat() if published else None,
            "cut": description.endswith(("...", "…", ",...")),  # полный текст — на странице заказа
        })
    return orders


async def fetch_fl_list(session: aiohttp.ClientSession, pages: int = 1) -> list[dict]:
    orders = []
    for page in range(1, pages + 1):
        url = FL_LIST_URL if page == 1 else f"{FL_LIST_URL}page-{page}/"
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            orders += parse_fl_list(await resp.text(), msk_now())
    return orders


FL_STATE = {"fallback": ""}  # почему пришлось читать RSS вместо ленты ('' — лента работает); для оповещения о поломке


async def fetch_fl(session: aiohttp.ClientSession, pages: int = 1) -> list[dict]:
    """Заказы FL.ru: свежая лента fl.ru/projects (частая проверка — 1 страница, полная — 2).
    Если вёрстка сменилась и лента пустая — запасной путь через RSS (он опаздывает, но работает)."""
    try:
        if orders := await fetch_fl_list(session, min(pages, 2)):
            FL_STATE["fallback"] = ""
            return orders
        FL_STATE["fallback"] = "в ленте fl.ru/projects не нашлось заказов — похоже, сменилась вёрстка"
    except (aiohttp.ClientError, TimeoutError) as e:
        FL_STATE["fallback"] = f"лента fl.ru/projects не открывается: {e or type(e).__name__}"
    logging.warning("FL.ru: %s — беру RSS", FL_STATE["fallback"])
    return await fetch_fl_rss(session)


async def fetch_fl_rss(session: aiohttp.ClientSession) -> list[dict]:
    """Заказы FL.ru из RSS разделов «Программирование» и «Сайты»."""
    orders, seen = [], set()
    for url in FL_FEEDS:
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                for o in parse_fl_rss(await resp.text()):
                    if o["link"] not in seen:  # один заказ бывает в обоих разделах
                        seen.add(o["link"])
                        orders.append(o)
        except (aiohttp.ClientError, TimeoutError, ET.ParseError) as e:
            logging.warning("FL.ru, %s: %s", url, e)
    return orders


# ---------- freelance.ru (лента freelance.ru/task) ----------

FR_LIST_URL = "https://freelance.ru/task"
FR_CARD_RE = re.compile(r'<article class="task-card.*?</article>', re.S)
FR_TEXT_RE = re.compile(r"<h1>.*?</h1>(.*?)<div class=\"tv-hero__meta\"", re.S)


def _int(text: str) -> int:
    return int(re.sub(r"\D", "", text) or 0)


def parse_freelanceru_list(html: str, now: datetime) -> list[dict]:
    orders = []
    for card in FR_CARD_RE.findall(html):
        title_m = re.search(r'class="task-card__title-link" href="(/task/view/\d+)"[^>]*>(.*?)</a>', card, re.S)
        if not title_m:
            continue
        desc_m = re.search(r'class="task-card__desc">(.*?)</p>', card, re.S)
        description = _text(desc_m.group(1)) if desc_m else ""
        budget_m = re.search(r'task-card__budget">(.*?)</div>', card, re.S)
        budget = _int(_text(budget_m.group(1)).split("/")[0]) if budget_m else 0  # «4 500 ₽ / заказ»
        when_m = re.search(r'fa-clock-o[^>]*></i>(.*?)</span>', card, re.S)
        published = parse_ago(_text(when_m.group(1)), now) if when_m else None
        offers_m = re.search(r'fa-comments[^>]*></i>(.*?)</span>', card, re.S)
        views_m = re.search(r'fa-eye[^>]*></i>(.*?)</span>', card, re.S)
        orders.append({
            "source": "freelance.ru", "link": "https://freelance.ru" + title_m.group(1), "title": _text(title_m.group(2)),
            "description": description,
            "category": ", ".join(_text(c) for c in re.findall(r'task-chip--cat">(.*?)</span>', card, re.S)),
            "budget": budget, "budget_max": budget,
            "views": _int(_text(views_m.group(1))) if views_m else 0,
            "offers": _int(_text(offers_m.group(1))) if offers_m else 0,
            "published_at": published.replace(microsecond=0).isoformat() if published else None,
            "cut": description.endswith(("...", "…")),  # в ленте описание обрезано — полное на странице заказа
        })
    return orders


def parse_freelanceru_page(html: str) -> str:
    """Полное описание со страницы задания freelance.ru. '' — не нашли (сменилась вёрстка)."""
    m = FR_TEXT_RE.search(html)
    if not m:
        return ""
    text = unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", m.group(1))))
    text = "\n".join(re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.strip().splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def fetch_freelanceru(session: aiohttp.ClientSession, pages: int = 1) -> list[dict]:
    orders = []
    for page in range(1, min(pages, 2) + 1):
        try:
            async with session.get(FR_LIST_URL, params={"page": page} if page > 1 else None, headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                orders += parse_freelanceru_list(await resp.text(), msk_now())
        except (aiohttp.ClientError, TimeoutError) as e:
            logging.warning("freelance.ru, страница %s: %s", page, e)
            break
    return orders


# ---------- Weblancer (лента weblancer.net/freelance) ----------
# В ленте только дата без времени, бюджет — в долларах (переводим в рубли по курсу USD_RUB из .env).

WL_LIST_URL = "https://www.weblancer.net/freelance/"
WL_CARD_RE = re.compile(r"<article.*?</article>", re.S)


def parse_weblancer_list(html: str, now: datetime) -> list[dict]:
    orders = []
    for card in WL_CARD_RE.findall(html):
        title_m = re.search(r'<h2[^>]*>\s*<a[^>]*href="(/freelance/[^"]+-\d+/)"[^>]*>(.*?)</a>', card, re.S)
        if not title_m:
            continue
        price_m = re.search(r'</h2>\s*<span[^>]*>(.*?)</span>', card, re.S)
        price = _text(re.sub(r"<!--.*?-->", "", price_m.group(1))) if price_m else ""
        amount = _int(price)
        rate = USD_RUB if "$" in price else 1 if ("₽" in price or "руб" in price) else 0
        budget = int(amount * rate)
        desc_m = re.search(r"<p(?:\s[^>]*)?>(.*?)</p>", card, re.S)  # не <path> из иконок
        description = _text(desc_m.group(1)) if desc_m else ""
        if amount and rate != 1:
            description += f"\n\nБюджет на бирже: {price}"
        date_m = re.search(r"<span>(\d{2})\.(\d{2})\.(\d{4})</span>", card)
        published = None
        if date_m:
            day = datetime(int(date_m.group(3)), int(date_m.group(2)), int(date_m.group(1)))
            # время в ленте не указано: сегодняшний заказ — «только что» (радар видит его в первые же секунды),
            # более старый — полдень того дня
            published = now if day.date() == now.date() else day.replace(hour=12)
        offers_m = re.search(r"<span>(\d+)\s+заяв", card)
        views_m = re.search(r"<span>(\d+)\s+просмотр", card)
        orders.append({
            "source": "Weblancer", "link": "https://www.weblancer.net" + title_m.group(1),
            "title": _text(title_m.group(2)), "description": description,
            "category": ", ".join(_text(c) for c in re.findall(r'<a[^>]*href="/freelance/[a-z0-9-]+/"[^>]*>(.*?)</a>',
                                                                card, re.S)),
            "budget": budget, "budget_max": budget,
            "views": int(views_m.group(1)) if views_m else 0, "offers": int(offers_m.group(1)) if offers_m else 0,
            "published_at": published.replace(microsecond=0).isoformat() if published else None,
        })
    return orders


async def fetch_weblancer(session: aiohttp.ClientSession, pages: int = 1) -> list[dict]:
    orders = []
    for page in range(1, min(pages, 2) + 1):
        try:
            async with session.get(WL_LIST_URL, params={"page": page} if page > 1 else None, headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                orders += parse_weblancer_list(await resp.text(), msk_now())
        except (aiohttp.ClientError, TimeoutError) as e:
            logging.warning("Weblancer, страница %s: %s", page, e)
            break
    return orders


async def fetch_full(session: aiohttp.ClientSession, link: str) -> str:
    """Полный текст заказа со страницы биржи, если в ленте он обрезан (FL.ru, freelance.ru). '' — не получилось."""
    parser = parse_freelanceru_page if "//freelance.ru/" in link else parse_fl_page
    try:
        async with session.get(link, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            return parser(await resp.text())
    except (aiohttp.ClientError, TimeoutError) as e:
        logging.warning("Полный текст %s: %s", link, e)
        return ""


SOURCES = [("Kwork", fetch_kwork), ("FL.ru", fetch_fl), ("freelance.ru", fetch_freelanceru),
           ("Weblancer", fetch_weblancer)]
UNKNOWN_OFFERS = ("TG ", "Profi.ru")  # у этих источников число откликов неизвестно (FL — теперь известно из ленты)
UNKNOWN_VIEWS = ("Profi.ru",)         # Profi не показывает и просмотры


def counts(o) -> tuple[str, str]:
    """Просмотры и отклики для показа: «?», если источник их не сообщает."""
    source = field(o, "source") or ""
    views = "?" if source.startswith(UNKNOWN_VIEWS) else str(field(o, "views") or 0)
    offers = "?" if source.startswith(UNKNOWN_OFFERS) else str(field(o, "offers") or 0)
    return views, offers


# ---------- Telegram ----------

BUDGET_RE = re.compile(r"(\d[\d\s]{0,8}\d|\d)(?:[.,]\d+)?\s*(₽|руб|р\.|р\b|rub|тыс|k\b|к\b)", re.I)
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))


def extract_budget(text: str) -> int:
    """Самая крупная сумма в тексте: «5 000 ₽», «15к», «до 20 тыс. руб». 0 — суммы нет."""
    best = 0
    for num, unit in BUDGET_RE.findall(text):
        value = int(re.sub(r"\s", "", num))
        if unit.lower() in ("тыс", "k", "к"):
            value *= 1000
        if value <= 5_000_000:  # отсекаем телефоны и номера заказов
            best = max(best, value)
    return best


def tg_username(raw: str) -> str:
    """'@name', 'name', 'https://t.me/name', 't.me/s/name' → 'name'. Пустая строка — не похоже на username."""
    raw = raw.strip()
    m = re.fullmatch(r"(?:https?://)?(?:t\.me/(?:s/)?|@)?([A-Za-z][A-Za-z0-9_]{3,31})/?", raw)
    return m.group(1) if m else ""


def looks_like_order(text: str, intent_words: list[str]) -> bool:
    return bool(match(text, intent_words))


def message_to_order(text: str, link: str, source: str, published_utc: datetime | None, views: int = 0,
                     author: str = "") -> dict | None:
    """Пост канала или сообщение чата → заказ радара. None — пустое сообщение."""
    text = text.translate(ZERO_WIDTH).strip()
    if not text:
        return None
    title = next((line.strip() for line in text.splitlines() if line.strip()), "")[:120]
    budget = extract_budget(text)
    return {
        "source": source,
        "link": link,
        "title": title,
        "description": (text + (f"\n\nАвтор: {author}" if author else ""))[:3000],
        "category": "Telegram",
        "budget": budget,
        "budget_max": budget,
        "views": views,
        "offers": 0,
        "published_at": (published_utc.astimezone(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None).isoformat()
        if published_utc else None,
    }


def _views(raw: str) -> int:
    raw = raw.strip().upper().replace(",", ".")
    mult = 1000 if raw.endswith("K") else 1_000_000 if raw.endswith("M") else 1
    try:
        return int(float(raw.rstrip("KM")) * mult)
    except ValueError:
        return 0


def parse_tg_channel(html: str, username: str) -> list[dict]:
    """Посты со страницы t.me/s/<канал> (последние ~20)."""
    orders = []
    for block in html.split('data-post="')[1:]:
        post = block[:block.index('"')]
        m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not m or not post.lower().startswith(username.lower() + "/"):
            continue
        text = unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", m.group(1))))
        t = re.search(r'<time datetime="([^"]+)"', block)
        v = re.search(r'tgme_widget_message_views">([^<]+)<', block)
        order = message_to_order(text, f"https://t.me/{post}", f"TG @{username}",
                                 datetime.fromisoformat(t.group(1)) if t else None, _views(v.group(1)) if v else 0)
        if order:
            orders.append(order)
    return orders


async def fetch_tg_channel(session: aiohttp.ClientSession, username: str) -> list[dict]:
    """Публичный канал через веб-версию t.me/s — без аккаунта и без бота."""
    try:
        async with session.get(f"https://t.me/s/{username}", headers=HEADERS,
                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            orders = parse_tg_channel(await resp.text(), username)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ValueError(f"недоступен: {e}") from e
    if not orders:
        raise ValueError("нет постов — канал закрытый, пустой или это чат")
    return orders
