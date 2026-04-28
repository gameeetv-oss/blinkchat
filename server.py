import asyncio, json, uuid, os, pathlib
from aiohttp import web, WSMsgType
import aiohttp

PORT = int(os.getenv("PORT", 8080))
BASE = pathlib.Path(__file__).parent

# [ws, interests, geo, gender, want_gender]
waiting_users = []
rooms = {}
user_rooms = {}
user_geo = {}
geo_cache = {}
online = 0


def country_to_flag(code):
    if not code or len(code) != 2:
        return "🌍"
    return chr(ord(code[0].upper()) + 127397) + chr(ord(code[1].upper()) + 127397)


async def get_geo(ip):
    if not ip or ip in ("127.0.0.1", "::1", "0.0.0.0", ""):
        return {"flag": "🖥️", "location": "Local"}
    if ip in geo_cache:
        return geo_cache[ip]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://ipapi.co/{ip}/json/",
                timeout=aiohttp.ClientTimeout(total=6),
                headers={"User-Agent": "blinkchat/1.0"}
            ) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    code = d.get("country_code", "")
                    flag = country_to_flag(code)
                    country = d.get("country_name", "Unknown")
                    city = d.get("city", "")
                    place = f"{country}, {city}" if city else country
                    result = {"flag": flag, "location": place}
                else:
                    result = {"flag": "🌍", "location": "Unknown"}
    except Exception:
        result = {"flag": "🌍", "location": "Unknown"}
    geo_cache[ip] = result
    return result


def find_best_match(my_interests, my_gender, want_gender):
    global waiting_users
    waiting_users = [u for u in waiting_users if not u[0].closed]

    compatible = []
    for i, (w, interests, geo, gender, their_want) in enumerate(waiting_users):
        # I want them
        if want_gender != "any" and gender != want_gender:
            continue
        # They want me
        if their_want != "any" and their_want != my_gender:
            continue
        compatible.append(i)

    if not compatible:
        return -1

    my_set = set(my_interests)
    best_idx, best_score = compatible[0], -1
    for i in compatible:
        _, interests, _, _, _ = waiting_users[i]
        score = len(my_set & set(interests)) if my_set and interests else 0
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


async def index(request):
    return web.FileResponse(BASE / "static" / "index.html")


async def sitemap(request):
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url>\n'
        '    <loc>https://blinkchat-k69c.onrender.com/</loc>\n'
        '    <lastmod>2026-04-25</lastmod>\n'
        '    <changefreq>weekly</changefreq>\n'
        '    <priority>1.0</priority>\n'
        '  </url>\n'
        '</urlset>'
    )
    return web.Response(text=xml, content_type="text/xml", headers={"Cache-Control": "no-cache"})


async def ping(request):
    return web.Response(text="ok")


async def ws_handler(request):
    global waiting_users, online
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    online += 1

    interests_raw = request.rel_url.query.get("interests", "")
    my_interests = [i.strip() for i in interests_raw.split(",") if i.strip()] if interests_raw else []

    my_gender = request.rel_url.query.get("gender", "other")       # male | female | other
    want_gender = request.rel_url.query.get("want", "any")          # male | female | any

    ip = request.headers.get("X-Forwarded-For", request.remote or "")
    if "," in ip:
        ip = ip.split(",")[0].strip()

    default_geo = {"flag": "🌍", "location": "Unknown"}
    geo = geo_cache.get(ip, default_geo)
    user_geo[id(ws)] = geo

    async def fetch_geo_bg():
        result = await get_geo(ip)
        user_geo[id(ws)] = result
        try:
            if not ws.closed:
                await ws.send_json({"type": "your_location", "flag": result["flag"], "location": result["location"]})
        except Exception:
            pass

    if ip and ip not in geo_cache and ip not in ("127.0.0.1", "::1", "0.0.0.0", ""):
        asyncio.create_task(fetch_geo_bg())
    else:
        try:
            await ws.send_json({"type": "your_location", "flag": geo["flag"], "location": geo["location"]})
        except Exception:
            pass

    idx = find_best_match(my_interests, my_gender, want_gender)
    if idx >= 0:
        partner_ws, partner_interests, _, p_gender, _ = waiting_users.pop(idx)
        partner_geo = user_geo.get(id(partner_ws), {"flag": "🌍", "location": "Unknown"})
        rid = str(uuid.uuid4())
        rooms[rid] = [ws, partner_ws]
        user_rooms[id(ws)] = rid
        user_rooms[id(partner_ws)] = rid
        common = list(set(my_interests) & set(partner_interests))
        await ws.send_json({
            "type": "matched", "role": "answerer", "online": online,
            "partner_flag": partner_geo["flag"], "partner_location": partner_geo["location"],
            "common_interests": common
        })
        await partner_ws.send_json({
            "type": "matched", "role": "offerer", "online": online,
            "partner_flag": geo["flag"], "partner_location": geo["location"],
            "common_interests": common
        })
    else:
        waiting_users.append([ws, my_interests, geo, my_gender, want_gender])
        await ws.send_json({"type": "waiting", "online": online})

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
                if data.get("type") == "my_location":
                    if not partner.closed:
                        await partner.send_json({
                            "type": "partner_location",
                            "flag": data.get("flag", "🌍"),
                            "location": data.get("location", "Unknown")
                        })
                elif not partner.closed:
                    await partner.send_str(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        online = max(0, online - 1)
        user_geo.pop(id(ws), None)
        waiting_users[:] = [u for u in waiting_users if u[0] is not ws]
        rid = user_rooms.pop(id(ws), None)
        if rid and rid in rooms:
            room = rooms.pop(rid)
            partner = room[1] if room[0] is ws else room[0]
            user_rooms.pop(id(partner), None)
            if not partner.closed:
                await partner.send_json({"type": "partner_left"})
    return ws


async def blinkchat_keepalive():
    await asyncio.sleep(60)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get("https://blinkchat-k69c.onrender.com/ping", timeout=aiohttp.ClientTimeout(total=10))
        except Exception:
            pass
        await asyncio.sleep(480)


async def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ping", ping)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/googlee2b500dcde5fee75.html", lambda r: web.FileResponse(BASE / "static" / "googlee2b500dcde5fee75.html"))
    app.router.add_get("/sitemap.xml", sitemap)
    app.router.add_get("/robots.txt", lambda r: web.FileResponse(BASE / "static" / "robots.txt"))
    app.router.add_static("/icons", BASE / "static" / "icons")
    app.router.add_static("/static", BASE / "static")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Blinkchat running on port {PORT}")
    asyncio.create_task(blinkchat_keepalive())
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
