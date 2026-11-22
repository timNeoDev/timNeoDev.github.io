"""Нейросеть munito.ai (API в формате OpenAI): разбор заказа, оценка «стоит ли браться», черновик отклика.
Ключ, модель и текст «о себе» хранятся в настройках базы и меняются в админке."""
import asyncio
import json
import logging
import re

import aiohttp

import db
import sources

API_URL = "https://api.munito.ai/inference/v1/chat/completions"
MODELS = {  # чат-модели из GET /v1/models; бесплатный лимит — запросов в минуту (OCR и pplx-decider не для текста)
    "deepseek-ai/deepseek-v4.1-flash-1017": "DeepSeek V4.1 Flash — быстрая и умная, 200 запросов/мин (рекомендую)",
    "deepseek-ai/deepseek-v4-flash": "DeepSeek V4 Flash — прошлая версия, 10 запросов/мин",
    "qwen/qwen3.8-27b": "Qwen 3.8 27B — подробнее рассуждает, медленнее (~20 сек), 15 запросов/мин",
    "zai-org/glm-5.3-flash": "GLM 5.3 Flash — 10 запросов/мин",
    "aleph-alpha/kolibri-1": "Kolibri 1 (Aleph Alpha) — 40 запросов/мин",
    "thinkingmachines/inkling-2-mini": "Inkling 2 Mini — 15 запросов/мин",
    "google/gemma-4-12b": "Gemma 4 12B — маленькая, проще в рассуждениях, 20 запросов/мин",
}
DEFAULT_MODEL = next(iter(MODELS))
DEFAULT_ABOUT = ("Тим, фрилансер. Делаю Telegram-боты и Mini Apps, лендинги, сайты и веб-приложения, парсеры, "
                 "скрипты, интеграции с API и автоматизацию. Портфолио: https://timneodev.github.io/ — в нём лендинг "
                 "кофейни, Telegram-бот записи клиентов (https://t.me/zayavki_demo_pro_bot) и ИИ-консультант для бизнеса "
                 "в Telegram. Ещё есть свой парсер заказов с бирж с веб-админкой и Telegram-ботом.")
# Как фрилансер работает — от этого зависит, что ему по силам и сколько времени займёт заказ
WORK_STYLE = """Как он работает: разработку ведёт вместе с ИИ-агентом Claude Code («вайбкодинг»). Код пишет ИИ, а фрилансер
ставит задачу, разбирается в требованиях, проверяет и тестирует результат, вносит правки, разворачивает проект и общается с заказчиком.
Поэтому при оценке:
- по силам почти любая типовая разработка, даже на незнакомом стеке: сайты, лендинги, веб-приложения, Telegram-боты и Mini Apps,
  парсеры, скрипты, интеграции API и платёжек, автоматизация (n8n, Google Таблицы, CRM), доработка небольших проектов;
- хуже подходят: нативные мобильные приложения с публикацией в сторах, вёрстка «пиксель в пиксель» сложного дизайна,
  поддержка большого старого кода, 1С, железо и встраиваемые системы, работа с выездом; совсем не подходят чистый
  дизайн, SEO, тексты, реклама, наём в штат — за такие ставь низкую оценку;
- срок считай с учётом ИИ — примерно в 2 раза быстрее обычного, но закладывай время на тесты, правки и согласование;
- цену называй рыночную, за результат, не занижай из-за скорости;
- главный риск таких заказов — неясное ТЗ, нужен доступ к чужим серверам/аккаунтам, очень большой объём или сжатый срок.
В отклике заказчику ИИ, нейросети и вайбкодинг не упоминай — пиши от лица разработчика, который берёт задачу и отвечает за результат."""

PROMPT = """Ты помогаешь фрилансеру отбирать заказы и писать отклики. О фрилансере:
{about}

{work_style}

Заказ с биржи {source}:
Название: {title}
Бюджет: {budget}
Откликов уже: {offers}
Описание:
{description}

Ответь ТОЛЬКО JSON-объектом без пояснений, на русском:
{{"score": целое число от 1 до 10 — насколько заказ подходит фрилансеру и стоит ли за него браться (навыки, реальность бюджета, ясность задачи); 8-10 — отличный заказ, 5-7 — можно, 1-4 — лучше пропустить,
 "hot": true — если заказ стоит взять прямо сейчас (подходит по навыкам, адекватный бюджет, понятная задача), иначе false,
 "summary": "что нужно сделать, 1-2 коротких предложения",
 "price": "реалистичная цена в рублях, например «8–12 тыс. ₽»",
 "days": "реалистичный срок, например «3–5 дней»",
 "risks": "главный риск или подводный камень, коротко; пусто, если нет",
 "reply": "отклик заказчику от первого лица: 4-6 предложений, до 900 символов, без шаблонных фраз, начни с сути его задачи, предложи как сделаешь, назови срок и цену, задай 1 уточняющий вопрос, дай ссылку на портфолио. Не выдумывай опыт и кейсы: упоминай только проекты из описания фрилансера. Не пиши, что ты начинающий."}}"""


