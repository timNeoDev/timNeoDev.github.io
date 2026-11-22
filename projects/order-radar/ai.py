"""Нейросеть munito.ai (API в формате OpenAI): разбор заказа, оценка «стоит ли браться», черновик отклика.
Ключ, модель и текст «о себе» хранятся в настройках базы и меняются в админке."""
import json
import logging
import re

import aiohttp

import db

API_URL = "https://api.munito.ai/inference/v1/chat/completions"
MODELS = {  # бесплатный лимит: запросов в минуту
    "deepseek-ai/deepseek-v4.1-flash-1017": "DeepSeek V4.1 Flash — быстрая, 200 запросов/мин (рекомендую)",
    "qwen/qwen3.8-27b": "Qwen 3.8 27B — 15 запросов/мин",
    "zai-org/glm-5.3-flash": "GLM 5.3 Flash — 10 запросов/мин",
    "deepseek-ai/deepseek-v4-flash": "DeepSeek V4 Flash — 10 запросов/мин",
}
DEFAULT_MODEL = next(iter(MODELS))
DEFAULT_ABOUT = ("Тим, фрилансер: Telegram-боты на Python (aiogram 3), лендинги и сайты (HTML/CSS/JS), парсеры, "
                 "интеграции с API, автоматизация. Портфолио: https://timneodev.github.io/ . "
                 "Пример бота: https://t.me/zayavki_demo_pro_bot")
TIMEOUT = aiohttp.ClientTimeout(total=90)

PROMPT = """Ты помогаешь фрилансеру отбирать заказы и писать отклики. О фрилансере:
{about}

Заказ с биржи {source}:
Название: {title}
Бюджет: {budget}
Откликов уже: {offers}
Описание:
{description}

Ответь ТОЛЬКО JSON-объектом без пояснений, на русском:
{{"score": число 0-100 — насколько заказ подходит фрилансеру и стоит ли за него браться (навыки, реальность бюджета, ясность задачи),
 "hot": true — если заказ стоит взять прямо сейчас (подходит по навыкам, адекватный бюджет, понятная задача), иначе false,
 "summary": "что нужно сделать, 1-2 коротких предложения",
 "price": "реалистичная цена в рублях, например «8–12 тыс. ₽»",
 "days": "реалистичный срок, например «3–5 дней»",
 "risks": "главный риск или подводный камень, коротко; пусто, если нет",
 "reply": "отклик заказчику от первого лица: 4-7 предложений, без шаблонных фраз, начни с сути его задачи, предложи как сделаешь, назови срок и цену, задай 1 уточняющий вопрос, дай ссылку на портфолио. Не пиши, что ты начинающий."}}"""


class AIError(Exception):
    pass


def settings() -> dict:
    return {"key": db.get_setting("ai_key"), "model": db.get_setting("ai_model") or DEFAULT_MODEL,
            "about": db.get_setting("ai_about") or DEFAULT_ABOUT, "enabled": db.get_setting("ai_enabled") == "1"}


def enabled() -> bool:
    s = settings()
    return s["enabled"] and bool(s["key"])


async def chat(session: aiohttp.ClientSession, prompt: str, key: str, model: str, max_tokens: int = 4000) -> str:
    """Модели munito сначала «думают» (reasoning, до ~2000 токенов), потом отвечают — лимит с запасом на оба."""
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.4, "max_tokens": max_tokens}
    try:
        async with session.post(API_URL, json=payload, timeout=TIMEOUT,
                                headers={"Authorization": f"Bearer {key}"}) as resp:
            body = await resp.text()
            if resp.status == 401:
                raise AIError("неверный ключ API")
            if resp.status == 429:
                raise AIError("исчерпан лимит запросов munito.ai, попробую позже")
            if resp.status >= 400:
                raise AIError(f"munito.ai ответил {resp.status}: {body[:200]}")
            choice = json.loads(body)["choices"][0]
            if choice.get("finish_reason") == "length" and not (choice["message"].get("content") or "").strip():
                raise AIError("модель долго рассуждала и не успела ответить — выберите DeepSeek V4.1 Flash")
            return choice["message"]["content"] or ""
    except (aiohttp.ClientError, TimeoutError) as e:
        raise AIError(f"нет связи с munito.ai: {e}") from e
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise AIError("непонятный ответ munito.ai") from e


def parse_answer(text: str) -> dict:
    """JSON из ответа модели (иногда она оборачивает его в ```json … ``` или добавляет рассуждения)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AIError("модель не вернула JSON")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError("модель вернула испорченный JSON") from e
    try:
        score = max(0, min(100, int(float(data.get("score") or 0))))
    except (TypeError, ValueError):
        score = 0
    clean = lambda k: str(data.get(k) or "").strip()[:2000]
    return {"ai_score": score, "ai_hot": int(data.get("hot") in (True, "true", 1)), "ai_summary": clean("summary"),
            "ai_price": clean("price"), "ai_days": clean("days"), "ai_risks": clean("risks"), "ai_reply": clean("reply")}


async def analyze(session: aiohttp.ClientSession, order) -> dict:
    """Разбор заказа нейросетью. Результат сохраняется в базе и возвращается. Ошибка — AIError."""
    s = settings()
    if not s["key"]:
        raise AIError("ключ munito.ai не задан — добавьте его в «Настройки → Нейросеть»")
    budget = db.rub(order["budget"]) if order["budget"] else "не указан"
    offers = "неизвестно" if order["source"].startswith(("TG ", "FL.ru")) else order["offers"]
    prompt = PROMPT.format(about=s["about"], source=order["source"], title=order["title"], budget=budget,
                           offers=offers, description=(order["description"] or "нет")[:6000])
    result = parse_answer(await chat(session, prompt, s["key"], s["model"]))
    db.save_ai(order["id"], result)
    db.set_setting("ai_error", "")
    return result


async def check_key(session: aiohttp.ClientSession, key: str, model: str) -> str:
    """Короткий запрос для проверки ключа. Возвращает ответ модели или бросает AIError."""
    return (await chat(session, "Ответь одним словом: работает", key, model, max_tokens=300)).strip()[:100]


async def safe_analyze(session: aiohttp.ClientSession, order) -> dict | None:
    try:
        return await analyze(session, order)
    except AIError as e:
        logging.warning("ИИ, заказ #%s: %s", order["id"], e)
        db.set_setting("ai_error", str(e))
        return None
