"""Веб-админка: заказы, ключевые слова, статистика. Вход по паролю из .env."""
import asyncio
import hashlib
import hmac
from html import escape
from urllib.parse import urlsplit

from aiohttp import web

import db
import feeds
from config import ADMIN_PASSWORD

COOKIE = "radar_session"
TABS = {"new": "Новые", "applied": "Откликнулся", "hidden": "Скрытые", "all": "Все заказы"}


def session_token() -> str:
    # Токен зависит от пароля: при смене пароля старые входы перестают работать
    return hmac.new(ADMIN_PASSWORD.encode(), b"order-radar-session", hashlib.sha256).hexdigest()


def is_logged_in(request: web.Request) -> bool:
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), session_token())


@web.middleware
async def auth(request: web.Request, handler):
    if request.path in ("/login",) or is_logged_in(request):
        return await handler(request)
    raise web.HTTPFound("/login")


STYLE = """
:root{--bg:#0b0d12;--card:#141821;--line:#262c3d;--text:#e7e9ef;--muted:#9aa1b2;--accent:#8b6cff;--ok:#3ecf8e}
*{box-sizing:border-box;margin:0}body{font:15px/1.55 system-ui,sans-serif;background:var(--bg);color:var(--text)}
a{color:var(--accent)}.wrap{max-width:1100px;margin:0 auto;padding:24px 16px}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;gap:12px;flex-wrap:wrap}
h1{font-size:22px}.grid{display:grid;grid-template-columns:1fr 280px;gap:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:12px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}.tabs a{padding:7px 14px;border-radius:9px;
border:1px solid var(--line);text-decoration:none;color:var(--muted)}.tabs a.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.meta{color:var(--muted);font-size:13px;margin:4px 0 8px}.desc{color:#c9cdd8;font-size:14px;white-space:pre-line}
.chip{display:inline-block;background:#232840;color:#c5b8ff;border-radius:6px;padding:1px 8px;font-size:12px;margin:8px 4px 0 0}
.row{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}button,input{font:inherit;border-radius:9px;border:1px solid var(--line);
padding:7px 12px;background:#1b2030;color:var(--text)}button{cursor:pointer}button:hover{border-color:var(--accent)}
button.main{background:var(--accent);border-color:var(--accent);color:#fff}input{width:100%}
.kw{display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--line)}
.kw button{padding:2px 9px}.stat{display:flex;justify-content:space-between;padding:3px 0}.stat b{color:var(--ok)}
.login{max-width:340px;margin:15vh auto}.err{color:#ff7b7b;margin-top:10px}
@media(max-width:800px){.grid{grid-template-columns:1fr}}
"""


def page(title: str, body: str) -> web.Response:
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>
<style>{STYLE}</style></head><body>{body}</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def login_page(request: web.Request):
    error = ""
    if request.method == "POST":
        form = await request.post()
        if ADMIN_PASSWORD and hmac.compare_digest(str(form.get("password", "")), ADMIN_PASSWORD):
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, session_token(), httponly=True, samesite="Strict", max_age=30 * 86400)
            raise resp
        await asyncio.sleep(1)  # замедляем перебор паролей
        error = '<p class="err">Неверный пароль</p>'
    return page("Вход — Радар заказов", f"""
<form class="card login" method="post"><h1>📡 Радар заказов</h1><p class="meta">Введите пароль администратора</p>
<input type="password" name="password" autofocus required><div class="row"><button class="main">Войти</button></div>{error}</form>""")


async def logout(request: web.Request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    raise resp


def order_card(o) -> str:
    chips = "".join(f'<span class="chip">{escape(k)}</span>' for k in o["matched"].split(",") if k)
    buttons = {"new": [("applied", "✅ Откликнулся"), ("hidden", "🙈 Скрыть")],
               "applied": [("new", "↩ В новые")], "hidden": [("new", "↩ В новые")]}[o["status"]]
    forms = "".join(
        f'<form method="post" action="/status"><input type="hidden" name="id" value="{o["id"]}">'
        f'<input type="hidden" name="status" value="{s}"><button>{label}</button></form>'
        for s, label in buttons
    )
    published = (o["published_at"] or "")[:16].replace("T", " ")
    return f"""<div class="card"><a href="{escape(o['link'])}" target="_blank" rel="noopener"><b>{escape(o['title'])}</b></a>
<div class="meta">{escape(o['source'])} · {escape(o['category'] or 'без категории')} · {escape(published)} UTC</div>
<div class="desc">{escape((o['description'] or '')[:500])}</div>{chips}<div class="row">{forms}</div></div>"""


async def index(request: web.Request):
    view = request.query.get("view", "new")
    if view not in TABS:
        view = "new"
    tabs = "".join(f'<a href="/?view={k}" class="{"on" if k == view else ""}">{v}</a>' for k, v in TABS.items())
    orders = db.list_orders(view)
    cards = "".join(order_card(o) for o in orders) or '<div class="card meta">Здесь пока пусто.</div>'
    s = db.stats()
    keywords = "".join(
        f'<div class="kw"><span>{escape(w)}</span><form method="post" action="/keywords/delete">'
        f'<input type="hidden" name="word" value="{escape(w)}"><button title="Удалить">×</button></form></div>'
        for w in db.get_keywords()
    )
    return page("Радар заказов", f"""<div class="wrap">
<header><h1>📡 Радар заказов</h1><form method="post" action="/logout"><button>Выйти</button></form></header>
<div class="grid"><main><nav class="tabs">{tabs}</nav>{cards}</main>
<aside><div class="card"><b>Статистика</b>
<div class="stat"><span>Просмотрено</span><b>{s['total']}</b></div>
<div class="stat"><span>Подходящих</span><b>{s['matched']}</b></div>
<div class="stat"><span>Сегодня</span><b>{s['today']}</b></div>
<div class="stat"><span>Откликнулся</span><b>{s['applied']}</b></div></div>
<div class="card"><b>Ключевые слова</b><p class="meta">Ищутся с начала слова: «бот» найдёт «боты».</p>{keywords}
<form method="post" action="/keywords/add" class="row"><input name="words" placeholder="бот, лендинг" required>
<button class="main">Добавить</button></form></div></aside></div></div>""")


async def change_status(request: web.Request):
    form = await request.post()
    try:
        db.set_status(int(form["id"]), str(form["status"]))
    except (KeyError, ValueError):
        raise web.HTTPBadRequest()
    ref = urlsplit(request.headers.get("Referer", "/"))  # только путь: не уводим на чужой сайт
    raise web.HTTPFound((ref.path or "/") + (f"?{ref.query}" if ref.query else ""))


async def add_keywords(request: web.Request):
    form = await request.post()
    for w in str(form.get("words", "")).split(","):
        if w := feeds.clean_keyword(w):
            db.add_keyword(w)
    raise web.HTTPFound("/")


async def delete_keyword(request: web.Request):
    form = await request.post()
    db.delete_keyword(str(form.get("word", "")))
    raise web.HTTPFound("/")


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth])
    app.add_routes([
        web.get("/", index),
        web.get("/login", login_page), web.post("/login", login_page),
        web.post("/logout", logout),
        web.post("/status", change_status),
        web.post("/keywords/add", add_keywords),
        web.post("/keywords/delete", delete_keyword),
    ])
    return app
