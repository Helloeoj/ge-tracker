# ge_tracker_full.py — OSRS GE Tracker (v3.3, with Skill Filter + Sailing)
# Single file: backend + embedded frontend

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, List

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
import uvicorn

# --------------------------- Config ---------------------------
MAPPING_URL = "https://prices.runescape.wiki/api/v1/osrs/mapping"
PRICES_URL = "https://prices.runescape.wiki/api/v1/osrs/latest"
ONEH_URL = "https://prices.runescape.wiki/api/v1/osrs/1h"

FETCH_INTERVAL_SECONDS = 40
DEFAULT_MAX_RESULTS = 30
DEFAULT_MIN_VOLUME = 10

# -------------------------- Skill Tags -----------------------
SKILL_TAGS = {
    "sailing": [
        "log", "oak log", "teak log", "log",
        "plank", "oak plank", "teak plank", "plank",
        "plank sack", "log basket", "wood beam", "ship hull", "ship deck", "ship cabin",
        "bar", "bronze bar", "iron bar", "steel bar", "mithril bar", "adamantite bar", "rune bar",
        "nail", "bronze nail", "iron nail", "steel nail", "mithril nail", "adamant nail", "rune nail", "metal framework",
        "rope", "swamp tar", "tar", "swamp paste", "paste", "sail", "sail cloth", "rigging kit", "fastener", "metal bolt", "bolt",
        "cannonball", "cannon", "ship cannon", "repair kit", "repair tool", "ammo crate", "ammo box",
        "cargo hold", "hold upgrade", "storage crate", "supply crate", "supply sack", "ship mast", "crow’s nest", "crows nest",
        "anchor", "ship wheel", "flax", "canvas", "sea chart", "sea map", "spyglass",
        "crew uniform", "deck planking", "marine paint", "marine timber"
    ],
    "herblore": ["herb", "grimy", "clean", "unfinished", "unf", "vial", "potion", "guam", "marrentill", "tarromin", "harralander", "ranarr", "irit", "avantoe", "kwuarm", "snapdragon", "cadantine", "lantadyme", "dwarf weed", "torstol", "limpwurt", "red spiders'", "white berries", "crushed", "toadflax", "unicorn horn", "snape grass"],
    "fletching": ["log", "arrow shaft", "arrow", "arrowtips", "headless", "shortbow", "longbow", "crossbow", "crossbow string", "bolts", "feather", "dart", "javelin", "ogre arrow", "broad arrow"],
    "cooking": ["raw", "cooked", "karambwan", "lobster", "shark", "anglerfish", "trout", "salmon", "tuna", "monkfish", "swordfish", "cake", "pie", "potato", "chocolate", "shrimp"],
    "smithing": ["bar", "bronze", "iron", "steel", "mithril", "adamant", "rune", "nail", "knife", "dart tip", "arrowtips", "sword", "scimitar", "battleaxe", "mace", "platebody", "platelegs", "full helm"],
    "mining": ["ore", "uncut", "pickaxe", "coal", "iron ore", "copper ore", "tin ore", "adamantite", "rune ore", "pay-dirt"],
    "fishing": ["raw", "fish", "lobster", "shark", "anglerfish", "tuna", "salmon", "trout", "karambwan", "fishing bait", "feather", "harpoon"],
    "woodcutting": ["logs", "oak logs", "willow logs", "maple logs", "yew logs", "magic logs", "axe", "adze"],
    "runecrafting": ["rune essence", "pure essence", "talisman", "tiara", "rune", "binding necklace", "rune pouch"],
    "crafting": ["bowstring", "flax", "leather", "needle", "thread", "gem", "uncut", "cut", "amulet", "necklace", "ring", "bracelet", "glass"],
    "farming": ["seed", "compost", "supercompost", "ultracompost", "watering can", "rake", "spade", "herb", "plant cure", "potato", "tomato", "limpwurt", "white lily"]
}

# --------------------------- Logging --------------------------
log = logging.getLogger("ge_tracker")
log.setLevel(logging.INFO)
fh = RotatingFileHandler("ge_tracker.log", maxBytes=5_000_000, backupCount=3)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(fh)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(ch)

# ------------------------ App & State -------------------------
app = FastAPI()
http_session: Optional[aiohttp.ClientSession] = None
_mapping, _latest, _oneh = {}, {}, {}
clients: Dict[WebSocket, Dict[str, Any]] = {}
lock_mapping, lock_latest, lock_oneh, lock_clients = asyncio.Lock(), asyncio.Lock(), asyncio.Lock(), asyncio.Lock()

