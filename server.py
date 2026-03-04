import asyncio, json, uuid, os, pathlib
from aiohttp import web, WSMsgType
import requests
from requests.auth import HTTPBasicAuth

PORT        = int(os.getenv("PORT", 8080))
BASE        = pathlib.Path(__file__).parent
TWILIO_SID  = os.getenv("TWILIO_SID")
TWILIO_TOKEN= os.getenv("TWILIO_TOKEN")

waiting_user = None
rooms = {}
user_rooms = {}
online = 0

def get_ice_servers():
    """Twilio Network Traversal Service'ten TURN credentials al."""
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Tokens.json",
            auth=HTTPBasicAuth(TWILIO_SID, TWILIO_TOKEN),
            timeout=5
        )
        data = r.json()
        return data.get("ice_servers", [])
    except Exception as e:
        print(f"Twilio TURN hatasi: {e}")
        return [
            {"url": "stun:stun.l.google.com:19302"},
            {"url": "stun:stun1.l.google.com:19302"},
        ]

async def index(request):
    return web.FileResponse(BASE / "static" / "index.html")

async def ice_handler(request):
    servers = await asyncio.to_thread(get_ice_servers)
    return web.json_response({"ice_servers": servers})

async def ws_handler(request):
    global waiting_user, online
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    online += 1

    if waiting_user and not waiting_user.closed:
        partner = waiting_user
        waiting_user = None
        rid = str(uuid.uuid4())
        rooms[rid] = [ws, partner]
        user_rooms[id(ws)] = rid
        user_rooms[id(partner)] = rid
        await ws.send_json({"type": "matched", "role": "answerer", "online": online})
        await partner.send_json({"type": "matched", "role": "offerer", "online": online})
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
                if not partner.closed:
                    await partner.send_str(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        online = max(0, online - 1)
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

async def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ice", ice_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", BASE / "static")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Blinkchat running on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
