"""Сбор заказов с бирж и фильтрация по ключевым словам."""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import aiohttp

from config import HOT_MAX_AGE_MIN, HOT_MAX_OFFERS, KWORK_PAGES, KWORK_URL

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


def apply_filters(o: dict, plus: list[str], minus: list[str], min_budget: int, is_chat: bool,
                  intent_words: list[str]) -> list[str]:
    """filter_order + для чатов: сообщение должно быть похоже на заказ, а не на болтовню."""
    matched = filter_order(o, plus, minus, min_budget)
    if is_chat and matched and not looks_like_order(o["description"], intent_words):
        return []
    return matched


def msk_now() -> datetime:
    """Текущее московское время без часового пояса — в том же виде, в каком Kwork отдаёт даты заказов."""
    return (datetime.now(timezone.utc) + timedelta(hours=3)).replace(tzinfo=None)


def is_hot(order, now: datetime) -> bool:
    if not order["published_at"]:
        return False
    age_min = (now - datetime.fromisoformat(order["published_at"])).total_seconds() / 60
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


async def fetch_kwork(session: aiohttp.ClientSession) -> list[dict]:
    """Свежие заказы Kwork. При ошибке сети или смене вёрстки — то, что успели собрать (без падения опроса)."""
    orders = []
    for page in range(1, KWORK_PAGES + 1):
        try:
            async with session.get(KWORK_URL.format(page=page), headers=HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                orders += parse_kwork(await resp.text())
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            logging.warning("Kwork, страница %s: %s", page, e)
            break
    return orders


SOURCES = [("Kwork", fetch_kwork)]


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
