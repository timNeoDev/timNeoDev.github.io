"""Веб-админка радара: обзор, заказы, CRM, финансы, шаблоны, настройки. Вход по паролю из .env."""
import asyncio
import hashlib
import hmac
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

import db
import sources
from config import ADMIN_PASSWORD

BASE = Path(__file__).parent
COOKIE = "radar_session"
MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

env = Environment(loader=FileSystemLoader(BASE / "templates"), autoescape=select_autoescape(["html"]))


def rub(n) -> str:
    return f"{int(n or 0):,}".replace(",", " ") + " ₽"


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


def month_label(ym: str) -> str:
    return MONTHS[int(ym[5:7]) - 1]


env.filters.update(rub=rub, ago=ago, month_label=month_label)
env.globals.update(STAGES=db.STAGES, ORDER_STATUSES=db.ORDER_STATUSES, ACTIVE_STAGES=db.ACTIVE_STAGES)


def render(request: web.Request, name: str, **ctx) -> web.Response:
    ctx.setdefault("path", request.path)
    ctx["new_waiting"] = db.overview()["new_waiting"] if name != "login.html" else 0
    ctx["today"] = date.today().isoformat()
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


# ---------- Вход ----------

def session_token() -> str:
    # Токен зависит от пароля: при смене пароля старые входы перестают работать
    return hmac.new(ADMIN_PASSWORD.encode(), b"order-radar-session", hashlib.sha256).hexdigest()


@web.middleware
async def auth(request: web.Request, handler):
    if request.path == "/login" or request.path.startswith("/static/") or \
            hmac.compare_digest(request.cookies.get(COOKIE, ""), session_token()):
        return await handler(request)
    raise web.HTTPFound("/login")


async def login(request: web.Request):
    error = False
    if request.method == "POST":
        form = await request.post()
        if ADMIN_PASSWORD and hmac.compare_digest(str(form.get("password", "")), ADMIN_PASSWORD):
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


# ---------- Обзор ----------

async def dashboard(request: web.Request):
    return render(
        request, "dashboard.html",
        s=db.overview(), income=db.income_by_month(6), found=db.found_by_day(14),
        categories=db.top_categories(6), funnel=db.funnel(), due=db.due_projects(7),
        hot=[o for o in db.list_orders("new", limit=60) if sources.is_hot(o, sources.msk_now())][:5],
        last_poll=db.get_setting("last_poll"), last_error=db.get_setting("last_error"),
    )


# ---------- Заказы с бирж ----------

async def orders(request: web.Request):
    view = request.query.get("view", "new")
    if view not in (*db.ORDER_STATUSES, "all"):
        view = "new"
    q = request.query.get("q", "").strip()
    budget = to_int(request.query.get("budget"))
    hot_only = request.query.get("hot") == "1"
    now = sources.msk_now()
    items = [(o, sources.is_hot(o, now)) for o in db.list_orders(view, q=q, min_budget=budget)]
    if hot_only:
        items = [(o, h) for o, h in items if h]
    return render(request, "orders.html", items=items, view=view, q=q, budget=budget, hot_only=hot_only)


async def order_status(request: web.Request):
    form = await request.post()
    try:
        db.set_order_status(int(form["id"]), str(form["status"]))
    except (KeyError, ValueError):
        raise web.HTTPBadRequest()
    raise back(request, "/orders")


async def order_to_crm(request: web.Request):
    project_id = db.project_from_order(int(request.match_info["id"]))
    raise web.HTTPFound(f"/projects/{project_id}")


# ---------- CRM ----------

async def projects(request: web.Request):
    stage = request.query.get("stage", "")
    if stage and stage not in db.STAGES:
        stage = ""
    all_projects = db.list_projects()
    counts = {k: sum(p["stage"] == k for p in all_projects) for k in db.STAGES}
    shown = [p for p in all_projects if not stage or p["stage"] == stage]
    return render(request, "projects.html", projects=shown, stage=stage, counts=counts, total=len(all_projects))


async def project_page(request: web.Request):
    pid = request.match_info.get("id")
    project = db.get_project(int(pid)) if pid else None
    if pid and not project:
        raise web.HTTPNotFound()
    error = ""
    if request.method == "POST":
        form = dict(await request.post())
        try:
            new_id = db.save_project(form, int(pid) if pid else None)
            raise web.HTTPFound(f"/projects/{new_id}")
        except ValueError:
            error = "Заполните название проекта и проверьте этап."
            project = form
    payments = db.list_payments(int(pid)) if pid else []
    return render(request, "project.html", p=project, payments=payments, error=error, is_new=not pid)