# -------------------------- Utils ----------------------------
def ffloat(v):
    try:
        return float(v)
    except Exception:
        return None

def buy_price(info): return ffloat(info.get("low"))
def sell_price(info): return ffloat(info.get("high"))
def profit_gp(b, s): return s - b if b is not None and s is not None else None
def profit_pct(b, s): return ((s - b) / b * 100.0) if b and s and b != 0 else None

# ------------------------ Fetchers ---------------------------
async def fetch_json(url):
    async with http_session.get(url) as resp:
        return await resp.json()

async def fetch_mapping():
    data = await fetch_json(MAPPING_URL)
    return {str(d["id"]): d for d in data if "id" in d}

async def fetch_latest():
    data = await fetch_json(PRICES_URL)
    return data.get("data", {})

async def fetch_oneh():
    data = await fetch_json(ONEH_URL)
    return data.get("data", {})

# ------------------- Filtering / Payload ---------------------
async def build_payload_for_client(filters):
    async with lock_mapping: mapping = dict(_mapping)
    async with lock_latest: latest = dict(_latest)
    async with lock_oneh: oneh = dict(_oneh)

    items = []
    for item_id, price_info in latest.items():
        b, s = buy_price(price_info), sell_price(price_info)
        gp, pc = profit_gp(b, s), profit_pct(b, s)
        if None in (b, s, gp, pc):
            continue

        vol = (ffloat(oneh.get(item_id, {}).get("highPriceVolume")) or 0) + (ffloat(oneh.get(item_id, {}).get("lowPriceVolume")) or 0)
        vol *= 1 if filters.get("volume_mode", "hourly") == "hourly" else 24

        try:
            if filters.get("max_price") and b > float(filters["max_price"]): continue
            if filters.get("min_profit_gp") and gp < float(filters["min_profit_gp"]): continue
            if filters.get("min_profit_pct") and pc < float(filters["min_profit_pct"]): continue
            if filters.get("min_volume") and vol < float(filters["min_volume"]): continue
        except Exception:
            continue

        name = mapping.get(item_id, {}).get("name", item_id)
        if filters.get("skill") and filters["skill"] in SKILL_TAGS:
            if not any(tag.lower() in name.lower() for tag in SKILL_TAGS[filters["skill"]]):
                continue

        items.append({
            "id": int(item_id),
            "name": name,
            "buy": b,
            "sell": s,
            "profit": gp,
            "profit_pct": pc,
            "volume": vol
        })

    key = {"cost": "buy", "profit_pct": "profit_pct"}.get(filters.get("sort"), "profit")
    items.sort(key=lambda x: x.get(key, 0), reverse=(key != "buy"))
    return {"type": "update", "mode": filters.get("volume_mode", "hourly"), "items": items[:int(filters.get("max_results", DEFAULT_MAX_RESULTS))]}

# ------------------- WebSocket & Broadcast -------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    default_filters = {
        "skill": None, "max_price": None, "min_profit_gp": None,
        "min_profit_pct": None, "min_volume": DEFAULT_MIN_VOLUME,
        "sort": "profit", "max_results": DEFAULT_MAX_RESULTS,
        "search": "", "volume_mode": "hourly"
    }
    async with lock_clients:
        clients[websocket] = default_filters
    await websocket.send_text(json.dumps(await build_payload_for_client(default_filters)))

    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            if msg.get("type") == "set_filters":
                async with lock_clients:
                    cur = clients.get(websocket, {}).copy()
                    for key in default_filters:
                        cur[key] = msg.get(key, cur.get(key))
                    clients[websocket] = cur
                await websocket.send_text(json.dumps(await build_payload_for_client(cur)))
            elif msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except (WebSocketDisconnect, Exception):
        async with lock_clients:
            clients.pop(websocket, None)

async def broadcast_all():
    async with lock_clients:
        sockets = list(clients.items())
    for ws, filters in sockets:
        try:
            await ws.send_text(json.dumps(await build_payload_for_client(filters)))
        except Exception:
            async with lock_clients:
                clients.pop(ws, None)

