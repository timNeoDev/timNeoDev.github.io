"""Работа с Claude API: системный промпт, отправка запроса и учёт расходов."""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import anthropic
from anthropic import AsyncAnthropic

from config import BUSINESS_NAME, PHONE

MODEL = "claude-opus-5-5"
# Метка, которой модель просит бота показать кнопку бронирования
BOOK_TAG = "[BOOK]"

KNOWLEDGE = (Path(__file__).parent / "knowledge.md").read_text(encoding="utf-8")

SYSTEM_PROMPT = f"""Ты — дружелюбный ИИ-консультант кофейни {BUSINESS_NAME} в Telegram. \
Ты отвечаешь гостям на вопросы о кофейне: меню, цены, состав, режим работы, адрес, доставка, \
бронирование, программа лояльности. Помогаешь выбрать напиток или десерт под настроение и вкус.

Как отвечать:
- Пиши по-русски, тепло и по-человечески, как опытный бариста. Обращайся на «вы».
- Коротко: обычно 1–4 предложения. Списки — только если гость просит перечислить, например всё меню.
- Только обычный текст: без Markdown, звёздочек, решёток и HTML. Эмодзи — не больше одного-двух.
- Опирайся только на базу знаний ниже. Если ответа в ней нет — честно скажи, что не знаешь, \
и предложи позвонить по телефону {PHONE}. Не выдумывай цены, блюда и акции.
- Если вопрос не про кофейню, вежливо ответь одной фразой и верни разговор к кофе.
- Жалобы и сложные ситуации: извинись, попроси подробности и дай телефон {PHONE}.

Бронирование:
- Если гость хочет забронировать столик или спрашивает, как это сделать, скажи, что это можно \
сделать кнопкой ниже, и добавь в самом конце ответа метку {BOOK_TAG}. Сам данные для брони не собирай.

База знаний кофейни:
<knowledge>
{KNOWLEDGE}
</knowledge>"""

# Цены Claude Opus 5.5 в долларах за 1 млн токенов — для примерной оценки расходов
PRICE_INPUT = 4.00
PRICE_CACHE_WRITE = 5.00
PRICE_CACHE_READ = 0.20
PRICE_OUTPUT = 20.00

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic()  # ключ берётся из переменной окружения ANTHROPIC_API_KEY
    return _client


@dataclass
class Reply:
    text: str
    wants_booking: bool
    refused: bool
    cost_usd: float


class AIUnavailable(Exception):
    """Claude API временно недоступен (сеть, лимиты, ошибка сервера)."""


def clean(text: str) -> tuple[str, bool]:
    """Убирает метку бронирования (и её недописанный хвост при стриминге)."""
    wants_booking = BOOK_TAG in text
    text = text.replace(BOOK_TAG, "")
    for i in range(len(BOOK_TAG) - 1, 0, -1):  # «[», «[B», «[BO»... в конце потока
        if text.endswith(BOOK_TAG[:i]):
            text = text[:-i]
            break
    return text.strip(), wants_booking


def estimate_cost(usage) -> float:
    return (
        (usage.input_tokens or 0) * PRICE_INPUT
        + (usage.cache_creation_input_tokens or 0) * PRICE_CACHE_WRITE
        + (usage.cache_read_input_tokens or 0) * PRICE_CACHE_READ
        + (usage.output_tokens or 0) * PRICE_OUTPUT
    ) / 1_000_000


async def ask(history: list[dict], on_update: Callable[[str], Awaitable[None]]) -> Reply:
    """Отправляет диалог в Claude и стримит ответ: on_update получает накопленный текст."""
    accumulated = ""
    try:
        async with get_client().beta.messages.stream(
            model=MODEL,
            max_tokens=4000,
            # Для чата хватает низкого уровня размышлений: быстрее и дешевле
            output_config={"effort": "low"},
            # Если модель откажет по соображениям безопасности, API сам повторит запрос на запасной модели
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            # Кэшируем системный промпт с базой знаний и историю диалога — повторные запросы дешевле
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            cache_control={"type": "ephemeral"},
            messages=history,
        ) as stream:
            async for chunk in stream.text_stream:
                accumulated += chunk
                await on_update(accumulated)
            final = await stream.get_final_message()
    except anthropic.AuthenticationError:
        logging.error("Неверный ANTHROPIC_API_KEY")
        raise AIUnavailable
    except anthropic.RateLimitError:
        logging.warning("Claude API: превышен лимит запросов")
        raise AIUnavailable
    except anthropic.APIStatusError as e:
        logging.error("Claude API: ошибка %s: %s", e.status_code, e.message)
        raise AIUnavailable
    except anthropic.APIConnectionError:
        logging.error("Claude API: нет соединения")
        raise AIUnavailable

    cost = estimate_cost(final.usage)
    if final.stop_reason == "refusal":
        return Reply(text="", wants_booking=False, refused=True, cost_usd=cost)

    text = "".join(b.text for b in final.content if b.type == "text")
    text, wants_booking = clean(text)
    return Reply(text=text, wants_booking=wants_booking, refused=False, cost_usd=cost)
