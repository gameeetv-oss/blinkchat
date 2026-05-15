import asyncio, json, uuid, os, pathlib, logging, time, re, secrets
from aiohttp import web, WSMsgType
import aiosqlite
import jwt
import bcrypt

logging.basicConfig(level=logging.INFO)

PORT = int(os.getenv("PORT", 8080))
BASE = pathlib.Path(__file__).parent
DB_PATH = BASE / "data" / "vibe.db"
JWT_SECRET = os.getenv("JWT_SECRET", "vibe-secret-change-in-prod-2026")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@vibeapp.co")
APP_URL = os.getenv("APP_URL", "https://blinkchat-k69c.onrender.com")
WHOP_API_KEY = os.getenv("WHOP_API_KEY", "")
WHOP_WEBHOOK_SECRET = os.getenv("WHOP_WEBHOOK_SECRET", "")
WHOP_PLAN_ID = os.getenv("WHOP_PLAN_ID", "")   # plan_xxxxxx
FREE_DAILY_LIKES = 15

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reset_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS premium (
                user_id TEXT PRIMARY KEY,
                expires_at REAL NOT NULL,
                source TEXT DEFAULT 'whop'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_likes (
                user_id TEXT PRIMARY KEY,
                count INTEGER DEFAULT 0,
                date TEXT NOT NULL
            )
        """)
        # Add new columns if they don't exist
        for col_sql in [
            "ALTER TABLE users ADD COLUMN lat REAL DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN lng REAL DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN orientation TEXT DEFAULT 'straight'",
            "ALTER TABLE users ADD COLUMN looking_for TEXT DEFAULT 'opposite'",
            "ALTER TABLE users ADD COLUMN passport_lat REAL DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN passport_lng REAL DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN country TEXT DEFAULT ''",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass
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


# ── EMAIL ─────────────────────────────────────────────────

async def send_email(to: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        logging.warning(f"[EMAIL] No RESEND_API_KEY — would send to {to}: {subject}")
        return True
    import aiohttp as _aiohttp
    try:
        async with _aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": FROM_EMAIL, "to": [to], "subject": subject, "html": html},
                timeout=_aiohttp.ClientTimeout(total=10)
            ) as r:
                return r.status in (200, 201)
    except Exception as e:
        logging.error(f"[EMAIL] Send failed: {e}")
        return False


import math

def _default_looking_for(gender: str, orientation: str) -> str:
    if orientation == "gay":
        return "men" if gender == "male" else "women"
    if orientation == "bisexual":
        return "both"
    # straight / other
    if gender == "male":
        return "women"
    if gender == "female":
        return "men"
    return "both"

def _haversine(lat1, lng1, lat2, lng2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def _matches_looking_for(me, them) -> bool:
    my_lf = me["looking_for"] or "both"
    their_lf = them["looking_for"] or "both"
    my_gender = me["gender"]
    their_gender = them["gender"]

    def wants(lf, gender):
        if lf == "both": return True
        if lf == "men": return gender == "male"
        if lf == "women": return gender == "female"
        return True

    return wants(my_lf, their_gender) and wants(their_lf, my_gender)


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
    orientation = str(d.get("orientation", "straight")).strip()
    looking_for = str(d.get("looking_for", "opposite")).strip()

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
    if orientation not in ("straight", "gay", "bisexual", "other"):
        orientation = "straight"
    if looking_for not in ("men", "women", "both"):
        looking_for = _default_looking_for(gender, orientation)

    uid = str(uuid.uuid4())
    pw_hash = hash_pw(password)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users (id,email,password_hash,name,age,gender,orientation,looking_for,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, email, pw_hash, name, int(age), gender, orientation, looking_for, time.time())
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


async def forgot_password(request):
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(d.get("email", "")).strip().lower()
    if not email:
        return web.json_response({"error": "Email gerekli"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name FROM users WHERE email=?", (email,)) as cur:
            row = await cur.fetchone()

    # Always return success to prevent email enumeration
    if row:
        token = secrets.token_urlsafe(32)
        expires = time.time() + 3600  # 1 hour

        async with aiosqlite.connect(DB_PATH) as db:
            # Remove old tokens for this user
            await db.execute("DELETE FROM reset_tokens WHERE user_id=?", (row["id"],))
            await db.execute(
                "INSERT INTO reset_tokens (token, user_id, expires_at) VALUES (?,?,?)",
                (token, row["id"], expires)
            )
            await db.commit()

        reset_link = f"{APP_URL}/reset?token={token}"
        await send_email(
            to=email,
            subject="Vibe – Şifrenizi Sıfırlayın",
            html=f"""
            <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px;background:#0d0d14;color:#fff;border-radius:16px">
              <h2 style="color:#e94560;margin-bottom:8px">Şifre Sıfırlama</h2>
              <p style="color:#aaa;margin-bottom:24px">Merhaba {row['name']}, aşağıdaki butona tıklayarak şifreni sıfırlayabilirsin.</p>
              <a href="{reset_link}" style="display:inline-block;background:linear-gradient(135deg,#e94560,#c73652);color:#fff;padding:14px 28px;border-radius:12px;text-decoration:none;font-weight:700">Şifremi Sıfırla</a>
              <p style="color:#666;font-size:13px;margin-top:24px">Bu link 1 saat geçerlidir. Eğer bu isteği sen yapmadıysan bu emaili görmezden gelebilirsin.</p>
            </div>"""
        )

    return web.json_response({"ok": True})


async def reset_password_page(request):
    token = request.rel_url.query.get("token", "")
    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vibe – Şifre Sıfırla</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d0d14;color:#fff;font-family:-apple-system,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}}
.box{{background:#1e1e30;border-radius:20px;padding:36px;width:100%;max-width:400px}}
h2{{color:#e94560;margin-bottom:6px}}
p{{color:#888;font-size:14px;margin-bottom:28px}}
input{{width:100%;padding:14px 16px;background:#14141f;border:1.5px solid #252535;border-radius:12px;color:#fff;font-size:16px;outline:none;margin-bottom:14px}}
input:focus{{border-color:#e94560}}
button{{width:100%;padding:15px;background:linear-gradient(135deg,#e94560,#c73652);color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:700;cursor:pointer}}
.msg{{padding:12px 16px;border-radius:10px;font-size:14px;margin-bottom:14px;display:none}}
.msg.ok{{background:#0a2a10;border:1px solid #1a5020;color:#6fdb7f}}
.msg.err{{background:#2a0a10;border:1px solid #5a1020;color:#ff6b6b}}
</style>
</head>
<body>
<div class="box">
  <h2>Yeni Şifre Belirle</h2>
  <p>En az 6 karakter uzunluğunda bir şifre seç</p>
  <div id="msg" class="msg"></div>
  <input type="password" id="pw1" placeholder="Yeni şifre" autocomplete="new-password">
  <input type="password" id="pw2" placeholder="Şifreyi tekrar gir" autocomplete="new-password">
  <button onclick="submit()">Şifremi Güncelle</button>
</div>
<script>
async function submit() {{
  const pw1 = document.getElementById('pw1').value;
  const pw2 = document.getElementById('pw2').value;
  const msg = document.getElementById('msg');
  msg.style.display = 'none';
  if (pw1.length < 6) {{ showMsg('err', 'Şifre en az 6 karakter olmalı'); return; }}
  if (pw1 !== pw2) {{ showMsg('err', 'Şifreler eşleşmiyor'); return; }}
  const res = await fetch('/api/reset-password', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{token: '{token}', password: pw1}})
  }});
  const data = await res.json();
  if (res.ok) {{
    showMsg('ok', 'Şifren güncellendi! Uygulamadan giriş yapabilirsin.');
    document.querySelector('button').disabled = true;
    document.getElementById('pw1').disabled = true;
    document.getElementById('pw2').disabled = true;
  }} else {{
    showMsg('err', data.error || 'Bir hata oluştu');
  }}
}}
function showMsg(type, text) {{
  const el = document.getElementById('msg');
  el.className = 'msg ' + type;
  el.textContent = text;
  el.style.display = 'block';
}}
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def do_reset_password(request):
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token = str(d.get("token", ""))
    password = str(d.get("password", ""))

    if not token or len(password) < 6:
        return web.json_response({"error": "Geçersiz istek"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reset_tokens WHERE token=?", (token,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return web.json_response({"error": "Geçersiz veya kullanılmış link"}, status=400)
        if time.time() > row["expires_at"]:
            await db.execute("DELETE FROM reset_tokens WHERE token=?", (token,))
            await db.commit()
            return web.json_response({"error": "Link süresi dolmuş, tekrar isteyin"}, status=400)

        new_hash = hash_pw(password)
        await db.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, row["user_id"]))
        await db.execute("DELETE FROM reset_tokens WHERE token=?", (token,))
        await db.commit()

    return web.json_response({"ok": True})


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
    my_lat = me["passport_lat"] or me["lat"]
    my_lng = me["passport_lng"] or me["lng"]
    max_km = 150  # default radius

    results = []
    for row in rows:
        their_mode = row["mode"]
        if my_mode == "dating" and their_mode == "friendship":
            continue
        if my_mode == "friendship" and their_mode == "dating":
            continue

        # Orientation / looking_for filter
        if not _matches_looking_for(me, row):
            continue

        # Distance filter (only if both have location)
        distance_km = None
        if my_lat and my_lng and row["lat"] and row["lng"]:
            distance_km = _haversine(my_lat, my_lng, row["lat"], row["lng"])
            if distance_km > max_km:
                continue

        results.append({
            "id": row["id"],
            "name": row["name"],
            "age": row["age"],
            "gender": row["gender"],
            "mode": row["mode"],
            "city": row["city"] or "",
            "country": row["country"] or "",
            "interests": json.loads(row["interests"] or "[]"),
            "has_photo": bool(row["photo_b64"]),
            "photo_b64": row["photo_b64"] or "",
            "voice_b64": row["voice_b64"] or "",
            "distance_km": round(distance_km) if distance_km else None,
        })

    return web.json_response(results[:20])


# ── PREMIUM HELPERS ───────────────────────────────────────

async def is_premium(uid: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT expires_at FROM premium WHERE user_id=?", (uid,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row and row[0] > time.time())


async def get_likes_today(uid: str) -> int:
    today = time.strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count, date FROM daily_likes WHERE user_id=?", (uid,)
        ) as cur:
            row = await cur.fetchone()
    if not row or row[1] != today:
        return 0
    return row[0]


async def increment_likes_today(uid: str):
    today = time.strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count, date FROM daily_likes WHERE user_id=?", (uid,)
        ) as cur:
            row = await cur.fetchone()
        if not row or row[1] != today:
            await db.execute(
                "INSERT OR REPLACE INTO daily_likes (user_id, count, date) VALUES (?,1,?)",
                (uid, today)
            )
        else:
            await db.execute(
                "UPDATE daily_likes SET count=count+1 WHERE user_id=?", (uid,)
            )
        await db.commit()


async def grant_premium(email: str, days: int, source: str = "whop") -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM users WHERE email=?", (email.lower(),)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        uid = row[0]
        expires = time.time() + days * 86400
        await db.execute(
            "INSERT OR REPLACE INTO premium (user_id, expires_at, source) VALUES (?,?,?)",
            (uid, expires, source)
        )
        await db.commit()
    return True


# ── API: PREMIUM ───────────────────────────────────────────

@require_auth
async def get_premium_status(request):
    uid = request["uid"]
    premium = await is_premium(uid)
    likes_today = await get_likes_today(uid)
    remaining = None if premium else max(0, FREE_DAILY_LIKES - likes_today)
    return web.json_response({
        "is_premium": premium,
        "likes_remaining": remaining,   # None = unlimited
        "free_limit": FREE_DAILY_LIKES,
    })


@require_auth
async def who_liked_me(request):
    uid = request["uid"]
    if not await is_premium(uid):
        return web.json_response({"error": "premium_required"}, status=403)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT u.id, u.name, u.age, u.gender, u.city, u.photo_b64, u.mode
            FROM users u
            JOIN likes l ON l.from_id = u.id
            WHERE l.to_id = ?
              AND u.id NOT IN (SELECT to_id FROM likes WHERE from_id = ?)
              AND u.id NOT IN (SELECT to_id FROM passes WHERE from_id = ?)
            ORDER BY l.created_at DESC LIMIT 50
        """, (uid, uid, uid)) as cur:
            rows = await cur.fetchall()

    return web.json_response([{
        "id": r["id"], "name": r["name"], "age": r["age"],
        "gender": r["gender"], "city": r["city"] or "",
        "photo_b64": r["photo_b64"] or "", "mode": r["mode"]
    } for r in rows])