class AIError(Exception):
    pass


def settings() -> dict:
    return {"key": db.get_setting("ai_key"), "model": db.get_setting("ai_model") or DEFAULT_MODEL,
            "about": db.get_setting("ai_about") or DEFAULT_ABOUT, "enabled": db.get_setting("ai_enabled") == "1"}


def looks_like_key(key: str) -> bool:
    """Ключ API — одна строка без пробелов, «;» и «=» (так отсекаем cookie, которые браузер подставляет в поле пароля)."""
    return 10 <= len(key) <= 300 and not re.search(r"[\s;=]", key)


def enabled() -> bool:
    s = settings()
    return s["enabled"] and bool(s["key"])


class _Busy(AIError):
    """munito перегружен (503) или не ответил вовремя — такое бывает на бесплатном тарифе, стоит повторить."""


async def chat(session: aiohttp.ClientSession, prompt: str, key: str, model: str, max_tokens: int = 4000,
               timeout: float = 90, retries: int = 1) -> str:
    """Модели munito сначала «думают» (reasoning, до ~2000 токенов), потом отвечают — лимит с запасом на оба.
    Если munito перегружен или не успел ответить, повторяем через несколько секунд (retries раз)."""
    for attempt in range(retries + 1):
        try:
            return await _chat_once(session, prompt, key, model, max_tokens, timeout)
        except _Busy:
            if attempt == retries:
                raise
            await asyncio.sleep(4)
    raise AIError("munito.ai не ответил")  # недостижимо: цикл либо вернул ответ, либо пробросил ошибку


async def _chat_once(session, prompt: str, key: str, model: str, max_tokens: int, timeout: float) -> str:
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.4, "max_tokens": max_tokens}
    try:
        async with session.post(API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=timeout),
                                headers={"Authorization": f"Bearer {key}"}) as resp:
            body = await resp.text()
            if resp.status == 401:
                raise AIError("неверный ключ API")
            if resp.status == 429:
                raise AIError("исчерпан лимит запросов munito.ai, попробую позже")
            if resp.status in (502, 503, 504):
                raise _Busy("munito.ai перегружен (бесплатный тариф) — попробуйте через минуту")
            if resp.status >= 400:
                raise AIError(f"munito.ai ответил {resp.status}: {body[:200]}")
            choice = json.loads(body)["choices"][0]
            if choice.get("finish_reason") == "length" and not (choice["message"].get("content") or "").strip():
                raise AIError("модель долго рассуждала и не успела ответить — выберите DeepSeek V4.1 Flash")
            return choice["message"]["content"] or ""
    except TimeoutError as e:
        raise _Busy(f"munito.ai не ответил за {int(timeout)} сек — бесплатный тариф перегружен, попробуйте позже") from e
    except aiohttp.ClientError as e:
        raise AIError(f"нет связи с munito.ai: {e or type(e).__name__}") from e
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
        raw = float(data.get("score") or 0)
        # модель отвечает по шкале 1–10; храним 0–100 (так считается «шанс»), показываем снова из 10
        score = max(0, min(100, int(round(raw * 10 if raw <= 10 else raw))))
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
    offers = "неизвестно" if order["source"].startswith(sources.UNKNOWN_OFFERS) else order["offers"]
    prompt = PROMPT.format(about=s["about"], work_style=WORK_STYLE, source=order["source"], title=order["title"], budget=budget,
                           offers=offers, description=(order["description"] or "нет")[:6000])
    result = parse_answer(await chat(session, prompt, s["key"], s["model"]))
    db.save_ai(order["id"], result)
    db.set_setting("ai_error", "")
    return result


ADVICE_PROMPT = """Ты опытный фрилансер-наставник. Помоги коллеге решить, брать ли заказ, и как его выиграть. О нём:
{about}

{work_style}

Заказ с биржи {source}:
Название: {title}
Бюджет: {budget}
Откликов уже: {offers}
Описание:
{description}

Дай короткий практичный совет на русском, без вступлений, ровно в таком виде (5 строк, каждая с эмодзи):
🎯 Брать или нет: да/нет/осторожно — и почему, одним предложением.
💪 На что надавить в отклике: чем зацепить именно этого заказчика.
❓ Что спросить у заказчика: 1-2 главных вопроса, которые снимут неясности.
💵 Как назвать цену: стратегия (фикс, этапы, предоплата) и примерная сумма.
⚠️ Подвох: на что обратить внимание, чтобы не попасть на лишнюю работу или без оплаты.
Пиши конкретно про этот заказ, до 700 символов всего. Не выдумывай опыт, которого нет в описании фрилансера."""


