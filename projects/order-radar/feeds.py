"""Загрузка RSS-лент бирж и поиск по ключевым словам."""
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import aiohttp

HEADERS = {"User-Agent": "Mozilla/5.0 (order-radar)"}


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def clean_keyword(word: str) -> str:
    """Ключевое слово из админки или бота: в нижнем регистре, без лишних пробелов."""
    return normalize(" ".join(word.split()))[:50]


def match(text: str, keywords: list[str]) -> list[str]:
    """Ключевые слова, найденные в тексте. Ищутся с начала слова: «бот» найдётся в «боты», но не в «работа»."""
    text = normalize(text)
    return [k for k in keywords if re.search(r"\b" + re.escape(normalize(k)), text)]


def parse_rss(xml_text: str, source: str) -> list[dict]:
    orders = []
    for item in ET.fromstring(xml_text).iter("item"):
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        if not link or not title:
            continue
        pub = item.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(pub).isoformat() if pub else None
        except (TypeError, ValueError):
            published_at = None
        orders.append({
            "source": source,
            "link": link,
            "title": title,
            "description": re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip(),
            "category": (item.findtext("category") or "").strip(),
            "published_at": published_at,
        })
    return orders


async def fetch(session: aiohttp.ClientSession, source: str, url: str) -> list[dict]:
    """Заказы из одной ленты. При ошибке сети или формата — пустой список, чтобы не ронять опрос."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            return parse_rss(await resp.text(), source)
    except (aiohttp.ClientError, TimeoutError, ET.ParseError) as e:
        logging.warning("Лента %s недоступна: %s", source, e)
        return []