# ── WEBHOOK: WHOP ──────────────────────────────────────────

async def whop_webhook(request):
    import hmac, hashlib
    body = await request.read()

    if WHOP_WEBHOOK_SECRET:
        sig = request.headers.get("x-whop-signature", "")
        expected = hmac.new(WHOP_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return web.Response(status=401)

    try:
        data = json.loads(body)
    except Exception:
        return web.Response(status=400)

    event = data.get("event") or data.get("action", "")
    membership = data.get("data", {})

    if event in ("membership.went_valid", "membership.created", "payment.succeeded"):
        email = (membership.get("user", {}) or {}).get("email", "")
        if not email:
            email = membership.get("email", "")
        if email:
            await grant_premium(email, days=31, source="whop")
            logging.info(f"[WHOP] Premium granted: {email}")

    elif event in ("membership.went_invalid", "membership.expired"):
        email = (membership.get("user", {}) or {}).get("email", "")
        if email:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT id FROM users WHERE email=?", (email.lower(),)) as cur:
                    row = await cur.fetchone()
                if row:
                    await db.execute("DELETE FROM premium WHERE user_id=?", (row[0],))
                    await db.commit()

    return web.Response(text="ok")


@require_auth
async def verify_apple_iap(request):
    """iOS: RevenueCat server-to-server verification."""
    uid = request["uid"]
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # RevenueCat sends entitlements after verifying receipt with Apple
    # Frontend calls this after successful purchase via @revenuecat/purchases-capacitor
    rc_user_id = d.get("rc_user_id", "")
    product_id = d.get("product_id", "")
    expires_date = d.get("expires_date")  # Unix timestamp ms

    if not rc_user_id or not product_id:
        return web.json_response({"error": "Eksik veri"}, status=400)

    days = 366 if "annual" in product_id.lower() or "yearly" in product_id.lower() else 32

    async with aiosqlite.connect(DB_PATH) as db:
        expires = (expires_date / 1000) if expires_date else (time.time() + days * 86400)
        await db.execute(
            "INSERT OR REPLACE INTO premium (user_id, expires_at, source) VALUES (?,?,?)",
            (uid, expires, "apple_iap")
        )
        await db.commit()

    return web.json_response({"ok": True, "is_premium": True})


# ── API: LIKE / PASS ──────────────────────────────────────

@require_auth
async def like_user(request):
    uid = request["uid"]
    target_id = request.match_info["target_id"]

    if uid == target_id:
        return web.json_response({"error": "Kendini beğenemezsin"}, status=400)

    # Daily like limit for free users
    if not await is_premium(uid):
        likes_today = await get_likes_today(uid)
        if likes_today >= FREE_DAILY_LIKES:
            return web.json_response({"error": "limit_reached", "limit": FREE_DAILY_LIKES}, status=429)

    await increment_likes_today(uid)

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


@require_auth
async def update_location(request):
    uid = request["uid"]
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    lat = d.get("lat")
    lng = d.get("lng")
    country = str(d.get("country", "")).strip()
    if lat is None or lng is None:
        return web.json_response({"error": "lat/lng required"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET lat=?,lng=?,country=? WHERE id=?", (lat, lng, country, uid))
        await db.commit()
    return web.json_response({"ok": True})


@require_auth
async def set_passport(request):
    uid = request["uid"]
    premium = await is_premium(uid)
    if not premium:
        return web.json_response({"error": "Premium gerekli"}, status=403)
    try:
        d = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    lat = d.get("lat")
    lng = d.get("lng")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET passport_lat=?,passport_lng=? WHERE id=?", (lat, lng, uid))
        await db.commit()
    return web.json_response({"ok": True})


@require_auth
async def delete_account(request):
    uid = request["uid"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE id=?", (uid,))
        await db.execute("DELETE FROM likes WHERE from_id=? OR to_id=?", (uid, uid))
        await db.execute("DELETE FROM passes WHERE from_id=? OR to_id=?", (uid, uid))
        await db.execute("DELETE FROM blocks WHERE from_id=? OR to_id=?", (uid, uid))
        await db.execute("DELETE FROM reports WHERE from_id=? OR to_id=?", (uid, uid))
        await db.execute("DELETE FROM premium WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM daily_likes WHERE user_id=?", (uid,))
        await db.commit()
    return web.json_response({"ok": True})


async def privacy_policy(request):
    html = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vibe – Privacy Policy</title>
<style>body{font-family:-apple-system,sans-serif;max-width:720px;margin:0 auto;padding:24px;color:#333;line-height:1.7}h1{color:#e94560}h2{margin-top:32px}a{color:#e94560}</style>
</head><body>
<h1>Vibe – Privacy Policy</h1>
<p><em>Last updated: May 2026</em></p>
<p>Vibe ("we", "us") is a voice-first dating and friendship app. This policy explains how we collect and use your data.</p>
<h2>1. Age Requirement</h2>
<p>Vibe is strictly for users 18 years of age or older. By registering, you confirm you are at least 18. We do not knowingly collect data from anyone under 18. If we discover an underage account, it will be immediately deleted.</p>
<h2>2. Data We Collect</h2>
<ul>
<li>Name, age, gender, email address</li>
<li>City, interests, profile photo, voice clip (30 seconds)</li>
<li>Like/pass interactions and match history</li>
</ul>
<h2>3. How We Use Your Data</h2>
<ul>
<li>To show your profile to potential matches</li>
<li>To send password reset emails</li>
<li>To manage premium subscriptions</li>
</ul>
<h2>4. Data Sharing</h2>
<p>We do not sell your personal data. We share data only with: Apple (in-app purchases via RevenueCat), Resend (transactional email only).</p>
<h2>5. Data Retention & Deletion</h2>
<p>You can delete your account at any time from the Profile tab. All your data is permanently deleted within 30 days.</p>
<h2>6. Safety</h2>
<p>Users can block and report other users at any time. We review all reports and take appropriate action.</p>
<h2>7. Contact</h2>
<p>For privacy questions or data deletion requests: <a href="mailto:support@vibeapp.co">support@vibeapp.co</a></p>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


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
    app.router.add_post("/api/forgot-password", forgot_password)
    app.router.add_get("/reset", reset_password_page)
    app.router.add_post("/api/reset-password", do_reset_password)
    app.router.add_get("/api/premium-status", get_premium_status)
    app.router.add_get("/api/who-liked-me", who_liked_me)
    app.router.add_post("/api/verify-apple-iap", verify_apple_iap)
    app.router.add_post("/webhooks/whop", whop_webhook)
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
    app.router.add_delete("/api/account", delete_account)
    app.router.add_put("/api/location", update_location)
    app.router.add_put("/api/passport", set_passport)
    app.router.add_get("/privacy", privacy_policy)
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