async def advise(session: aiohttp.ClientSession, order) -> str:
    """Совет «как взять этот заказ». Сохраняется в базе (ai_advice) и возвращается. Ошибка — AIError."""
    s = settings()
    if not s["key"]:
        raise AIError("ключ munito.ai не задан — добавьте его в «Настройки → Нейросеть»")
    budget = db.rub(order["budget"]) if order["budget"] else "не указан"
    offers = "неизвестно" if order["source"].startswith(sources.UNKNOWN_OFFERS) else order["offers"]
    prompt = ADVICE_PROMPT.format(about=s["about"], work_style=WORK_STYLE, source=order["source"], title=order["title"],
                                  budget=budget, offers=offers, description=(order["description"] or "нет")[:6000])
    text = re.sub(r"<think>.*?</think>", "", await chat(session, prompt, s["key"], s["model"]), flags=re.S).strip()
    if not text:
        raise AIError("модель вернула пустой совет — попробуйте ещё раз")
    text = text[:1500]
    db.save_ai(order["id"], {"ai_advice": text})
    db.set_setting("ai_error", "")
    return text


REWRITE_MODES = {
    "short": "Сделай его короче: 2-3 предложения, до 400 символов. Оставь суть: как сделаешь, срок, цену и вопрос.",
    "long": "Сделай его подробнее: 6-8 предложений, до 1300 символов. Распиши по шагам, как сделаешь, что получит "
            "заказчик, срок и цену, задай 1-2 уточняющих вопроса.",
}
ORDER_CONTEXT = """О фрилансере:
{about}

{work_style}

Заказ с биржи {source}:
Название: {title}
Бюджет: {budget}
Описание:
{description}"""


def _order_context(s: dict, order) -> str:
    budget = db.rub(order["budget"]) if order["budget"] else "не указан"
    return ORDER_CONTEXT.format(about=s["about"], work_style=WORK_STYLE, source=order["source"], title=order["title"],
                                budget=budget, description=(order["description"] or "нет")[:6000])


def _clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip().strip('"«»').strip()
    if not text:
        raise AIError("модель вернула пустой ответ — попробуйте ещё раз")
    return text[:2000]


async def rewrite(session: aiohttp.ClientSession, order, mode: str) -> str:
    """Переписать отклик короче или подробнее. Сохраняет новый отклик (ai_reply) и возвращает его."""
    s = settings()
    if not s["key"]:
        raise AIError("ключ munito.ai не задан — добавьте его в «Настройки → Нейросеть»")
    if mode not in REWRITE_MODES:
        raise AIError("неизвестный режим")
    current = (order["ai_reply"] or "").strip()
    task = (f"Вот отклик заказчику:\n{current}\n\n{REWRITE_MODES[mode]}" if current else
            f"Напиши отклик заказчику от первого лица. {REWRITE_MODES[mode]}")
    prompt = (f"{_order_context(s, order)}\n\n{task}\nДай ссылку на портфолио, не выдумывай опыт и кейсы, "
              "не упоминай ИИ и нейросети. Ответь только текстом отклика, без пояснений.")
    text = _clean(await chat(session, prompt, s["key"], s["model"]))
    db.save_ai(order["id"], {"ai_reply": text})
    return text


async def answer_client(session: aiohttp.ClientSession, order, question: str) -> str:
    """Ответ на вопрос заказчика из чата биржи — с учётом заказа. Сохраняет вопрос и ответ в базе."""
    s = settings()
    if not s["key"]:
        raise AIError("ключ munito.ai не задан — добавьте его в «Настройки → Нейросеть»")
    question = question.strip()[:2000]
    if not question:
        raise AIError("вставьте вопрос заказчика")
    prompt = (f"{_order_context(s, order)}\n\nЗаказчик написал фрилансеру в чате биржи:\n«{question}»\n\n"
              "Напиши ответ от лица фрилансера: вежливо, по делу, конкретно, 2-5 предложений. Если вопрос про цену "
              "или срок — назови реалистичные цифры. Если чего-то не хватает — задай уточняющий вопрос. "
              "Не выдумывай опыт, не упоминай ИИ и нейросети, не уводи общение за пределы биржи. "
              "Ответь только текстом сообщения.")
    text = _clean(await chat(session, prompt, s["key"], s["model"]))
    db.save_ai(order["id"], {"ai_question": question, "ai_answer": text})
    return text


async def check_key(session: aiohttp.ClientSession, key: str, model: str) -> str:
    """Короткий запрос для проверки ключа. Возвращает ответ модели или бросает AIError."""
    return (await chat(session, "Ответь одним словом: работает", key, model, max_tokens=300,
                       timeout=25, retries=0)).strip()[:100]


async def safe_analyze(session: aiohttp.ClientSession, order) -> dict | None:
    try:
        return await analyze(session, order)
    except AIError as e:
        logging.warning("ИИ, заказ #%s: %s", order["id"], e)
        db.set_setting("ai_error", str(e))
        return None
