import asyncio
import os
import secrets
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from markupsafe import Markup
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import casino
import db
from pk.hub import hub
from pk.registry import GAME_INFO, PK_GAMES, TEAM_GAMES, client_js

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_or_create(filename: str, nbytes: int, env: str | None = None) -> str:
    """從環境變數或 instance/ 底下的檔案讀取祕密；不存在就產生一個。"""
    if env and os.environ.get(env):
        return os.environ[env]
    path = os.path.join(db.INSTANCE_DIR, filename)
    os.makedirs(db.INSTANCE_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_urlsafe(nbytes))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


ADMIN_PASSWORD = _load_or_create("admin_password.txt", 9, env="ADMIN_PASSWORD")
SECRET_KEY = _load_or_create("secret_key.txt", 32)

db.init_db()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY,
                   same_site="lax", https_only=False, max_age=60 * 60 * 24 * 7)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# 樣式直接內嵌進每個頁面，不另外抓 style.css：
# 透過 ngrok 免費版時，獨立的 CSS 請求可能被 ngrok 警告頁攔下（或卡在 Safari 快取），畫面就變白底。
_CSS_PATH = os.path.join(BASE_DIR, "static", "style.css")
_css_cache: dict = {}


def inline_css() -> Markup:
    mtime = os.path.getmtime(_CSS_PATH)
    if _css_cache.get("mtime") != mtime:
        with open(_CSS_PATH, encoding="utf-8") as f:
            _css_cache.update(mtime=mtime, css=Markup(f.read().replace("</", "<\\/")))
    return _css_cache["css"]


templates.env.globals["inline_css"] = inline_css


