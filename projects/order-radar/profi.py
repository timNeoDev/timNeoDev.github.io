"""Profi.ru: лента заказов специалиста. Заказы видны только после входа, поэтому работаем через cookie сессии,
которую пользователь вставляет в админке (Настройки → Аккаунты бирж). Только чтение: ничего не откликаем и не меняем.

Как устроен вход: сайт авторизует запросы коротким токеном prfr_bo_tkn (живёт ~10 минут) и продлевает его запросом
/auth/token/renew — продление работает и по недавно истёкшему токену. Радар делает то же самое и сохраняет
новый токен в базу, поэтому cookie не нужно вставлять заново, пока сессия на сайте жива (не нажимали «Выйти»)."""
import base64
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

import db

EXCHANGE = "Profi.ru"
BASE = "https://profi.ru"
ORDER_URL = BASE + "/backoffice/n.php?o={id}"
MIN_INTERVAL = 30        # Profi опрашиваем бережно: не чаще раза в 30 секунд (одна лёгкая страница ленты)
RENEW_BEFORE = 180       # продлеваем токен, если до конца осталось меньше 3 минут
TIMEOUT = aiohttp.ClientTimeout(total=30)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json", "Accept-Language": "ru-RU,ru;q=0.9",
    "x-app-id": "BO", "x-new-auth-compatible": "1", "x-requested-with": "XMLHttpRequest",
    "x-warp-ui-app": "WEBBO", "x-warp-ui-type": "WEB", "x-warp-ui-ver": "1.0",
    "Origin": BASE, "Referer": BASE + "/backoffice/n.php",
}
# Запросы ровно в том виде, в каком их шлёт сайт (первая строка — подпись запроса, без неё сервер не отвечает).
# Если Profi обновит сайт и подпись устареет, в админке появится ошибка — запрос нужно будет снять заново.
GQL_DIR = Path(__file__).with_name("profi_gql")


def _query(name: str) -> str:
    return (GQL_DIR / f"{name}.graphql").read_text(encoding="utf-8")


FEED_QUERY = _query("feed")
FEED_VARS = {"allVerticals": True, "searchQuery": "", "searchEntities": [], "pageSize": 30,
             "useSavedFilter": True, "sort": "DEFAULT", "filter": {}}  # useSavedFilter — услуги из анкеты на Profi

_last_fetch = 0.0


class ProfiError(Exception):
    pass


# ---------- cookie и токен ----------

def parse_cookie(raw: str) -> dict[str, str]:
    """Строка cookie из браузера → {имя: значение}. Понимает и вариант с «cookie:» в начале."""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:]
    pairs = {}
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            pairs[name] = value
    return pairs


