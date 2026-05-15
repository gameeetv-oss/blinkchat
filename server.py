import asyncio, json, uuid, os, pathlib, logging, time, re
from aiohttp import web, WSMsgType
import aiosqlite
import jwt
import bcrypt

logging.basicConfig(level=logging.INFO)

PORT = int(os.getenv("PORT", 8080))
BASE = pathlib.Path(__file__).parent
DB_PATH = BASE / "data" / "vibe.db"
JWT_SECRET = os.getenv("JWT_SECRET", "vibe-secret-change-in-prod-2026")

# WebRTC signaling: matched users only
rooms = {}         # room_id -> [ws1, ws2]
user_rooms = {}    # id(ws) -> room_id
waiting_calls = {} # user_id -> ws


# ── DATABASE ──────────────────────────────────────────────

async def init_db():
    os.makedirs(DB_PATH.parent, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                age INTEGER NOT NULL,
                gender TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'both',
                city TEXT DEFAULT '',
                interests TEXT DEFAULT '[]',
                voice_b64 TEXT DEFAULT '',
                photo_b64 TEXT DEFAULT '',
                setup_done INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS likes (
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (from_id, to_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS passes (
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                PRIMARY KEY (from_id, to_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                PRIMARY KEY (from_id, to_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                reason TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        await db.commit()


# ── AUTH HELPERS ──────────────────────────────────────────

def make_token(user_id: str) -> str:
    return jwt.encode({"uid": user_id, "iat": int(time.time())}, JWT_SECRET, algorithm="HS256")

def decode_token(token: str):
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"]).get("uid")
    except Exception:
        return None

def get_uid(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return decode_token(auth[7:])
    token = request.rel_url.query.get("token")
    if token:
        return decode_token(token)
    return None

def require_auth(handler):
    async def wrapper(request):
        uid = get_uid(request)
        if not uid:
            return web.json_response({"error": "Unauthorized"}, status=401)
        request["uid"] = uid
        return await handler(request)
    return wrapper

def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


# ── API: AUTH ─────────────────────────────────────────────

async def register(request):
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(d.get("email", "")).strip().lower()
    password = str(d.get("password", ""))
    name = str(d.get("name", "")).strip()
    age = d.get("age")
    gender = str(d.get("gender", "")).strip()

    if not email or not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return web.json_response({"error": "Geçerli bir email gir"}, status=400)
    if len(password) < 6:
        return web.json_response({"error": "Şifre en az 6 karakter olmalı"}, status=400)
    if not name or len(name) < 2:
        return web.json_response({"error": "İsim gerekli"}, status=400)
    if not age or not str(age).isdigit() or not (18 <= int(age) <= 99):
        return web.json_response({"error": "Yaş 18-99 arasında olmalı"}, status=400)
    if gender not in ("male", "female", "other"):
        return web.json_response({"error": "Cinsiyet seçilmeli"}, status=400)

    uid = str(uuid.uuid4())
    pw_hash = hash_pw(password)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users (id,email,password_hash,name,age,gender,created_at) VALUES (?,?,?,?,?,?,?)",
                (uid, email, pw_hash, name, int(age), gender, time.time())
            )
            await db.commit()
    except aiosqlite.IntegrityError:
        return web.json_response({"error": "Bu email zaten kayıtlı"}, status=409)

    return web.json_response({"token": make_token(uid), "uid": uid})


async def login(request):
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(d.get("email", "")).strip().lower()
    password = str(d.get("password", ""))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE email=?", (email,)) as cur:
            row = await cur.fetchone()

    if not row or not check_pw(password, row["password_hash"]):
        return web.json_response({"error": "Email veya şifre hatalı"}, status=401)

    return web.json_response({"token": make_token(row["id"]), "uid": row["id"]})


# ── API: PROFILE ──────────────────────────────────────────

def user_to_dict(row, include_contact=False):
    d = {
        "id": row["id"],
        "name": row["name"],
        "age": row["age"],
        "gender": row["gender"],
        "mode": row["mode"],
        "city": row["city"] or "",
        "interests": json.loads(row["interests"] or "[]"),
        "has_voice": bool(row["voice_b64"]),
        "has_photo": bool(row["photo_b64"]),
        "setup_done": bool(row["setup_done"]),
    }
    if include_contact:
        d["voice_b64"] = row["voice_b64"] or ""
        d["photo_b64"] = row["photo_b64"] or ""
        d["email"] = row["email"]
    return d


@require_auth
async def get_me(request):
    uid = request["uid"]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as cur:
            row = await cur.fetchone()
    if not row:
        return web.json_response({"error": "User not found"}, status=404)
    return web.json_response(user_to_dict(row, include_contact=True))


@require_auth
async def update_profile(request):
    uid = request["uid"]
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    allowed = ["name", "age", "city", "interests", "mode", "gender"]
    updates = {}
    for k in allowed:
        if k in d:
            updates[k] = d[k]

    if "interests" in updates:
        updates["interests"] = json.dumps(updates["interests"][:10])
    if "age" in updates:
        age = int(updates["age"])
        if not (18 <= age <= 99):
            return web.json_response({"error": "Geçersiz yaş"}, status=400)
        updates["age"] = age
    if "mode" in updates and updates["mode"] not in ("dating", "friendship", "both"):
        return web.json_response({"error": "Geçersiz mod"}, status=400)

    if not updates:
        return web.json_response({"error": "Güncellenecek alan yok"}, status=400)

    set_clause = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [uid]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {set_clause} WHERE id=?", vals)
        await db.commit()

    return web.json_response({"ok": True})


@require_auth
async def upload_voice(request):
    uid = request["uid"]
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    voice_b64 = str(d.get("voice_b64", ""))
    if not voice_b64:
        return web.json_response({"error": "Ses kaydı boş"}, status=400)
    if len(voice_b64) > 3_000_000:
        return web.json_response({"error": "Ses kaydı çok büyük (max 30sn)"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET voice_b64=?, setup_done=1 WHERE id=?", (voice_b64, uid))
        await db.commit()

    return web.json_response({"ok": True})


@require_auth
async def upload_photo(request):
    uid = request["uid"]
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    photo_b64 = str(d.get("photo_b64", ""))
    if len(photo_b64) > 2_000_000:
        return web.json_response({"error": "Fotoğraf çok büyük"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET photo_b64=? WHERE id=?", (photo_b64, uid))
        await db.commit()

    return web.json_response({"ok": True})


@require_auth
async def mark_setup_done(request):
    uid = request["uid"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET setup_done=1 WHERE id=?", (uid,))
        await db.commit()
    return web.json_response({"ok": True})


# ── API: DISCOVER ─────────────────────────────────────────

@require_auth
async def discover(request):
    uid = request["uid"]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as cur:
            me = await cur.fetchone()
        if not me:
            return web.json_response([], status=200)

        async with db.execute("SELECT to_id FROM likes WHERE from_id=?", (uid,)) as cur:
            liked = {r[0] for r in await cur.fetchall()}
        async with db.execute("SELECT to_id FROM passes WHERE from_id=?", (uid,)) as cur:
            passed = {r[0] for r in await cur.fetchall()}
        async with db.execute(
            "SELECT to_id FROM blocks WHERE from_id=? UNION SELECT from_id FROM blocks WHERE to_id=?",
            (uid, uid)
        ) as cur:
            blocked = {r[0] for r in await cur.fetchall()}

        exclude = liked | passed | blocked | {uid}

        placeholders = ",".join("?" * len(exclude))
        async with db.execute(
            f"SELECT * FROM users WHERE id NOT IN ({placeholders}) AND setup_done=1 ORDER BY created_at DESC LIMIT 50",
            list(exclude)
        ) as cur:
            rows = await cur.fetchall()

    my_mode = me["mode"]
    results = []
    for row in rows:
        their_mode = row["mode"]
        if my_mode == "dating" and their_mode == "friendship":
            continue
        if my_mode == "friendship" and their_mode == "dating":
            continue
        results.append({
            "id": row["id"],
            "name": row["name"],
            "age": row["age"],
            "gender": row["gender"],
            "mode": row["mode"],
            "city": row["city"] or "",
            "interests": json.loads(row["interests"] or "[]"),
            "has_photo": bool(row["photo_b64"]),
            "photo_b64": row["photo_b64"] or "",
            "voice_b64": row["voice_b64"] or "",
        })

    return web.json_response(results[:20])


# ── API: LIKE / PASS ──────────────────────────────────────

@require_auth
async def like_user(request):
    uid = request["uid"]
    target_id = request.match_info["target_id"]

    if uid == target_id:
        return web.json_response({"error": "Kendini beğenemezsin"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute("SELECT id FROM users WHERE id=?", (target_id,)) as cur:
            if not await cur.fetchone():
                return web.json_response({"error": "Kullanıcı bulunamadı"}, status=404)

        await db.execute(
            "INSERT OR IGNORE INTO likes (from_id, to_id, created_at) VALUES (?,?,?)",
            (uid, target_id, time.time())
        )
        await db.commit()

        async with db.execute(
            "SELECT 1 FROM likes WHERE from_id=? AND to_id=?", (target_id, uid)
        ) as cur:
            matched = await cur.fetchone() is not None

    return web.json_response({"matched": matched})


@require_auth
async def pass_user(request):
    uid = request["uid"]
    target_id = request.match_info["target_id"]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO passes (from_id, to_id) VALUES (?,?)",
            (uid, target_id)
        )
        await db.commit()

    return web.json_response({"ok": True})


# ── API: MATCHES ──────────────────────────────────────────

@require_auth
async def get_matches(request):
    uid = request["uid"]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.* FROM users u
            WHERE u.id IN (SELECT from_id FROM likes WHERE to_id=?)
              AND u.id IN (SELECT to_id FROM likes WHERE from_id=?)
            ORDER BY u.created_at DESC
        """, (uid, uid)) as cur:
            rows = await cur.fetchall()

    return web.json_response([{
        "id": row["id"],
        "name": row["name"],
        "age": row["age"],
        "gender": row["gender"],
        "mode": row["mode"],
        "city": row["city"] or "",
        "photo_b64": row["photo_b64"] or "",
    } for row in rows])


# ── API: REPORT / BLOCK ───────────────────────────────────

@require_auth
async def report_user(request):
    uid = request["uid"]
    target_id = request.match_info["target_id"]
    try:
        d = await request.json()
        reason = str(d.get("reason", ""))[:200]
    except Exception:
        reason = ""

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reports (id,from_id,to_id,reason,created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), uid, target_id, reason, time.time())
        )
        await db.execute("INSERT OR IGNORE INTO blocks (from_id,to_id) VALUES (?,?)", (uid, target_id))
        await db.commit()

    return web.json_response({"ok": True})


@require_auth
async def block_user(request):
    uid = request["uid"]
    target_id = request.match_info["target_id"]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO blocks (from_id,to_id) VALUES (?,?)", (uid, target_id))
        await db.commit()

    return web.json_response({"ok": True})


# ── WEBSOCKET: VIDEO CALL SIGNALING ───────────────────────

async def ws_handler(request):
    uid = get_uid(request)
    if not uid:
        return web.Response(status=401)

    partner_id = request.rel_url.query.get("partner_id")
    if not partner_id:
        return web.Response(status=400)

    # Verify mutual match
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM likes l1
               JOIN likes l2 ON l2.from_id=? AND l2.to_id=?
               WHERE l1.from_id=? AND l1.to_id=?""",
            (partner_id, uid, uid, partner_id)
        ) as cur:
            if not await cur.fetchone():
                return web.Response(status=403)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    if partner_id in waiting_calls:
        partner_ws = waiting_calls.pop(partner_id)
        if not partner_ws.closed:
            rid = str(uuid.uuid4())
            rooms[rid] = [ws, partner_ws]
            user_rooms[id(ws)] = rid
            user_rooms[id(partner_ws)] = rid
            await ws.send_json({"type": "call_ready", "role": "answerer"})
            await partner_ws.send_json({"type": "call_ready", "role": "offerer"})
        else:
            waiting_calls[uid] = ws
            await ws.send_json({"type": "waiting"})
    else:
        waiting_calls[uid] = ws
        await ws.send_json({"type": "waiting"})

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                rid = user_rooms.get(id(ws))
                if not rid or rid not in rooms:
                    continue
                room = rooms[rid]
                partner = room[1] if room[0] is ws else room[0]
                if not partner.closed:
                    await partner.send_str(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        waiting_calls.pop(uid, None)
        rid = user_rooms.pop(id(ws), None)
        if rid and rid in rooms:
            room = rooms.pop(rid)
            partner = room[1] if room[0] is ws else room[0]
            user_rooms.pop(id(partner), None)
            if not partner.closed:
                await partner.send_json({"type": "call_ended"})

    return ws


# ── STATIC / MISC ─────────────────────────────────────────

async def index(request):
    return web.FileResponse(BASE / "static" / "index.html")

async def ping(request):
    return web.Response(text="ok")

async def keepalive():
    await asyncio.sleep(60)
    import aiohttp
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get("https://blinkchat-k69c.onrender.com/ping", timeout=aiohttp.ClientTimeout(total=10))
        except Exception:
            pass
        await asyncio.sleep(480)


# ── MAIN ──────────────────────────────────────────────────

async def main():
    await init_db()

    app = web.Application(client_max_size=5 * 1024 * 1024)

    app.router.add_post("/api/register", register)
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/me", get_me)
    app.router.add_put("/api/profile", update_profile)
    app.router.add_post("/api/voice", upload_voice)
    app.router.add_post("/api/photo", upload_photo)
    app.router.add_post("/api/setup-done", mark_setup_done)
    app.router.add_get("/api/discover", discover)
    app.router.add_post("/api/like/{target_id}", like_user)
    app.router.add_post("/api/pass/{target_id}", pass_user)
    app.router.add_get("/api/matches", get_matches)
    app.router.add_post("/api/report/{target_id}", report_user)
    app.router.add_post("/api/block/{target_id}", block_user)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", index)
    app.router.add_get("/ping", ping)
    app.router.add_static("/icons", BASE / "static" / "icons")
    app.router.add_static("/static", BASE / "static")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Vibe running on port {PORT}")
    asyncio.create_task(keepalive())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
