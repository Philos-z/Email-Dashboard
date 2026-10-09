"""Private LAN Progress Atlas server with hourly and manual Outlook refresh."""
from __future__ import annotations
import asyncio
import base64
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
import tempfile
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import runtime_settings
import mail_provider
import outlook_mcp
import ai_proxy
import item_context
import mail_sync
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

ROOT = Path(__file__).resolve().parent
DATA = runtime_settings.DATA
DB_PATH = DATA / "atlas.sqlite3"
AUTH_PATH = DATA / "auth.json"
SEED_PATH = None  # Tests may inject a synthetic fixture; new installations start empty.
DASHBOARD_PATH = ROOT / "web" / "dashboard_lan.html"
SESSION_SECRET_PATH = DATA / "session.key"
DB_LOCK = Lock()
PENDING = {}  # token -> {proposal, model_revision, expiry}; never persisted before user confirmation
SOCKETS: set[WebSocket] = set()
LOGIN_FAILURES = defaultdict(deque)
MAX_ITEMS = 500
ALLOWED_STATUS = {"rejected", "in_progress", "waiting", "verification", "blocked", "tracking", "planned", "done"}
ALLOWED_PRIORITY = {"P0", "P1", "P2", "P3"}
ALLOWED_MODE = {"project", "ongoing"}
EDITABLE_FIELDS = {"title", "category", "status", "priority", "mode", "deadline", "deadlineVerified", "next", "blocker", "notes", "tags"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def login_required():
    return os.getenv("ATLAS_REQUIRE_LOGIN", "1") == "1"


def write_private(path: Path, contents: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(contents)


def secret():
    if not SESSION_SECRET_PATH.exists():
        try:
            write_private(SESSION_SECRET_PATH, secrets.token_bytes(32))
        except FileExistsError:
            pass
    return SESSION_SECRET_PATH.read_bytes()


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    DATA.mkdir(exist_ok=True)
    with DB_LOCK, connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id=1), model TEXT NOT NULL, revision INTEGER NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS revisions (id TEXT PRIMARY KEY, version INTEGER NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, changed_at TEXT, kind TEXT, item_id TEXT, details TEXT)")
        r = con.execute("SELECT revision FROM state WHERE id=1").fetchone()
        if r is None:
            seed = json.loads(SEED_PATH.read_text(encoding="utf-8")) if SEED_PATH else runtime_settings.empty_model()
            validate_model(seed)
            con.execute("INSERT INTO state (id,model,revision) VALUES (1,?,1)", (json.dumps(seed, ensure_ascii=False),))
            con.executemany("INSERT INTO revisions (id,version) VALUES (?,1)", [(it["id"],) for it in seed["items"]])
            con.execute("INSERT INTO audit(changed_at,kind,item_id,details) VALUES (?,?,?,?)", (now(), "import_seed", "", "创建空白看板"))
    secret()


def read_snapshot():
    with DB_LOCK, connect() as con:
        row = con.execute("SELECT model,revision FROM state WHERE id=1").fetchone()
        versions = {x["id"]: x["version"] for x in con.execute("SELECT id,version FROM revisions")}
        return {"model": json.loads(row["model"]), "revision": row["revision"], "itemRevisions": versions, "loginRequired": login_required()}


def validate_item(item):
    if not isinstance(item, dict):
        raise ValueError("事项必须是对象")
    if not isinstance(item.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", item["id"]):
        raise ValueError("事项 ID 格式无效")
    if not isinstance(item.get("title"), str) or not 1 <= len(item["title"]) <= 180:
        raise ValueError("事项标题无效")
    if item.get("status") not in ALLOWED_STATUS:
        raise ValueError("事项状态无效")
    if item.get("priority") not in ALLOWED_PRIORITY:
        raise ValueError("优先级无效")
    if item.get("mode") not in ALLOWED_MODE:
        raise ValueError("事项模式无效")
    if not isinstance(item.get("milestones"), list) or len(item["milestones"]) > 100:
        raise ValueError("里程碑数据无效")
    for m in item["milestones"]:
        if not isinstance(m, dict) or not isinstance(m.get("text"), str) or not isinstance(m.get("done"), bool):
            raise ValueError("里程碑格式无效")
    if len(json.dumps(item, ensure_ascii=False)) > 100000:
        raise ValueError("单个事项超过安全大小限制")


def validate_model(model):
    if not isinstance(model, dict) or model.get("schemaVersion") != 1 or not isinstance(model.get("items"), list):
        raise ValueError("不是 Progress Atlas v1 数据")
    if len(model["items"]) > MAX_ITEMS:
        raise ValueError("事项数量超过上限")
    ids = set()
    for item in model["items"]:
        validate_item(item)
        if item["id"] in ids:
            raise ValueError("存在重复 ID")
        ids.add(item["id"])


def update_state(model, revision, con):
    con.execute("UPDATE state SET model=?, revision=? WHERE id=1", (json.dumps(model, ensure_ascii=False), revision))


def audit(con, kind, item_id, details):
    con.execute("INSERT INTO audit(changed_at,kind,item_id,details) VALUES (?,?,?,?)", (now(), kind, item_id, details[:300]))


def password_matches(password):
    if not AUTH_PATH.exists():
        return False
    auth = json.loads(AUTH_PATH.read_text())
    sample = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(auth["salt"]), auth["iterations"])
    return hmac.compare_digest(sample, bytes.fromhex(auth["hash"]))


def sign_cookie():
    expiry = int(time.time()) + 7 * 86400
    nonce = secrets.token_urlsafe(20)
    payload = f"{expiry}.{nonce}"
    sig = hmac.new(secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def check_cookie(value):
    if not value:
        return False
    try:
        raw = base64.urlsafe_b64decode(value.encode()).decode()
        expiry, nonce, signature = raw.split(".")
        if int(expiry) <= int(time.time()):
            return False
        correct = hmac.new(secret(), f"{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(correct, signature)
    except (ValueError, TypeError, UnicodeError):
        return False


def guard_origin(request):
    origin = request.headers.get("origin")
    if origin:
        u = urlparse(origin)
        # Protect against cross-site POST on HTTP LAN without trusting X-Forwarded-*.
        if not (u.scheme in {"http", "https"} and u.netloc == request.headers.get("host")):
            raise HTTPException(403, "非同源请求已拒绝")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "跨站请求被拒绝")


def check_auth(request):
    if login_required() and not check_cookie(request.cookies.get("atlas_session")):
        raise HTTPException(401, "请先登录")
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        guard_origin(request)


def safe_cookie_response(response, value):
    response.set_cookie("atlas_session", value, httponly=True, samesite="strict", secure=os.getenv("ATLAS_HTTPS", "0") == "1", max_age=7*86400, path="/")
    return response


LOGIN_PAGE = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>
<title>Progress Atlas · 私人登录</title><style>body{font-family:system-ui,-apple-system,sans-serif;background:#f3f6fa;display:grid;place-items:center;min-height:100vh;color:#20314d;margin:0}.box{background:white;border-radius:20px;padding:40px;max-width:380px;width:85%;box-shadow:0 10px 35px #25345013}h1{font-size:24px;margin:0 0 10px}p{color:#677b93;line-height:1.6}input,button{font:inherit;width:100%;box-sizing:border-box;padding:13px;margin-top:12px;border-radius:10px;border:1px solid #d9e1ee}button{background:#405dc2;color:white;font-weight:700;cursor:pointer}.warn{color:#b94b55}</style>
<section class=box><h1>🔒 Progress Atlas</h1><p>局域网私人看板 · 所有事项存储在自己的设备中</p><form id=f><input id=p type=password placeholder='请输入访问密码' required autocomplete=current-password autofocus><button>登录</button></form><p id=msg class=warn></p><small>此部署不包含互联网身份认证或云端同步。</small></section>
<script>document.getElementById('f').addEventListener('submit',async e=>{e.preventDefault();const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('p').value})});if(r.ok)location.href='/';else document.getElementById('msg').textContent='密码错误或登录过于频繁';});</script></html>"""

@asynccontextmanager
async def lifespan(app):
    init_db()
    if login_required() and not AUTH_PATH.exists():
        logging.warning("未配置访问密码。先运行 python setup_password.py，再启动服务器。")
    service = mail_sync.MailSyncService(sys.modules[__name__])
    context_service = item_context.ItemContextService(sys.modules[__name__])
    app.state.mail_sync = service
    app.state.item_context = context_service
    await service.start()
    await context_service.start()
    try:
        yield
    finally:
        await context_service.stop()
        await service.stop()

app = FastAPI(title="Progress Atlas LAN", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ROOT / "web" / "assets"), name="assets")

@app.middleware("http")
async def secure_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
    return response

@app.get("/")
async def index(request: Request):
    if login_required() and not check_cookie(request.cookies.get("atlas_session")):
        return HTMLResponse(LOGIN_PAGE)
    snap = read_snapshot()
    # Directly embed the server truth before legacy JS startup. Escaping prevents script tag injection.
    serialized = json.dumps(snap, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    webpage = DASHBOARD_PATH.read_text(encoding="utf-8")
    webpage = webpage.replace("/*__SERVER_SNAPSHOT__*/", "window.__ATLAS_SERVER__=" + serialized + ";")
    logout = '<button class="btn" type="button" data-lan-action="logout">安全退出</button>' if login_required() else ""
    webpage = webpage.replace("<!--__ATLAS_LOGOUT__-->", logout)
    return HTMLResponse(webpage)

@app.post("/api/login")
async def login(request: Request):
    guard_origin(request)
    if not login_required():
        return {"ok": True, "loginRequired": False}
    if not AUTH_PATH.exists():
        raise HTTPException(503, "请先设置密码")
    ip = request.client.host if request.client else "unknown"
    attempts = LOGIN_FAILURES[ip]
    while attempts and attempts[0] < time.time() - 600:
        attempts.popleft()
    if len(attempts) >= 8:
        raise HTTPException(429, "请十分钟后重试")
    info = await request.json()
    password = info.get("password", "") if isinstance(info, dict) else ""
    if not isinstance(password, str) or len(password) > 512 or not password_matches(password):
        attempts.append(time.time())
        raise HTTPException(401, "登录失败")
    attempts.clear()
    return safe_cookie_response(JSONResponse({"ok": True}), sign_cookie())

@app.post("/api/logout")
async def logout(request: Request):
    check_auth(request)
    r = JSONResponse({"ok": True})
    r.delete_cookie("atlas_session", path="/")
    return r

@app.get("/api/snapshot")
async def snapshot(request: Request):
    check_auth(request)
    return read_snapshot()

@app.get("/api/backup.sqlite3")
async def export_database(request: Request):
    check_auth(request)
    fd,path=tempfile.mkstemp(prefix="atlas-backup-",suffix=".sqlite3",dir=DATA)
    os.close(fd)
    try:
        with DB_LOCK, connect() as src, sqlite3.connect(path) as target:
            src.backup(target)
        b=Path(path).read_bytes()
        return Response(content=b,media_type="application/octet-stream",headers={"Content-Disposition":"attachment; filename=progress-atlas.sqlite3"})
    finally:
        Path(path).unlink(missing_ok=True)

@app.get("/api/audit")
async def recent_audit(request: Request, limit: int = 60):
    check_auth(request)
    with DB_LOCK, connect() as con:
        rows = con.execute("SELECT id, changed_at,kind,item_id,details FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return {"events": [dict(x) for x in rows]}

async def broadcast(revision: int, *, item_id=None, source=None):
    message = {"type": "changed", "revision": revision}
    if source:
        message.update(itemId=item_id, source=source)
    for ws in list(SOCKETS):
        try:
            await ws.send_json(message)
        except Exception:
            SOCKETS.discard(ws)

@app.websocket("/ws")
async def websocket_updates(ws: WebSocket):
    if login_required() and not check_cookie(ws.cookies.get("atlas_session")):
        await ws.close(code=4401)
        return
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).netloc != ws.headers.get("host"):
        await ws.close(code=4403)
        return
    await ws.accept()
    SOCKETS.add(ws)
    try:
        while True:
            await ws.receive_text()  # use browser keepalive for disconnect detection; no update triggers
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        SOCKETS.discard(ws)

async def require_json(request: Request, limit=2_000_000):
    if int(request.headers.get("content-length", "0")) > limit:
        raise HTTPException(413, "请求超过大小限制")
    blob = await request.body()
    if len(blob) > limit:
        raise HTTPException(413, "请求过大")
    try:
        return json.loads(blob)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "JSON 无效")

@app.put("/api/items/{item_id}")
async def put_item(item_id: str, request: Request):
    check_auth(request)
    body = await require_json(request)
    incoming = body.get("item")
    expected = body.get("expectedVersion")
    try:
        validate_item(incoming)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if incoming["id"] != item_id or not isinstance(expected, int):
        raise HTTPException(400, "ID 或版本号不匹配")
    with DB_LOCK, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        oldrev = con.execute("SELECT version FROM revisions WHERE id=?", (item_id,)).fetchone()
        if oldrev is None:
            raise HTTPException(404, "事项不存在")
        if oldrev["version"] != expected:
            raise HTTPException(409, "另一台设备已修改这个事项，请刷新后重试")
        row = con.execute("SELECT model,revision FROM state WHERE id=1").fetchone()
        model = json.loads(row["model"])
        changed_fields = set()
        for i, item in enumerate(model["items"]):
            if item["id"] == item_id:
                changed_fields = {key for key in item_context.INPUT_FIELDS if item.get(key) != incoming.get(key)}
                for key in item_context.CONTEXT_FIELDS:
                    if key in item:
                        incoming[key] = item[key]
                    else:
                        incoming.pop(key, None)
                if changed_fields:
                    today = datetime.now(runtime_settings.zone()).date().isoformat()
                    incoming['last'] = max(item.get('last') or '', today)
                model["items"][i] = incoming
                break
        model["lastSavedAt"] = now()
        r = row["revision"]+1
        update_state(model, r, con)
        con.execute("UPDATE revisions SET version=version+1 WHERE id=?", (item_id,))
        audit(con, "edit", item_id, incoming["title"])
        context_job = request.app.state.item_context.enqueue(con, item_id, expected+1, changed_fields)
    await broadcast(r)
    return {"ok": True, "revision": r, "itemVersion": expected+1, "contextUpdate": context_job}

@app.get("/api/items/{item_id}/context-status")
async def item_context_status(item_id: str, request: Request):
    check_auth(request)
    return request.app.state.item_context.status(item_id)

@app.post("/api/items/{item_id}/refresh-context", status_code=202)
async def refresh_item_context(item_id: str, request: Request):
    check_auth(request)
    with DB_LOCK, connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT version FROM revisions WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '事项不存在')
        result = request.app.state.item_context.enqueue(con, item_id, row['version'], {'manual_refresh'})
    return {"itemId": item_id, **result}

@app.post("/api/items")
async def create_item(request: Request):
    check_auth(request)
    body = await require_json(request)
    incoming = body.get("item")
    try:
        validate_item(incoming)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with DB_LOCK, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT model,revision FROM state WHERE id=1").fetchone()
        model=json.loads(row["model"])
        if len(model["items"]) >= MAX_ITEMS:
            raise HTTPException(400, "事项数量达到上限")
        if any(x["id"] == incoming["id"] for x in model["items"]):
            raise HTTPException(409, "事项 ID 已存在")
        model["items"].insert(0, incoming)
        model["lastSavedAt"]=now()
        r=row["revision"]+1
        update_state(model,r,con)
        con.execute("INSERT INTO revisions(id,version) VALUES (?,1)", (incoming["id"],))
        audit(con,"create",incoming["id"],incoming["title"])
    await broadcast(r)
    return {"ok":True,"revision":r,"itemVersion":1}

@app.post("/api/import")
async def import_snapshot(request: Request):
    check_auth(request)
    body=await require_json(request)
    newmodel=body.get("model")
    expected=body.get("expectedRevision")
    try:
        validate_model(newmodel)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with DB_LOCK, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT revision FROM state WHERE id=1").fetchone()
        if row["revision"] != expected:
            raise HTTPException(409, "已有更新，请先刷新数据再导入")
        r = row["revision"] + 1
        newmodel["lastSavedAt"] = now()
        update_state(newmodel,r,con)
        con.execute("DELETE FROM revisions")
        con.executemany("INSERT INTO revisions(id,version) VALUES (?,1)", [(x["id"],) for x in newmodel["items"]])
        audit(con,"import","",f"用户确认导入 {len(newmodel['items'])} 项")
    await broadcast(r)
    return {"ok":True,"revision":r}

@app.post("/api/legacy/preview")
async def legacy_preview(request: Request):
    check_auth(request)
    body=await require_json(request)
    old=body.get("model")
    try: validate_model(old)
    except ValueError as e:raise HTTPException(400,str(e))
    current=read_snapshot()
    current_by={x["id"]:x for x in current["model"]["items"]}
    chosen=[];new=[]
    for it in old["items"]:
        if it["id"] not in current_by:
            if it["id"].startswith("local-"):
                new.append(it["title"])
            continue
        saved=current_by[it["id"]]
        changes=[]
        if it.get("userUpdatedAt") and it.get("notes") != saved.get("notes") and it.get("notes"):
            changes.append("手动备注")
        old_user_ms=[m for m in it.get("milestones",[]) if m.get("certainty")=="user" or "手动" in str(m.get("source","")) or "本地" in str(m.get("source",""))]
        if any(m.get("text") not in {x.get("text") for x in saved.get("milestones",[])} for m in old_user_ms):
            changes.append("用户新增里程碑")
        if changes:chosen.append({"id":it["id"],"title":it["title"],"fields":changes})
    token=secrets.token_urlsafe(28)
    PENDING[token]={"kind":"legacy","model":old,"revision":current["revision"],"expiry":time.time()+600}
    return {"token":token,"items":chosen,"newTitles":new,"notice":"仅合并手动备注、手动新增里程碑和本地新增事项。不会覆盖 Outlook 证据或现有项目状态。"}

@app.post("/api/legacy/commit")
async def legacy_commit(request: Request):
    check_auth(request)
    body=await require_json(request)
    info=PENDING.pop(body.get("token"),None) if isinstance(body.get("token"),str) else None
    if not info or info.get("kind")!="legacy" or info["expiry"]<time.time():
        raise HTTPException(409,"合并预览已失效")
    with DB_LOCK, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT model,revision FROM state WHERE id=1").fetchone()
        if row["revision"]!=info["revision"]:
            raise HTTPException(409,"已有其它设备更新，请重新预览")
        model=json.loads(row["model"])
        byid={it["id"]:it for it in model["items"]}
        updated=0;added=0
        for old in info["model"]["items"]:
            item=byid.get(old["id"])
            if item is None:
                if old["id"].startswith("local-") and len(model["items"])<MAX_ITEMS:
                    model["items"].append(old)
                    byid[old["id"]]=old
                    con.execute("INSERT INTO revisions(id,version) VALUES (?,1)",(old["id"],))
                    audit(con,"legacy_new",old["id"],old["title"])
                    added+=1
                continue
            if not old.get("userUpdatedAt"):
                continue
            if (item.get("userUpdatedAt") or "") >= (old.get("userUpdatedAt") or ""):
                continue
            changed=False
            if old.get("notes") and old["notes"] != item.get("notes"):
                item["notes"] = old["notes"]
                changed=True
            savedmilestones={m.get("text") for m in item.get("milestones",[])}
            for m in old.get("milestones",[]):
                if m["text"] not in savedmilestones and (m.get("certainty")=="user" or "手动" in str(m.get("source","")) or "本地" in str(m.get("source",""))):
                    item.setdefault("milestones",[]).append(m)
                    savedmilestones.add(m["text"])
                    changed=True
            if changed:
                item["userUpdatedAt"]=now()
                con.execute("UPDATE revisions SET version=version+1 WHERE id=?",(item["id"],))
                audit(con,"legacy_merge",item["id"],"合并手动备注 / 里程碑")
                updated+=1
        model["categories"]=list(dict.fromkeys([*model.get("categories",[]),*(x.get("category","") for x in model["items"])]))
        model["lastSavedAt"]=now()
        revision=row["revision"]+1
        update_state(model,revision,con)
    await broadcast(revision)
    return {"ok":True,"revision":revision,"changed":updated,"added":added}

@app.post("/api/reset")
async def reset(request: Request):
    check_auth(request)
    body=await require_json(request)
    expected=body.get("expectedRevision")
    seed=json.loads(SEED_PATH.read_text(encoding="utf-8")) if SEED_PATH else runtime_settings.empty_model()
    with DB_LOCK, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT revision FROM state WHERE id=1").fetchone()
        if row["revision"]!=expected:
            raise HTTPException(409, "存在其他设备更新，请刷新后重试")
        if con.execute("SELECT 1 FROM mail_sync_runs WHERE status='running' LIMIT 1").fetchone():
            raise HTTPException(409, "邮件刷新进行中，请完成后再重置")
        con.execute('DELETE FROM mail_seen')
        con.execute('DELETE FROM mail_folder_sync')
        con.execute('UPDATE mail_sync_config SET enabled=0,cursor=NULL,next_run_at=? WHERE id=1', (mail_sync.next_hour(),))
        r=row["revision"]+1
        update_state(seed,r,con)
        con.execute("DELETE FROM revisions")
        con.executemany("INSERT INTO revisions(id,version) VALUES (?,1)", [(x["id"],) for x in seed["items"]])
        audit(con,"reset","","恢复初始快照")
    await broadcast(r)
    return {"ok":True,"revision":r}


def validate_outlook_proposal(payload, current):
    """Pure validation + change plan. Never fetch Outlook or mutate state here."""
    if not isinstance(payload, dict) or not isinstance(payload.get("changes"), list):
        raise ValueError("请粘贴包含 changes 数组的结构化 JSON")
    changes = payload["changes"]
    if len(changes) > 80:
        raise ValueError("一次建议最多修改 80 个事项")
    byid = {x["id"]: x for x in current["items"]}
    summary = []
    seen=set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("changes 条目必须是对象")
        itemid=change.get("itemId")
        if not isinstance(itemid,str) or itemid not in byid or itemid in seen:
            raise ValueError(f"无效或重复的项目 ID: {str(itemid)[:40]}")
        seen.add(itemid)
        fields=change.get("fields",{})
        if not isinstance(fields,dict) or any(k not in EDITABLE_FIELDS for k in fields):
            raise ValueError("不允许变更这些字段（只能更新项目属性）")
        for k,v in fields.items():
            if k=="status" and v not in ALLOWED_STATUS:raise ValueError("状态值不正确")
            if k=="priority" and v not in ALLOWED_PRIORITY:raise ValueError("优先级不正确")
            if k=="mode" and v not in ALLOWED_MODE:raise ValueError("mode 不正确")
            if isinstance(v,str) and len(v)>10000:raise ValueError("单字段内容过长")
        evidence=change.get("mailEvidenceAdd",[])
        milestones=change.get("milestonesAdd",[])
        if not isinstance(evidence,list) or len(evidence)>20 or not isinstance(milestones,list) or len(milestones)>20:
            raise ValueError("单个项目证据或里程碑不能超过 20 条")
        for e in evidence:
            if not isinstance(e,dict) or not all(isinstance(e.get(k),str) for k in ("date","subject","summary")):
                raise ValueError("mailEvidenceAdd 必须包含 date、subject、summary")
            if len(json.dumps(e))>6000:raise ValueError("邮件证据过长")
        for m in milestones:
            if not isinstance(m,dict) or not isinstance(m.get("text"),str) or not isinstance(m.get("done"),bool):
                raise ValueError("里程碑需包含 text 和 done")
        summary.append({"itemId":itemid,"title":byid[itemid]["title"],"fields":{k:{"old":byid[itemid].get(k),"new":v} for k,v in fields.items() if byid[itemid].get(k)!=v},"milestonesAdd":len(milestones),"mailEvidenceAdd":len(evidence)})
    newitems=payload.get("newItems",[])
    if not isinstance(newitems,list) or len(newitems)>20:
        raise ValueError("新事项不能超过 20 项")
    newids=set()
    for i in newitems:
        validate_item(i)
        if i["id"] in byid or i["id"] in newids:
            raise ValueError("新事项 ID 重复")
        newids.add(i["id"])
    for e in payload.get("todayUpdatesAdd",[]):
        if not isinstance(e,dict) or not all(isinstance(e.get(k),str) for k in ("id","time","title","detail","kind")):
            raise ValueError("todayUpdatesAdd 格式无效")
    return {"items":summary,"newItems":[{"id":i["id"],"title":i["title"]} for i in newitems],"updatesCount":len(payload.get("todayUpdatesAdd",[]))}

@app.post("/api/outlook/preview")
async def outlook_preview(request: Request):
    check_auth(request)
    payload=await require_json(request)
    current=read_snapshot()
    try:
        summary=validate_outlook_proposal(payload,current["model"])
    except ValueError as e:
        raise HTTPException(400,str(e))
    token=secrets.token_urlsafe(28)
    for key,v in list(PENDING.items()):
        if v["expiry"] < time.time():PENDING.pop(key,None)
    PENDING[token]={"payload":payload,"revision":current["revision"],"expiry":time.time()+600}
    return {"token":token,"expectedRevision":current["revision"],"summary":summary,"notice":"这只是预览，未读取 Outlook，也未修改数据库。"}

@app.post("/api/outlook/commit")
async def outlook_commit(request: Request):
    check_auth(request)
    body=await require_json(request)
    token=body.get("token")
    info=PENDING.pop(token,None) if isinstance(token,str) else None
    if not info or info["expiry"]<time.time():
        raise HTTPException(409,"预览已过期，请重新检查差异")
    payload=info["payload"]
    with DB_LOCK, connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row=con.execute("SELECT model,revision FROM state WHERE id=1").fetchone()
        if row["revision"]!=info["revision"]:
            raise HTTPException(409,"预览后另一设备已经编辑，请重新预览，以免覆盖")
        model=json.loads(row["model"])
        try: validate_outlook_proposal(payload,model)
        except ValueError as e: raise HTTPException(400,str(e))
        byid={x["id"]:x for x in model["items"]}
        changed_ids=set()
        for ch in payload["changes"]:
            it=byid[ch["itemId"]]
            for key,value in ch.get("fields",{}).items():it[key]=value
            evidence_keys={(e.get("date"),e.get("subject"),e.get("summary")) for e in it.get("mailEvidence",[])}
            for e in ch.get("mailEvidenceAdd",[]):
                ident=(e["date"],e["subject"],e["summary"])
                if ident not in evidence_keys:
                    it.setdefault("mailEvidence",[]).append(e)
                    evidence_keys.add(ident)
            milestone_keys={m.get("text") for m in it.get("milestones",[])}
            for m in ch.get("milestonesAdd",[]):
                if m["text"] not in milestone_keys:
                    it.setdefault("milestones",[]).append(m)
                    milestone_keys.add(m["text"])
            it["last"] = max(it.get("last") or "", ch.get("last") or "")
            changed_ids.add(it["id"])
            audit(con,"outlook_import",it["id"],it.get("title", ""))
        for item in payload.get("newItems",[]):
            model["items"].append(item)
            con.execute("INSERT INTO revisions(id,version) VALUES (?,1)",(item["id"],))
            audit(con,"outlook_new",item["id"],item["title"])
        for itemid in changed_ids:
            con.execute("UPDATE revisions SET version=version+1 WHERE id=?",(itemid,))
        existing={e.get("id") for e in model.get("todayUpdates",[])}
        for entry in payload.get("todayUpdatesAdd",[]):
            if entry["id"] not in existing:
                model.setdefault("todayUpdates",[]).insert(0,entry)
                existing.add(entry["id"])
        model["categories"] = list(dict.fromkeys([*model.get("categories",[]),*(x.get("category","") for x in model["items"])]))
        model["lastInboxUpdate"] = {"date":payload.get("checkedThrough",now()),"method":"manual_outlook_mcp_import","at":now()}
        model["lastSavedAt"]=now()
        r=row["revision"]+1
        update_state(model,r,con)
    await broadcast(r)
    return {"ok":True,"revision":r,"changed":len(changed_ids),"added":len(payload.get("newItems",[]))}

@app.get("/api/mail/status")
@app.get("/api/outlook/status")
async def outlook_status(request: Request):
    check_auth(request)
    return request.app.state.mail_sync.status()

@app.post("/api/mail/refresh", status_code=202)
@app.post("/api/outlook/refresh", status_code=202)
async def outlook_refresh(request: Request):
    check_auth(request)
    try:
        return request.app.state.mail_sync.trigger('manual')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

@app.put("/api/mail/schedule")
@app.put("/api/outlook/schedule")
async def outlook_schedule(request: Request):
    check_auth(request)
    body = await require_json(request, limit=1000)
    try:
        return request.app.state.mail_sync.configure(body.get('enabled'))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

@app.get("/health")
async def health():
    # No data exposure; health is intentionally minimal.
    return {"ok":True}


@app.get('/api/mail/config')
async def mail_config(request: Request):
    check_auth(request)
    try:
        config = runtime_settings.read()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from None
    with DB_LOCK, connect() as con:
        locked = bool(con.execute('SELECT 1 FROM mail_folder_sync LIMIT 1').fetchone())
    return {**config, 'locked': locked}


@app.put('/api/mail/config')
async def save_mail_config(request: Request):
    check_auth(request)
    body = await require_json(request, limit=4000)
    try:
        incoming = runtime_settings.validate(body)
        old = runtime_settings.read()
        with DB_LOCK, connect() as con:
            if con.execute("SELECT 1 FROM mail_sync_runs WHERE status='running' LIMIT 1").fetchone():
                raise HTTPException(409, '邮件刷新进行中，请完成后再更改设置')
            locked = con.execute('SELECT 1 FROM mail_folder_sync LIMIT 1').fetchone()
            immutable = ('provider', 'email', 'historySince', 'timeZone', 'mcpServer')
            if locked and any(incoming[k] != old[k] for k in immutable):
                raise HTTPException(409, '此数据目录已绑定邮箱与同步范围。更换邮箱、时区或起始日期时，请使用新的 ATLAS_DATA_DIR。')
            same_identity = all(incoming[k] == old[k] for k in ('provider', 'email', 'mcpServer'))
            incoming['verified'] = bool(same_identity and old['verified'])
            runtime_settings.write(incoming)
            con.execute('UPDATE mail_sync_config SET enabled=0,history_since=?,next_run_at=? WHERE id=1',
                        (incoming['historySince'], mail_sync.next_hour()))
            row = con.execute('SELECT model,revision FROM state WHERE id=1').fetchone()
            model = json.loads(row['model']); model['timezone'] = incoming['timeZone']
            revision = row['revision'] + 1
            update_state(model, revision, con)
        await broadcast(revision)
        return {**incoming, 'locked': bool(locked)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post('/api/mail/verify')
async def verify_mail(request: Request):
    check_auth(request)
    try:
        before = runtime_settings.read()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not before['email']:
        raise HTTPException(400, '请先保存自己的邮箱设置')
    try:
        result = await mail_provider.verify(before)
    except (outlook_mcp.OutlookError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None
    if runtime_settings.read() != before:
        raise HTTPException(409, '验证期间设置已变化，请重新验证')
    runtime_settings.write({**before, 'verified': True})
    return result

@app.get("/api/ai/config")
async def ai_config(request: Request):
    check_auth(request)
    return {**ai_proxy.public_config(), "automaticCalls": request.app.state.mail_sync.status()['enabled']}

@app.put("/api/ai/config")
async def ai_save_config(request: Request):
    check_auth(request)
    body = await require_json(request, limit=4000)
    try:
        return {**ai_proxy.save_config(body), "automaticCalls": request.app.state.mail_sync.status()['enabled']}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

@app.get("/api/ai/models")
async def ai_models(request: Request):
    check_auth(request)
    return await ai_proxy.list_models()

@app.post("/api/ai/test")
async def ai_test(request: Request):
    check_auth(request)
    started = time.monotonic()
    result = await ai_proxy.complete(ai_proxy.read_config(), [
        {"role": "user", "content": "连接测试。请只回复 OK。"}
    ], max_tokens=32)
    return {"ok": True, "model": result["model"],
            "latencyMs": round((time.monotonic() - started) * 1000)}

@app.post("/api/ai/chat")
async def ai_chat(request: Request):
    check_auth(request)
    body = await require_json(request, limit=250_000)
    snapshot = read_snapshot() if isinstance(body, dict) and body.get("includeContext") is True else None
    try:
        messages = ai_proxy.chat_messages(body, snapshot)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return await ai_proxy.complete(ai_proxy.read_config(), messages)

if __name__=="__main__":
    import uvicorn
    uvicorn.run("server:app",host=os.getenv("ATLAS_BIND","127.0.0.1"),port=int(os.getenv("ATLAS_PORT","8765")),reload=False)
