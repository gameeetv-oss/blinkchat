import asyncio, json, uuid, os, pathlib
from aiohttp import web, WSMsgType
import aiohttp

PORT = int(os.getenv("PORT", 8080))
BASE = pathlib.Path(__file__).parent

waiting_user = None
rooms = {}
user_rooms = {}
user_geo = {}      # id(ws) -> {"flag": "🇹🇷", "location": "Turkey, Istanbul"}
geo_cache = {}     # ip -> geo dict
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
                    region = d.get("region", "")
                    # İl seviyesi: city + country (ilçe zaten city içinde)
                    place = f"{country}, {city}" if city else country
                    result = {"flag": flag, "location": place}
                else:
                    result = {"flag": "🌍", "location": "Unknown"}
    except Exception:
        result = {"flag": "🌍", "location": "Unknown"}
    geo_cache[ip] = result
    return result


async def index(request):
    return web.FileResponse(BASE / "static" / "index.html")


async def ping(request):
    return web.Response(text="ok")


async def ws_handler(request):
    global waiting_user, online
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    online += 1

    # Gerçek IP (Render load balancer arkasında)
    ip = request.headers.get("X-Forwarded-For", request.remote or "")
    if "," in ip:
        ip = ip.split(",")[0].strip()

    # Cache'te varsa anında al, yoksa arka planda yükle — bağlantıyı geciktirme
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
        # Cache'ten geldi, hemen gönder
        try:
            await ws.send_json({"type": "your_location", "flag": geo["flag"], "location": geo["location"]})
        except Exception:
            pass

    if waiting_user and not waiting_user.closed:
        partner = waiting_user
        waiting_user = None
        partner_geo = user_geo.get(id(partner), {"flag": "🌍", "location": "Unknown"})
        rid = str(uuid.uuid4())
        rooms[rid] = [ws, partner]
        user_rooms[id(ws)] = rid
        user_rooms[id(partner)] = rid
        # Her kullanıcıya karşısındakinin konumunu gönder
        await ws.send_json({
            "type": "matched", "role": "answerer", "online": online,
            "partner_flag": partner_geo["flag"], "partner_location": partner_geo["location"]
        })
        await partner.send_json({
            "type": "matched", "role": "offerer", "online": online,
            "partner_flag": geo["flag"], "partner_location": geo["location"]
        })
    else:
        waiting_user = ws
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

                # GPS konum güncelleme — partner_location olarak ilet
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
        if waiting_user is ws:
            waiting_user = None
        rid = user_rooms.pop(id(ws), None)
        if rid and rid in rooms:
            room = rooms.pop(rid)
            partner = room[1] if room[0] is ws else room[0]
            user_rooms.pop(id(partner), None)
            if not partner.closed:
                await partner.send_json({"type": "partner_left"})
    return ws


async def blinkchat_keepalive():
    """Render'ın kendi servisini uyutmaması için self-ping."""
    await asyncio.sleep(60)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(f"http://0.0.0.0:{PORT}/ping", timeout=aiohttp.ClientTimeout(total=5))
        except Exception:
            pass
        await asyncio.sleep(600)


async def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ping", ping)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/googlee2b500dcde5fee75.html", lambda r: web.FileResponse(BASE / "static" / "googlee2b500dcde5fee75.html"))
    app.router.add_get("/sitemap.xml", lambda r: web.FileResponse(BASE / "static" / "sitemap.xml"))
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
