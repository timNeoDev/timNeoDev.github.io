"""Веб-админка радара: главная, заказы, сделки, финансы, аналитика, клиенты, поиск, шаблоны, настройки."""
import asyncio
import hashlib
import hmac
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

import db
import sources
import tg_user
from config import ADMIN_PASSWORD

BASE = Path(__file__).parent
COOKIE = "radar_session"
MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
FINANCE_TABS = {"forecast": "Предварительный доход", "payments": "Оплаты", "awaiting": "Ждём оплаты",
                "potential": "Потенциально", "workload": "Загрузка", "closed": "Закрыто"}

env = Environment(loader=FileSystemLoader(BASE / "templates"), autoescape=select_autoescape(["html"]))


def ago(iso: str | None) -> str:
    """Сколько прошло с момента публикации (время биржи — московское)."""
    if not iso:
        return ""
    minutes = int((sources.msk_now() - datetime.fromisoformat(iso)).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} мин назад"
    if minutes < 24 * 60:
        return f"{minutes // 60} ч назад"
    return f"{minutes // 1440} дн назад"


env.filters.update(rub=db.rub, ago=ago, month_label=lambda ym: MONTHS[int(ym[5:7]) - 1])
env.globals.update(STAGES=db.STAGES, STAGE_GROUPS=db.STAGE_GROUPS, RESPONSE_STAGES=db.RESPONSE_STAGES,
                   WORK_STAGES=db.WORK_STAGES, ORDER_STATUSES=db.ORDER_STATUSES, FINANCE_TABS=FINANCE_TABS)


def render(request: web.Request, name: str, **ctx) -> web.Response:
    ctx.update(path=request.path, today=date.today().isoformat(), search_q=request.query.get("q", ""))
    if name != "login.html":
        s = db.overview()
        ctx.setdefault("badges", {"/orders": s["new_waiting"], "/analytics": s["follow_ups"]})
    return web.Response(text=env.get_template(name).render(**ctx), content_type="text/html")


def back(request: web.Request, default: str = "/") -> web.HTTPFound:
    """Назад на страницу, с которой пришла форма (только путь — не уводим на чужой сайт)."""
    ref = urlsplit(request.headers.get("Referer", default))
    return web.HTTPFound((ref.path or default) + (f"?{ref.query}" if ref.query else ""))


def to_int(value, default: int = 0) -> int:
    try:
        return int(str(value).replace(" ", ""))
    except (TypeError, ValueError):
        return default


def same(a: str, b: str) -> bool:
    """Сравнение секретов за постоянное время. Байты — чтобы русские буквы не роняли вход."""
    return hmac.compare_digest(a.encode(), b.encode())


# ---------- Вход ----------

def session_token() -> str:
    # Токен зависит от пароля: при смене пароля старые входы перестают работать
    return hmac.new(ADMIN_PASSWORD.encode(), b"order-radar-session", hashlib.sha256).hexdigest()


@web.middleware
async def auth(request: web.Request, handler):
    if request.path == "/login" or request.path.startswith("/static/") or \
            same(request.cookies.get(COOKIE, ""), session_token()):
        return await handler(request)
    raise web.HTTPFound("/login")


async def login(request: web.Request):
    error = False
    if request.method == "POST":
        form = await request.post()
        if ADMIN_PASSWORD and same(str(form.get("password", "")), ADMIN_PASSWORD):
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, session_token(), httponly=True, samesite="Strict", max_age=30 * 86400)
            raise resp
        await asyncio.sleep(1)  # замедляем перебор паролей
        error = True
    return render(request, "login.html", error=error)


