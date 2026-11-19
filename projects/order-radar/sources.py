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