async def project_stage(request: web.Request):
    form = await request.post()
    try:
        db.set_stage(int(request.match_info["id"]), str(form["stage"]))
    except (KeyError, ValueError):
        raise web.HTTPBadRequest()
    raise back(request, "/projects")


async def project_delete(request: web.Request):
    db.delete_project(int(request.match_info["id"]))
    raise web.HTTPFound("/projects")


async def payment_add(request: web.Request):
    pid = int(request.match_info["id"])
    form = await request.post()
    try:
        db.add_payment(pid, to_int(form.get("amount")), str(form.get("paid_on") or date.today().isoformat()),
                       str(form.get("note", "")))
    except ValueError:
        raise web.HTTPBadRequest(text="Сумма должна быть больше нуля, дата — в формате ГГГГ-ММ-ДД")
    raise web.HTTPFound(f"/projects/{pid}")


async def payment_delete(request: web.Request):
    form = await request.post()
    db.delete_payment(int(request.match_info["id"]))
    raise web.HTTPFound(f"/projects/{to_int(form.get('project_id'))}")


# ---------- Финансы ----------

async def finance(request: web.Request):
    all_projects = db.list_projects()
    return render(
        request, "finance.html", s=db.overview(), income=db.income_by_month(12),
        unpaid=[p for p in all_projects if p["stage"] in db.ACTIVE_STAGES and p["paid"] < p["price"]],
        categories=db.top_categories(8),
    )


# ---------- Шаблоны откликов ----------

async def templates_page(request: web.Request):
    if request.method == "POST":
        form = await request.post()
        try:
            db.save_template(str(form.get("title", "")), str(form.get("body", "")),
                             to_int(form.get("id")) or None)
        except ValueError:
            raise web.HTTPBadRequest(text="Заполните название и текст шаблона")
        raise web.HTTPFound("/templates")
    return render(request, "templates.html", templates=db.list_templates())


async def template_delete(request: web.Request):
    db.delete_template(int(request.match_info["id"]))
    raise web.HTTPFound("/templates")


# ---------- Настройки ----------

async def settings(request: web.Request):
    return render(request, "settings.html", plus=db.get_words("plus"), minus=db.get_words("minus"),
                  min_budget=db.min_budget(), sources=[name for name, _ in sources.SOURCES],
                  last_poll=db.get_setting("last_poll"), last_error=db.get_setting("last_error"))


async def words_add(request: web.Request):
    form = await request.post()
    kind = str(form.get("kind"))
    if kind not in db.FILTER_KINDS:
        raise web.HTTPBadRequest()
    for w in str(form.get("words", "")).split(","):
        if w := sources.clean_word(w):
            db.add_word(kind, w)
    raise web.HTTPFound("/settings")


async def words_delete(request: web.Request):
    form = await request.post()
    db.delete_word(str(form.get("kind")), str(form.get("word")))
    raise web.HTTPFound("/settings")


async def budget_save(request: web.Request):
    form = await request.post()
    db.set_setting("min_budget", max(to_int(form.get("min_budget")), 0))
    raise web.HTTPFound("/settings")


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth])
    app.add_routes([
        web.get("/login", login), web.post("/login", login), web.post("/logout", logout),
        web.get("/", dashboard),
        web.get("/orders", orders), web.post("/orders/status", order_status),
        web.post("/orders/{id:\\d+}/crm", order_to_crm),
        web.get("/projects", projects),
        web.get("/projects/new", project_page), web.post("/projects/new", project_page),
        web.get("/projects/{id:\\d+}", project_page), web.post("/projects/{id:\\d+}", project_page),
        web.post("/projects/{id:\\d+}/stage", project_stage),
        web.post("/projects/{id:\\d+}/delete", project_delete),
        web.post("/projects/{id:\\d+}/payments", payment_add),
        web.post("/payments/{id:\\d+}/delete", payment_delete),
        web.get("/finance", finance),
        web.get("/templates", templates_page), web.post("/templates", templates_page),
        web.post("/templates/{id:\\d+}/delete", template_delete),
        web.get("/settings", settings),
        web.post("/settings/words", words_add), web.post("/settings/words/delete", words_delete),
        web.post("/settings/budget", budget_save),
    ])
    app.router.add_static("/static/", BASE / "static")
    return app