async def logout(request: web.Request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    raise resp


# ---------- Главная, аналитика, клиенты, поиск ----------

async def home(request: web.Request):
    now = sources.msk_now()
    return render(
        request, "home.html", s=db.overview(), found=db.found_by_day(14), income=db.income_by_month(6),
        follow_ups=db.follow_ups()[:6], due=db.due_deals(7),
        hot=[o for o in db.list_orders("new", limit=60) if sources.is_hot(o, now)][:5],
        last_poll=db.get_setting("last_poll"), last_error=db.get_setting("last_error"),
    )


async def analytics(request: web.Request):
    return render(request, "analytics.html", s=db.overview(), funnel=db.funnel(), by_source=db.responses_by_source(),
                  follow_ups=db.follow_ups(), categories=db.top_categories(8))


async def clients(request: web.Request):
    return render(request, "clients.html", clients=db.clients())


async def search(request: web.Request):
    q = request.query.get("q", "").strip()
    return render(request, "search.html", q=q, results=db.search(q) if q else None)


# ---------- Заказы с бирж ----------

async def orders(request: web.Request):
    view = request.query.get("view", "new")
    if view not in (*db.ORDER_STATUSES, "all"):
        view = "new"
    q = request.query.get("q", "").strip()
    budget = to_int(request.query.get("budget"))
    hot_only = request.query.get("hot") == "1"
    sort = "fresh" if request.query.get("sort") == "fresh" else "chance"
    now = sources.msk_now()
    items = [(o, sources.is_hot(o, now), sources.chance(o, now)) for o in db.list_orders(view, q=q, min_budget=budget)]
    if hot_only:
        items = [i for i in items if i[1]]
    if sort == "chance":
        items.sort(key=lambda i: -i[2])  # список из базы уже по новизне — сортировка устойчива
    return render(request, "orders.html", items=items, view=view, q=q, budget=budget, hot_only=hot_only, sort=sort,
                  chance_label=sources.chance_label)


async def order_status(request: web.Request):
    form = await request.post()
    try:
        db.set_order_status(int(form["id"]), str(form["status"]))
    except (KeyError, ValueError):
        raise web.HTTPBadRequest()
    raise back(request, "/orders")


async def order_respond(request: web.Request):
    try:
        deal_id = db.deal_from_order(int(request.match_info["id"]))
    except ValueError:
        raise web.HTTPNotFound(text="Заказ не найден — возможно, база радара была очищена")
    raise web.HTTPFound(f"/deals/{deal_id}")


# ---------- Сделки ----------

async def deals(request: web.Request):
    group = request.query.get("group", "responses")
    if group not in db.STAGE_GROUPS:
        group = "responses"
    stage = request.query.get("stage", "")
    stages = (stage,) if stage in db.STAGE_GROUPS[group][1] else db.STAGE_GROUPS[group][1]
    q = request.query.get("q", "").strip()
    client = request.query.get("client", "")
    if client:  # все сделки клиента, на любом этапе
        stages = ()
    all_deals = db.list_deals()
    counts = {k: sum(d["stage"] == k for d in all_deals) for k in db.STAGES}
    group_counts = {g: sum(counts[s] for s in st) for g, (_, st) in db.STAGE_GROUPS.items()}
    return render(request, "deals.html", deals=db.list_deals(stages, q=q, client=client),
                  group=group, stage=stage, counts=counts, group_counts=group_counts, q=q, client=client)


async def deal_page(request: web.Request):
    did = request.match_info.get("id")
    deal = db.get_deal(int(did)) if did else None
    if did and not deal:
        raise web.HTTPNotFound(text="Сделка не найдена")
    error = ""
    if request.method == "POST":
        form = dict(await request.post())
        try:
            new_id = db.save_deal(form, int(did) if did else None)
            raise web.HTTPFound(f"/deals/{new_id}")
        except ValueError:
            error = "Проверьте поля: нужно название, суммы — числами, даты — в формате ГГГГ-ММ-ДД."
            deal = {**(dict(deal) if deal else {"paid": 0}), **form}
    return render(request, "deal.html", d=deal, payments=db.list_payments(int(did)) if did else [],
                  error=error, is_new=not did, accounts={a["exchange"]: a for a in db.list_accounts()})


async def deal_stage(request: web.Request):
    form = await request.post()
    try:
        db.set_stage(int(request.match_info["id"]), str(form["stage"]))
    except (KeyError, ValueError):
        raise web.HTTPBadRequest()
    raise back(request, "/deals")


async def deal_delete(request: web.Request):
    db.delete_deal(int(request.match_info["id"]))
    raise web.HTTPFound("/deals")


async def payment_add(request: web.Request):
    did = int(request.match_info["id"])
    form = await request.post()
    try:
        db.add_payment(did, to_int(form.get("amount")), str(form.get("paid_on") or date.today().isoformat()),
                       str(form.get("note", "")))
    except ValueError:
        raise web.HTTPBadRequest(text="Сумма должна быть больше нуля, дата — в формате ГГГГ-ММ-ДД")
    raise web.HTTPFound(f"/deals/{did}")


async def payment_delete(request: web.Request):
    db.delete_payment(int(request.match_info["id"]))
    raise back(request, "/finance?tab=payments")


async def old_project_link(request: web.Request):
    """Ссылки /projects/N из старых сообщений бота ведут на сделку."""
    raise web.HTTPFound(f"/deals/{request.match_info['id']}")


# ---------- Финансы ----------

async def finance(request: web.Request):
    tab = request.query.get("tab", "forecast")
    if tab not in FINANCE_TABS:
        tab = "forecast"
    all_deals = db.list_deals()
    ctx = {
        "awaiting": [d for d in all_deals if d["stage"] in db.WORK_STAGES and d["paid"] < d["price"]],
        "potential": [d for d in all_deals if d["stage"] in db.RESPONSE_STAGES],
        "workload": [d for d in all_deals if d["stage"] in ("ready", "work", "revisions", "review")],
        "closed": [d for d in all_deals if d["stage"] in db.FINAL_STAGES],
    }
    return render(request, "finance.html", tab=tab, s=db.overview(), income=db.income_by_month(12),
                  payments=db.list_payments() if tab == "payments" else [], **ctx)


# ---------- Шаблоны ----------

async def templates_page(request: web.Request):
    if request.method == "POST":
        form = await request.post()
        try:
            db.save_template(str(form.get("title", "")), str(form.get("body", "")), to_int(form.get("id")) or None)
        except ValueError:
            raise web.HTTPBadRequest(text="Заполните название и текст шаблона")
        raise web.HTTPFound("/templates")
    return render(request, "templates.html", templates=db.list_templates())


async def template_delete(request: web.Request):
    db.delete_template(int(request.match_info["id"]))
    raise web.HTTPFound("/templates")


# ---------- Настройки и аккаунты бирж ----------

async def settings(request: web.Request):
    api_id, api_hash = tg_user.credentials()
    return render(request, "settings.html", plus=db.get_words("plus"), minus=db.get_words("minus"),
                  accounts=db.list_accounts(), exchanges=db.EXCHANGES, s=db.overview(),
                  tg_sources=db.list_tg_sources(), tg_kinds=db.TG_KINDS,
                  tg_name=await tg_user.account_name(), tg_step=tg_user.login_step(),
                  tg_api_id=api_id or "", tg_api_hash_set=bool(api_hash), tg_msg=request.query.get("tg_msg", ""),
                  tg_error=request.query.get("tg_error", ""),
                  follow_up_days=db.int_setting("follow_up_days"), min_budget=db.min_budget(),
                  last_poll=db.get_setting("last_poll"), last_error=db.get_setting("last_error"))


async def words_add(request: web.Request):
    form = await request.post()
    kind = str(form.get("kind"))
    if kind not in db.FILTER_KINDS:
        raise web.HTTPBadRequest()
    for w in str(form.get("words", "")).split(","):
        if w := sources.clean_word(w):
            db.add_word(kind, w)
    db.rematch_new_orders()
    raise web.HTTPFound("/settings")


async def words_delete(request: web.Request):
    form = await request.post()
    db.delete_word(str(form.get("kind")), str(form.get("word")))
    db.rematch_new_orders()
    raise web.HTTPFound("/settings")


async def general_save(request: web.Request):
    form = await request.post()
    for key in db.SETTINGS_DEFAULTS:
        if key in form:
            db.set_setting(key, max(to_int(form[key]), 0))
    db.rematch_new_orders()  # мог измениться минимальный бюджет
    raise web.HTTPFound("/settings")


async def account_action(request: web.Request):
    form = await request.post()
    exchange, action = str(form.get("exchange")), str(form.get("action"))
    if exchange not in db.EXCHANGES:
        raise web.HTTPBadRequest()
    if action == "enable":
        db.set_account_enabled(exchange, True)
    elif action == "disable":
        db.set_account_enabled(exchange, False)
    elif action == "connect":
        try:
            db.connect_account(exchange, str(form.get("login", "")), str(form.get("secret", "")))
        except ValueError:
            raise web.HTTPBadRequest(text="Вставьте данные входа")
    elif action == "disconnect":
        db.disconnect_account(exchange)
    elif action == "price":
        db.set_response_price(exchange, to_int(form.get("response_price")))
    else:
        raise web.HTTPBadRequest()
    raise web.HTTPFound("/settings#accounts")


async def tg_action(request: web.Request):
    form = await request.post()
    action = str(form.get("action"))
    if action == "add":
        username = sources.tg_username(str(form.get("username", "")))
        if not username:
            raise web.HTTPFound("/settings?tg_error=1#telegram")
        try:
            db.add_tg_source(username, str(form.get("kind", "channel")))
        except ValueError:
            raise web.HTTPBadRequest(text="Неизвестный тип источника")
    elif action in ("enable", "disable"):
        db.set_tg_source(str(form.get("username")), enabled=int(action == "enable"))
    elif action == "delete":
        db.delete_tg_source(str(form.get("username")))
    else:
        raise web.HTTPBadRequest()
    raise web.HTTPFound("/settings#telegram")


async def tg_account(request: web.Request):
    """Подключение отдельного Telegram-аккаунта для парсинга: код → пароль → готово; или отключение."""
    from telethon.errors import RPCError
    form = await request.post()
    action, msg = str(form.get("action")), ""
    try:
        if action == "send_code":
            api_hash = str(form.get("api_hash", "")) or tg_user.credentials()[1]  # пусто — оставить сохранённый
            await tg_user.send_code(str(form.get("api_id", "")), api_hash, str(form.get("phone", "")))
            msg = "Код отправлен в Telegram на этот номер"
        elif action == "code":
            msg = "Аккаунт подключён" if await tg_user.verify_code(str(form.get("code", ""))) \
                else "Включена двухэтапная защита — введите пароль"
        elif action == "password":
            await tg_user.verify_password(str(form.get("password", "")))
            msg = "Аккаунт подключён"
        elif action == "cancel":
            await tg_user.cancel_login()
        elif action == "logout":
            await tg_user.logout()
            msg = "Аккаунт отключён, сессия завершена"
        else:
            raise web.HTTPBadRequest()
    except (ValueError, RPCError, ConnectionError) as e:
        msg = f"Ошибка: {e}"
    raise web.HTTPFound(f"/settings?tg_msg={quote(msg)}#tg-account")


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth])
    app.add_routes([
        web.get("/login", login), web.post("/login", login), web.post("/logout", logout),
        web.get("/", home), web.get("/analytics", analytics), web.get("/clients", clients), web.get("/search", search),
        web.get("/orders", orders), web.post("/orders/status", order_status),
        web.post("/orders/{id:\\d+}/respond", order_respond),
        web.get("/deals", deals),
        web.get("/deals/new", deal_page), web.post("/deals/new", deal_page),
        web.get("/deals/{id:\\d+}", deal_page), web.post("/deals/{id:\\d+}", deal_page),
        web.post("/deals/{id:\\d+}/stage", deal_stage), web.post("/deals/{id:\\d+}/delete", deal_delete),
        web.post("/deals/{id:\\d+}/payments", payment_add), web.post("/payments/{id:\\d+}/delete", payment_delete),
        web.get("/projects/{id:\\d+}", old_project_link),
        web.get("/finance", finance),
        web.get("/templates", templates_page), web.post("/templates", templates_page),
        web.post("/templates/{id:\\d+}/delete", template_delete),
        web.get("/settings", settings), web.post("/settings/words", words_add),
        web.post("/settings/words/delete", words_delete), web.post("/settings/general", general_save),
        web.post("/accounts", account_action), web.post("/tg", tg_action), web.post("/tg/account", tg_account),
    ])
    app.router.add_static("/static/", BASE / "static")
    return app