def build_cookie(pairs: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def token_left(pairs: dict[str, str], now: float | None = None) -> float:
    """Сколько секунд осталось жить токену (отрицательно — уже истёк; -inf — токена нет или он битый)."""
    token = pairs.get("prfr_bo_tkn", "")
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        return payload["exp"] - (time.time() if now is None else now)
    except (IndexError, KeyError, ValueError, TypeError):
        return float("-inf")


async def _auth_step(session: aiohttp.ClientSession, step: str, pairs: dict[str, str]) -> dict[str, str]:
    """Один шаг входа сайта (/auth/token/renew или /touch). Возвращает cookie с обновлёнными значениями."""
    async with session.get(f"{BASE}/auth/token/{step}?dt={int(time.time() * 1000)}", timeout=TIMEOUT,
                           headers={**HEADERS, "Cookie": build_cookie(pairs)}, allow_redirects=False) as resp:
        if resp.status in (401, 403):
            raise ProfiError("сессия закончилась — войдите на profi.ru и вставьте cookie заново")
        if resp.status == 423:  # сайт в этот момент сам продлевает вход (например, открыт в браузере) — подождём
            raise ProfiError("profi.ru занят продлением входа, повторю через минуту")
        if resp.status != 200:
            raise ProfiError(f"продление входа ({step}): ответ {resp.status}")
        fresh = dict(pairs)
        for header in resp.headers.getall("Set-Cookie", []):
            name, _, rest = header.partition("=")
            value = rest.split(";", 1)[0]
            if name.strip() and value and value != "deleted":
                fresh[name.strip()] = value
    return fresh


async def renew(session: aiohttp.ClientSession, pairs: dict[str, str]) -> dict[str, str]:
    """Продлевает токен так же, как сайт: renew выдаёт новый токен, touch делает его рабочим.
    Новую cookie сразу сохраняем в базу — старый токен после продления уже не принимается."""
    fresh = await _auth_step(session, "renew", pairs)
    if fresh.get("prfr_bo_tkn") == pairs.get("prfr_bo_tkn"):
        raise ProfiError("продление входа не выдало новый токен")
    db.set_account_secret(EXCHANGE, build_cookie(fresh))
    fresh = await _auth_step(session, "touch", fresh)
    db.set_account_secret(EXCHANGE, build_cookie(fresh))
    return fresh


# ---------- вход по СМС (кнопка «Заменить cookie» в админке) ----------
# Повторяем шаги сайта: гостевой токен → проверка капчи → authStrategyStart (Profi шлёт СМС) →
# authStrategyUse с кодом → /auth/token/renew/?login и /touch → полноценная cookie специалиста.

LOGIN_TTL = 600          # код из СМС ждём не дольше 10 минут
_login: dict = {}        # незавершённый вход: cookie гостя, дескрипторы шага, телефон


def normalize_phone(raw: str) -> str:
    """Любая запись российского мобильного → «+7 999 123 45 67» (так номер шлёт сам сайт)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10 or digits[0] != "9":
        raise ProfiError("нужен российский мобильный номер, например +7 999 123-45-67")
    return f"+7 {digits[:3]} {digits[3:6]} {digits[6:8]} {digits[8:]}"


def mask_phone(phone: str) -> str:
    last = re.sub(r"\D", "", phone)[-2:]
    return f"+7 *** *** ** {last}"


def _merge_cookies(resp: aiohttp.ClientResponse, pairs: dict[str, str]) -> None:
    for header in resp.headers.getall("Set-Cookie", []):
        name, _, rest = header.partition("=")
        name, value = name.strip(), rest.split(";", 1)[0]
        if not name:
            continue
        if value in ("", "deleted"):
            pairs.pop(name, None)
        else:
            pairs[name] = value


async def _get(session: aiohttp.ClientSession, path: str, pairs: dict[str, str]) -> int:
    async with session.get(BASE + path, headers={**HEADERS, "Cookie": build_cookie(pairs)}, timeout=TIMEOUT,
                           allow_redirects=False) as resp:
        _merge_cookies(resp, pairs)
        return resp.status


async def _gql(session: aiohttp.ClientSession, pairs: dict[str, str], query: str, variables: dict) -> dict:
    async with session.post(f"{BASE}/graphql", json={"query": query, "variables": variables}, timeout=TIMEOUT,
                            headers={**HEADERS, "Content-Type": "application/json", "Cookie": build_cookie(pairs)}) as resp:
        _merge_cookies(resp, pairs)
        status, text = resp.status, await resp.text()
    if status != 200:
        raise ProfiError(f"вход: profi.ru ответил {status}")
    data = json.loads(text)
    if data.get("errors"):
        raise ProfiError(f"вход: {str(data['errors'][0].get('message', ''))[:150]}")
    return data.get("data") or {}


def _result_error(result: dict) -> str | None:
    """Текст ошибки шага входа (неверный код, слишком много попыток…) или None, если шаг прошёл."""
    if result.get("__typename") in ("AuthStrategyResultRetry", "AuthStrategyResultError"):
        errors = (result.get("answer") or {}).get("errors") or []
        return "; ".join(e.get("message") or e.get("code") or "" for e in errors).strip("; ") or "Profi отклонил вход"
    return None


def login_pending() -> str:
    """Замаскированный номер, если ждём код из СМС; '' — входа в процессе нет."""
    if _login and _login["until"] > time.monotonic():
        return mask_phone(_login["phone"])
    _login.clear()
    return ""


def login_cancel() -> None:
    _login.clear()


async def login_start(raw_phone: str) -> str:
    """Шаг 1: Profi отправляет СМС с кодом. Возвращает замаскированный номер. ProfiError — с понятным текстом."""
    phone = normalize_phone(raw_phone)
    pairs: dict[str, str] = {}
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            await _get(session, "/backoffice/", pairs)                                  # cookie посетителя
            await _get(session, f"/auth/token/init?dt={int(time.time() * 1000)}", pairs)  # гостевой токен
            captcha = await _gql(session, pairs, _query("auth_captcha"), {"limitGroupId": "PIN", "phoneNumber": phone})
            if (captcha.get("authCaptchaLimit") or {}).get("shouldShown"):
                raise ProfiError("Profi просит пройти капчу — войдите на profi.ru в браузере и вставьте cookie вручную, "
                                 "либо попробуйте снова через час")
            data = await _gql(session, pairs, _query("auth_start"), {
                "type": "phone",
                "initialState": {"phoneNumber": phone, "implementer": {"siteId": "1815"},
                                 "defaultOrderCityId": "prfr", "currentHost": BASE}})
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ProfiError(f"нет связи с profi.ru: {e}") from e
    except json.JSONDecodeError as e:
        raise ProfiError("profi.ru вернул непонятный ответ") from e
    start = data.get("authStrategyStart") or {}
    result = start.get("result") or {}
    if err := _result_error(result):
        raise ProfiError(err)
    step = result.get("step") or {}
    if step.get("__typename") != "AuthStrategyStepValidatePincode":
        raise ProfiError("Profi предложил другой способ входа — войдите в браузере и вставьте cookie вручную")
    strategy = start.get("strategy") or {}
    _login.clear()
    _login.update(pairs=pairs, strategy=strategy.get("strategyDescriptor"), step=strategy.get("stepDescriptor"),
                  phone=phone, until=time.monotonic() + LOGIN_TTL)
    return mask_phone(phone)


async def login_finish(raw_code: str) -> str:
    """Шаг 2: код из СМС → вход → новая cookie сохраняется в базу и сбор включается. Возвращает номер (маской)."""
    global _last_fetch
    if not login_pending():
        raise ProfiError("код устарел или вход не начат — запросите код ещё раз")
    code = re.sub(r"\D", "", raw_code)
    if not 4 <= len(code) <= 8:
        raise ProfiError("введите код из СМС — только цифры")
    pairs = _login["pairs"]
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            data = await _gql(session, pairs, _query("auth_use"), {
                "method": "submitStep", "strategyDescriptor": _login["strategy"],
                "payload": {"AuthStrategyStepValidatePin": {"pincode": code}, "stepDescriptor": _login["step"]}})
            use = data.get("authStrategyUse") or {}
            result = use.get("result") or {}
            if err := _result_error(result):
                _login["step"] = (use.get("strategy") or {}).get("stepDescriptor") or _login["step"]  # для повтора кода
                raise ProfiError(err)
            login_url = (result.get("auth") or {}).get("loginUrl")
            if not login_url or not login_url.startswith("/"):
                raise ProfiError("Profi не завершил вход — запросите код ещё раз")
            for path in (login_url, f"/auth/token/touch?dt={int(time.time() * 1000)}"):
                if await _get(session, path, pairs) != 200:
                    raise ProfiError("Profi не выдал вход после кода — запросите код ещё раз")
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ProfiError(f"нет связи с profi.ru: {e}") from e
    except json.JSONDecodeError as e:
        raise ProfiError("profi.ru вернул непонятный ответ") from e
    if not _is_implementer(pairs):
        raise ProfiError("вход выполнен, но это не аккаунт специалиста")
    phone = mask_phone(_login["phone"])
    db.connect_account(EXCHANGE, phone, build_cookie(pairs))
    db.set_setting("profi_error", "")
    _login.clear()
    _last_fetch = 0.0  # новая cookie — сразу проверим ленту
    return phone


def _is_implementer(pairs: dict[str, str]) -> bool:
    try:
        payload = json.loads(base64.urlsafe_b64decode(pairs.get("prfr_bo_tkn", "").split(".")[1] + "=="))
        return bool((payload.get("roles") or {}).get("implementer"))
    except (IndexError, ValueError, TypeError, AttributeError):
        return False


# ---------- лента ----------

PRICE_RE = re.compile(r"\d[\d\s  ]*")


def parse_price(price: dict | None) -> int:
    m = PRICE_RE.search(str((price or {}).get("value") or ""))
    return int(re.sub(r"\D", "", m.group())) if m else 0


def parse_feed(data: dict) -> list[dict]:
    """Заказы из ответа BoSearchBoardItems: обычные карточки и карточки из подборки (карусели)."""
    board = (data.get("data") or {}).get("boSearchBoardItems") or {}
    snippets = []
    for item in board.get("items") or []:
        if item.get("type") == "SNIPPET":
            snippets.append(item)
        elif item.get("type") == "CAROUSEL":
            snippets += item.get("snippets") or []
    orders, seen = [], set()
    for s in snippets:
        oid = str(s.get("id") or "")
        if not oid.isdigit() or oid in seen:
            continue
        seen.add(oid)
        budget = parse_price(s.get("price"))
        ts = s.get("lastUpdateDate")
        published = ((datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=3)).replace(tzinfo=None).isoformat()
                     if isinstance(ts, (int, float)) else None)  # время московское, как у остальных бирж
        remote = ((s.get("geo") or {}).get("remote") or {}).get("prefix") or ""
        badges = ", ".join(b["label"] for b in s.get("badges") or [] if b.get("label"))
        orders.append({
            "source": EXCHANGE, "link": ORDER_URL.format(id=oid),
            "title": str(s.get("title") or "").strip(),
            "description": str(s.get("description") or "").replace(" · ", "\n").strip(),
            "category": " · ".join(x for x in (remote, badges) if x),
            "budget": budget, "budget_max": budget, "views": 0, "offers": 0, "published_at": published,
        })
    return orders


async def fetch(_session: aiohttp.ClientSession | None = None) -> list[dict] | None:
    """Свежие заказы из ленты специалиста. None — пропуск (рано для нового опроса или аккаунт не подключён).
    ProfiError — вход сломался или сайт изменился (текст ошибки показываем в админке).
    Своя сессия без хранилища cookie: cookie Profi передаём сами и не смешиваем с запросами к другим биржам."""
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        return await _fetch(session)


async def _fetch(session: aiohttp.ClientSession) -> list[dict] | None:
    global _last_fetch
    acc = db.get_account(EXCHANGE)
    if not acc or not acc["enabled"] or not (acc["secret"] or "").strip():
        return None
    if time.monotonic() - _last_fetch < MIN_INTERVAL:
        return None
    _last_fetch = time.monotonic()
    pairs = parse_cookie(acc["secret"])
    if "prfr_bo_tkn" not in pairs:
        raise ProfiError("в cookie нет prfr_bo_tkn — скопируйте cookie со страницы ленты заказов profi.ru")
    try:
        if token_left(pairs) < RENEW_BEFORE:
            pairs = await renew(session, pairs)
        for attempt in range(2):
            async with session.post(f"{BASE}/graphql", json={"query": FEED_QUERY, "variables": FEED_VARS},
                                    timeout=TIMEOUT, headers={**HEADERS, "Cookie": build_cookie(pairs)}) as resp:
                status, text = resp.status, await resp.text()
            if status == 401 and attempt == 0:  # токен отозвали раньше срока — продлеваем и пробуем ещё раз
                pairs = await renew(session, pairs)
                continue
            if status != 200:
                raise ProfiError(f"лента: ответ {status}")
            data = json.loads(text)
            if data.get("errors"):
                message = str(data["errors"][0].get("message", ""))
                raise ProfiError("нет доступа к ленте — войдите заново и вставьте cookie" if "denied" in message.lower()
                                 else f"лента: {message[:150]}")
            return parse_feed(data)
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ProfiError(f"нет связи с profi.ru: {e}") from e
    except json.JSONDecodeError as e:
        raise ProfiError("profi.ru вернул непонятный ответ") from e
    return []