# 主畫面圖示：一樣直接內嵌（data URI）。ngrok 免費版會把瀏覽器單獨抓的圖片導到警告頁，圖示就會壞掉。
def _icon_uri(name: str) -> str:
    import base64
    with open(os.path.join(BASE_DIR, "static", "icons", name), "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


ICONS = {k: _icon_uri(f) for k, f in {"apple": "apple-touch-icon.png", "favicon": "favicon-32.png",
                                       "192": "icon-192.png", "512": "icon-512.png"}.items()}
templates.env.globals["ICONS"] = ICONS


def _manifest(name: str, short: str, start: str) -> JSONResponse:
    """加到主畫面用（Android 讀這個；iPhone 讀 <link rel=apple-touch-icon>）。每位玩家各自一份，打開就是自己的頁面。"""
    return JSONResponse({
        "name": name, "short_name": short, "lang": "zh-Hant",
        "start_url": start, "scope": start, "id": start,
        "display": "standalone", "orientation": "portrait",
        "background_color": "#031a11", "theme_color": "#0b3d2a",
        "icons": [{"src": ICONS["192"], "sizes": "192x192", "type": "image/png"},
                  {"src": ICONS["512"], "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")
templates.env.globals["TEAMS"] = db.TEAMS
templates.env.globals["PK_GAMES"] = PK_GAMES
templates.env.globals["TEAM_GAMES"] = TEAM_GAMES
templates.env.globals["GAME_INFO"] = GAME_INFO
templates.env.globals["GAME_CLIENT_JS"] = Markup(client_js())


def redirect(url: str, msg: str | None = None, error: bool = False) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + ("err=" if error else "msg=") + quote(msg)
    return RedirectResponse(url, status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ======================= 玩家 =======================

def _player(conn, token: str):
    user = db.get_user_by_token(conn, token)
    if user is None:
        raise HTTPException(404, "連結無效，請向主持人索取你的專屬連結。")
    return user


@app.get("/p/{token}", response_class=HTMLResponse)
def player_page(request: Request, token: str):
    with db.read() as conn:
        user = _player(conn, token)
        rnd = db.active_round(conn)
        bet = db.get_bet(conn, user["id"], rnd["id"]) if rnd else None
        history = db.user_history(conn, user["id"])
        epoch = db.get_epoch(conn)
        adj = db.adjustments_version(conn, user["id"])
        pk = _pk_ctx(conn, rnd, user["id"])
    available = user["balance"] + (bet["amount"] if bet else 0)
    return templates.TemplateResponse(request, "player.html", {
        **pk,
        "user": user, "round": rnd, "bet": bet, "history": history, "epoch": epoch, "adj": adj,
        "max_bet": available if db.MAX_BET is None else min(db.MAX_BET, available), "min_bet": db.MIN_BET,
        # 剩不到最低注額就等於出局
        "eliminated": user["balance"] < db.MIN_BET and user["pending"] == 0,
        "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
    })


def _pk_ctx(conn, rnd, user_id: int | None) -> dict:
    """PK 盤需要的資料：投票候選人、我投給誰、兩位選手、我是哪一邊。"""
    if rnd is None or rnd["kind"] != "pk":
        return {"candidates": [], "my_vote": None, "pk_a": None, "pk_b": None, "my_side": None, "is_team": False}
    if rnd["game"] in TEAM_GAMES:      # 團體賽：A / B 是隊伍
        return {
            "candidates": [], "my_vote": None, "is_team": True,
            "pk_a": {"name": db.TEAMS[rnd["team_a"]], "team": rnd["team_a"]},
            "pk_b": {"name": db.TEAMS[rnd["team_b"]], "team": rnd["team_b"]},
            "my_side": db.my_side(conn, rnd, user_id),
        }
    my_vote = db.get_vote(conn, rnd["id"], user_id) if user_id else None
    return {
        "candidates": [u for u in db.users(conn) if u["id"] != user_id] if rnd["status"] == "voting" else [],
        "my_vote": my_vote["candidate_id"] if my_vote else None,
        "pk_a": db.get_user(conn, rnd["player_a"]) if rnd["player_a"] else None,
        "pk_b": db.get_user(conn, rnd["player_b"]) if rnd["player_b"] else None,
        "my_side": db.my_side(conn, rnd, user_id),
        "is_team": False,
    }


@app.post("/p/{token}/vote")
def player_vote(token: str, candidate: int = Form(...)):
    with db.read() as conn:
        user = _player(conn, token)
    try:
        msg = db.cast_vote(user["id"], candidate)
    except db.BetError as e:
        return redirect(f"/p/{token}", str(e), error=True)
    return redirect(f"/p/{token}", msg)


@app.post("/p/{token}/bet")
def player_bet(token: str, amount: str = Form("0"), choice: str | None = Form(None)):
    with db.read() as conn:
        user = _player(conn, token)
    try:
        amt = int(amount or 0)
    except ValueError:
        return redirect(f"/p/{token}", "點數必須是整數。", error=True)
    try:
        msg = db.place_bet(user["id"], choice, amt)
    except db.BetError as e:
        return redirect(f"/p/{token}", str(e), error=True)
    return redirect(f"/p/{token}", msg)


@app.get("/p/{token}/manifest.webmanifest")
def player_manifest(token: str):
    with db.read() as conn:
        user = _player(conn, token)
    return _manifest(f"DS科大賭場 · {user['name']}", "DS科大賭場", f"/p/{token}")


@app.get("/admin/manifest.webmanifest")
def admin_manifest():
    return _manifest("DS科大賭場 · 後台", "DS賭場後台", "/admin")


@app.get("/favicon.ico")
def favicon():
    import base64
    return Response(base64.b64decode(ICONS["favicon"].split(",", 1)[1]), media_type="image/png")


@app.get("/p/{token}/state")
def player_state(token: str):
    """玩家頁輪詢用：輪次或狀態改變時，前端重新整理。"""
    with db.read() as conn:
        user = _player(conn, token)
        rnd = db.active_round(conn)
        epoch = db.get_epoch(conn)
        adj = db.adjustments_version(conn, user["id"])
    return JSONResponse({"round": rnd["id"] if rnd else None,
                         "status": rnd["status"] if rnd else None,
                         "epoch": epoch, "adj": adj, "team": user["team"]})


# ======================= 娛樂城（玩家自己玩） =======================

@app.get("/p/{token}/casino", response_class=HTMLResponse)
def casino_page(request: Request, token: str):
    with db.read() as conn:
        user = _player(conn, token)
        rnd = db.active_round(conn)
        hilo = casino.hilo_current(conn, user["id"])
        recent = casino.recent(conn, user["id"])
    return templates.TemplateResponse(request, "casino.html", {
        "user": user, "rules": casino.rules(), "hilo": hilo, "recent": recent,
        "round_status": rnd["status"] if rnd else None,
    })


async def _casino_call(request: Request, token: str, fn, *keys):
    with db.read() as conn:
        user = _player(conn, token)
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = []
    for k in keys:
        v = body.get(k)
        if k == "bet":
            try:
                v = int(v)
            except (TypeError, ValueError):
                return JSONResponse({"error": "點數必須是整數。"}, status_code=400)
        args.append(v)
    try:
        return JSONResponse(fn(user["id"], *args))
    except db.BetError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/p/{token}/casino/slot")
async def casino_slot(request: Request, token: str):
    return await _casino_call(request, token, casino.spin, "bet")


@app.post("/p/{token}/casino/sicbo")
async def casino_sicbo(request: Request, token: str):
    return await _casino_call(request, token, casino.sicbo, "bet", "pick")


@app.post("/p/{token}/casino/hilo/start")
async def casino_hilo_start(request: Request, token: str):
    return await _casino_call(request, token, casino.hilo_start, "bet")


@app.post("/p/{token}/casino/hilo/guess")
async def casino_hilo_guess(request: Request, token: str):
    return await _casino_call(request, token, casino.hilo_guess, "guess")


@app.post("/p/{token}/casino/hilo/cashout")
async def casino_hilo_cashout(request: Request, token: str):
    return await _casino_call(request, token, casino.hilo_cashout)


# ======================= 管理員 =======================

def _is_admin(request: Request) -> bool:
    return request.session.get("admin") is True


def _require_admin(request: Request):
    if not _is_admin(request):
        raise HTTPException(303, headers={"Location": "/admin/login"})


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "admin_login.html",
                                      {"err": request.query_params.get("err")})


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        request.session["admin"] = True
        return redirect("/admin")
    return redirect("/admin/login", "密碼錯誤。", error=True)


@app.post("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return redirect("/admin/login")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    _require_admin(request)
    with db.read() as conn:
        rnd = db.active_round(conn)
        ctx = {
            "round": rnd,
            "board": db.leaderboard(conn),
            "past": db.past_rounds(conn),
            "awards": db.recent_awards(conn),
            "casino": casino.summary(conn),
            "team_counts": db.team_counts(conn),
            "prize": db.MAIN_MATCH_PRIZE,
            "team_prize": db.TEAM_MATCH_PRIZE,
            "base_url": str(request.base_url).rstrip("/"),
            "game_alive": hub.game is not None,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        }
        ctx.update(_live_ctx(conn, rnd))
        ctx.update(_pk_ctx(conn, rnd, None))
    return templates.TemplateResponse(request, "admin.html", ctx)


def _live_ctx(conn, rnd):
    if rnd is None:
        return {"bets": [], "total_a": 0, "total_b": 0, "count_a": 0, "count_b": 0, "tally": []}
    if rnd["status"] == "voting":
        tally = db.vote_tally(conn, rnd["id"])
        return {"bets": [], "total_a": 0, "total_b": 0, "count_a": 0, "count_b": 0, "tally": tally,
                "voted": sum(1 for t in tally if t["voted"])}
    bets = db.round_bets(conn, rnd["id"])
    return {
        "tally": [],
        "bets": bets,
        "total_a": sum(b["amount"] for b in bets if b["choice"] == "A"),
        "total_b": sum(b["amount"] for b in bets if b["choice"] == "B"),
        "count_a": sum(1 for b in bets if b["choice"] == "A"),
        "count_b": sum(1 for b in bets if b["choice"] == "B"),
    }


@app.get("/admin/live", response_class=HTMLResponse)
def admin_live(request: Request):
    """管理員頁每幾秒抓一次，只更新下注狀況區塊。"""
    _require_admin(request)
    with db.read() as conn:
        rnd = db.active_round(conn)
        ctx = {"round": rnd, **_live_ctx(conn, rnd)}
    return templates.TemplateResponse(request, "_live.html", ctx)


def _board_ctx(conn):
    return {"board": db.leaderboard(conn), "round": db.active_round(conn), "min_bet": db.MIN_BET}


@app.get("/admin/leaderboard", response_class=HTMLResponse)
def admin_leaderboard(request: Request):
    """獨立排行榜頁（適合投影），每 5 秒自動更新。"""
    _require_admin(request)
    with db.read() as conn:
        ctx = _board_ctx(conn)
    return templates.TemplateResponse(request, "leaderboard.html", ctx)


@app.get("/admin/leaderboard/data", response_class=HTMLResponse)
def admin_leaderboard_data(request: Request):
    _require_admin(request)
    with db.read() as conn:
        ctx = _board_ctx(conn)
    return templates.TemplateResponse(request, "_board.html", ctx)


def _admin_action(request: Request, fn, *args, ok: str):
    _require_admin(request)
    try:
        fn(*args)
    except db.BetError as e:
        return redirect("/admin", str(e), error=True)
    return redirect("/admin", ok)


@app.post("/admin/rounds")
def admin_create_round(request: Request, title: str = Form(""), kind: str = Form("normal"),
                       option_a: str = Form(""), option_b: str = Form(""), game: str = Form(""),
                       team_a: str = Form(""), team_b: str = Form("")):
    ok = {"pk": "PK 盤已開始投票。", "team": "團體賽已建立，準備好就按「開始」。"}.get(kind, "已開盤。")
    return _admin_action(request, db.create_round, title, option_a, option_b, kind,
                         game or None, team_a or None, team_b or None, ok=ok)


@app.post("/admin/rounds/{round_id}/close-vote")
def admin_close_vote(request: Request, round_id: int):
    _require_admin(request)
    try:
        a, b = db.close_voting(round_id)
    except db.BetError as e:
        return redirect("/admin", str(e), error=True)
    return redirect("/admin", f"PK 人選：A {a} vs B {b}，開放下注！")


@app.post("/admin/rounds/{round_id}/start")
async def admin_start_pk(request: Request, round_id: int):
    _require_admin(request)
    if hub.game is not None:
        return redirect("/admin", "已經有一場 PK 在進行中。", error=True)
    try:
        rnd = await asyncio.to_thread(db.start_pk, round_id)
    except db.BetError as e:
        return redirect("/admin", str(e), error=True)
    await hub.start(rnd)
    return redirect("/admin", "已開始！")


@app.post("/admin/rounds/{round_id}/lock")
def admin_lock(request: Request, round_id: int):
    return _admin_action(request, db.lock_round, round_id, ok="已關盤，玩家無法再下注或修改。")


@app.post("/admin/rounds/{round_id}/reopen")
def admin_reopen(request: Request, round_id: int):
    return _admin_action(request, db.reopen_round, round_id, ok="已重新開盤。")


@app.post("/admin/rounds/{round_id}/settle")
def admin_settle(request: Request, round_id: int, result: str = Form(...)):
    return _admin_action(request, db.settle_round, round_id, result, ok=f"已結算，正確答案：{result}。")


@app.post("/admin/rounds/{round_id}/void")
async def admin_void(request: Request, round_id: int):
    _require_admin(request)
    try:
        await asyncio.to_thread(db.void_round, round_id)
    except db.BetError as e:
        return redirect("/admin", str(e), error=True)
    await hub.stop(round_id)
    with db.read() as conn:
        rnd = db.get_round(conn, round_id)
    return redirect("/admin", "已作廢這場團體賽。" if rnd and rnd["team_a"] else "已作廢，所有下注退還。")


# ======================= 即時連線（PK 觀戰 / 連打）=======================

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """玩家用 ?t=<token> 連線；管理員用登入的 session。選手送 JSON 動作（見 game.py 各遊戲的 handle）。"""
    token = ws.query_params.get("t")
    user_id = None
    if token:
        with db.read() as conn:
            user = db.get_user_by_token(conn, token)
        if user is None:
            await ws.close(code=4404)
            return
        user_id = user["id"]
    elif ws.session.get("admin") is not True:
        await ws.close(code=4403)
        return

    await ws.accept()
    hub.clients[ws] = user_id
    try:
        if hub.game:
            await hub.send(ws, hub.game.message())
        while True:
            hub.handle(user_id, await ws.receive_text())
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.pop(ws, None)


@app.post("/admin/players")
def admin_add_player(request: Request, name: str = Form(""), team: str = Form("")):
    return _admin_action(request, db.create_user, name, team, ok=f"已新增玩家「{name.strip()}」。")


@app.post("/admin/players/{user_id}/team")
def admin_set_team(request: Request, user_id: int, team: str = Form("")):
    return _admin_action(request, db.set_team, user_id, team,
                         ok=f"已改為{db.TEAMS.get(team, '未分隊')}。")


@app.post("/admin/awards")
def admin_award(request: Request, target: str = Form(""), amount: str = Form(""),
                reason: str = Form("")):
    _require_admin(request)
    try:
        msg = db.award(target, int(amount), reason)
    except ValueError:
        return redirect("/admin", "點數必須是整數。", error=True)
    except db.BetError as e:
        return redirect("/admin", str(e), error=True)
    return redirect("/admin", msg)


@app.post("/admin/awards/{batch}/undo")
def admin_undo_award(request: Request, batch: str):
    return _admin_action(request, db.undo_award, batch, ok="已撤銷這筆發放。")


@app.post("/admin/reset-game")
def admin_reset_game(request: Request, confirm: str = Form("")):
    _require_admin(request)
    if confirm.strip() != "重新開始":
        return redirect("/admin", "請輸入「重新開始」四個字確認。", error=True)
    path = db.reset_game()
    return redirect("/admin", f"已重新開始，所有人回到 {db.INITIAL_POINTS} 點。舊資料備份在 {os.path.relpath(path, BASE_DIR)}")


@app.post("/admin/players/{user_id}/delete")
def admin_delete_player(request: Request, user_id: int):
    _require_admin(request)
    try:
        name = db.delete_user(user_id)
    except db.BetError as e:
        return redirect("/admin", str(e), error=True)
    return redirect("/admin", f"已刪除玩家「{name}」。")


@app.post("/admin/players/{user_id}/reset")
def admin_reset_link(request: Request, user_id: int):
    return _admin_action(request, db.reset_token, user_id, ok="已重新產生連結，舊連結失效。")