# -------------------- Background refresher -------------------
async def refresher_loop():
    while True:
        try:
            m, l, o = await fetch_mapping(), await fetch_latest(), await fetch_oneh()
            async with lock_mapping:
                _mapping.clear()
                _mapping.update(m)
            async with lock_latest:
                _latest.clear()
                _latest.update(l)
            async with lock_oneh:
                _oneh.clear()
                _oneh.update(o)
            await broadcast_all()
        except Exception as e:
            log.exception("Refresher error: %s", e)
        await asyncio.sleep(FETCH_INTERVAL_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_session
    http_session = aiohttp.ClientSession()
    task = asyncio.create_task(refresher_loop())
    yield
    task.cancel()
    await http_session.close()

app.router.lifespan_context = lifespan

# ------------------------ HTML Frontend -----------------------
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>OSRS GE Tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:sans-serif;background:#0b0b0b;color:#c79a2a;margin:0}
label{font-size:12px;color:#bfb2a0;margin-bottom:6px}
input,select{padding:8px;border-radius:6px;background:#222;color:#c79a2a;border:none}
button{margin-top:8px;padding:8px;border:none;border-radius:6px;background:#333;color:#c79a2a;cursor:pointer}
table{width:100%;margin-top:12px;border-collapse:collapse}
th,td{padding:6px;border-bottom:1px solid #333}
th{color:#c79a2a}
</style>
</head>
<body>
<div style="max-width:1000px;margin:auto;padding:20px">
<h1>OSRS GE Tracker</h1>
<div style="display:flex;flex-wrap:wrap;gap:10px">
<div><label>Max price</label><input id="max_price" type="number"/></div>
<div><label>Min GP</label><input id="min_profit_gp" type="number"/></div>
<div><label>Min %</label><input id="min_profit_pct" type="number"/></div>
<div><label>Min volume</label><input id="min_volume" type="number" value="10"/></div>
<div><label>Volume mode</label><select id="volume_mode"><option value="hourly">Hourly</option><option value="daily">Daily</option></select></div>
<div><label>Sort</label><select id="sort"><option value="profit">Profit (gp)</option><option value="profit_pct">Profit %</option><option value="cost">Cost</option></select></div>
<div><label>Results</label><input id="max_results" type="number" value="30"/></div>
<div><label>Skill</label><select id="skill_filter">
<option value="">All</option>
<option value="sailing">Sailing</option>
<option value="herblore">Herblore</option>
<option value="fletching">Fletching</option>
<option value="cooking">Cooking</option>
<option value="smithing">Smithing</option>
<option value="mining">Mining</option>
<option value="fishing">Fishing</option>
<option value="woodcutting">Woodcutting</option>
<option value="runecrafting">Runecrafting</option>
<option value="crafting">Crafting</option>
<option value="farming">Farming</option>
</select></div>
<button onclick="sendFilters()">Apply</button></div>
<div id="status">Connecting...</div>
<table>
<thead><tr><th>ID</th><th>Name</th><th>Buy</th><th>Sell</th><th>Profit</th><th>%</th><th>Volume</th></tr></thead>
<tbody id="items_body"><tr><td colspan="7">Loading...</td></tr></tbody></table></div>
<script>
const ws = new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/ws");
ws.onopen = () => { document.getElementById("status").textContent = "Connected"; sendFilters(); };
ws.onmessage = evt => {
  const data = JSON.parse(evt.data);
  if (data.type === "update") {
    const body = document.getElementById("items_body");
    body.innerHTML = "";
    for (const it of data.items) {
      const row = `<tr><td>${it.id}</td><td>${it.name}</td><td>${it.buy}</td><td>${it.sell}</td><td>${it.profit}</td><td>${it.profit_pct.toFixed(1)}%</td><td>${Math.round(it.volume)}</td></tr>`;
      body.innerHTML += row;
    }
  }
};
function get(id){ return document.getElementById(id).value || null }
function sendFilters(){
  ws.send(JSON.stringify({
    type:"set_filters",
    max_price:get("max_price"),
    min_profit_gp:get("min_profit_gp"),
    min_profit_pct:get("min_profit_pct"),
    min_volume:get("min_volume"),
    sort:get("sort"),
    max_results:get("max_results"),
    volume_mode:get("volume_mode"),
    skill:get("skill_filter")
  }))
}
setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:"ping"})) }, 30000);
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def serve(request: Request):
    return HTMLResponse(HTML)

# ------------------------- Entrypoint ------------------------
if __name__ == "__main__":
    uvicorn.run("ge_tracker_full:app", host="0.0.0.0", port=8000, reload=False)
