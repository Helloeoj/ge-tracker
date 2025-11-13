# ge_tracker_full.py — OSRS GE Tracker (v4)
# Full backend + embedded HTML with live search, skill filter, graph view, and item descriptions

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Path
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import time
import re

# ==========================
# API URLs
# ==========================
MAPPING_URL = "https://prices.runescape.wiki/api/v1/osrs/mapping"
PRICES_URL = "https://prices.runescape.wiki/api/v1/osrs/latest"
ONEH_URL = "https://prices.runescape.wiki/api/v1/osrs/1h"

FETCH_INTERVAL_SECONDS = 40
DEFAULT_MAX_RESULTS = 30
DEFAULT_MIN_VOLUME = 10

# ==========================
# Skill Tags
# ==========================
SKILL_TAGS = {
    "sailing": [
        "plank", "oak plank", "teak plank", "mahogany plank",
        "nail", "bronze nail", "iron nail", "steel nail", "mithril nail",
        "adamant nail", "rune nail", "rope", "tar", "swamp paste", "sail",
        "canvas", "rigging", "anchor", "spyglass", "chart", "map", "ship",
        "deck", "hold", "repair", "bar", "cargo", "log",
    ],
    "construction": [
        "plank", "oak plank", "teak plank", "mahogany plank",
        "nail", "steel nail", "soft clay", "limestone brick",
        "marble block", "gold leaf", "bolt of cloth", "bagged"
    ],
    "herblore": [
        "vial of water", "potion", "herb", "grimy herb", "clean herb",
        "eye of newt", "unicorn horn dust", "red spiders' eggs",
        "snape grass", "white berries", "toadflax", "blue dragon scale",
        "crushed nest", "chocolate dust", "ivy berries", "goat horn dust"
    ],
    "fletching": [
        "arrow shaft", "headless arrow", "unfinished broad arrow",
        "shortbow (u)", "longbow (u)", "crossbow (u)",
        "bolt (u)", "javelin tip", "dart tip"
    ],
    "cooking": ["raw", "dough", "grape", "jug"],
    "smithing": [
        "ore", "bar", "bronze bar", "iron bar", "steel bar",
        "mithril bar", "adamant bar", "rune bar"
    ],
    "mining": [
        "copper ore", "tin ore", "iron ore", "coal", "mithril ore",
        "adamantite ore", "ore", "pay‑dirt"
    ],
    "fishing": ["fishing bait", "feather", "fishing gear", "harpoon", "fishing rod"],
    "woodcutting": [
        "logs", "oak logs", "willow logs", "maple logs",
        "yew logs", "magic logs", "axe", "adze", "anima bark"
    ],
    "runecrafting": [
        "essence", "rune essence", "pure essence", "talisman",
        "tiara", "rune pouch"
    ],
    "crafting": [
        "leather", "hard leather", "thread", "needle",
        "gem", "uncut gem", "cut gem", "amulet mould",
        "necklace mould", "ring mould", "gold bar", "silver bar"
    ],
    "farming": [
        "seed", "compost", "ultracompost", "watering can",
        "spade", "potato seed", "tomato seed", "limpwurt root"
    ]
}


# ==========================
# Logging Setup
# ==========================
log = logging.getLogger("ge_tracker")
log.setLevel(logging.INFO)
fh = RotatingFileHandler("ge_tracker.log", maxBytes=5_000_000, backupCount=3)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(fh)
log.addHandler(logging.StreamHandler())


# ==========================
# FastAPI App and Globals
# ==========================
app = FastAPI()
http_session: Optional[aiohttp.ClientSession] = None
_mapping, _latest, _oneh = {}, {}, {}
clients: Dict[WebSocket, Dict[str, Any]] = {}

templates = Jinja2Templates(directory="templates")
history: Dict[str, list] = {}  # item price history


# ==========================
# Helpers
# ==========================
async def fetch_json(url):
    async with http_session.get(url) as resp:
        return await resp.json()

async def fetch_mapping():
    data = await fetch_json(MAPPING_URL)
    return {str(d["id"]): d for d in data if "id" in d}

async def fetch_latest():
    return (await fetch_json(PRICES_URL)).get("data", {})

async def fetch_oneh():
    return (await fetch_json(ONEH_URL)).get("data", {})

def ffloat(v):
    try:
        return float(v)
    except:
        return None

def buy_price(info): return ffloat(info.get("low"))
def sell_price(info): return ffloat(info.get("high"))
def profit_gp(b, s): return s - b if b and s else None
def profit_pct(b, s): return ((s - b) / b * 100.0) if b and s and b != 0 else None


# ==========================
# Build Websocket Payload
# ==========================
async def build_payload(filters):
    mapping, latest, oneh = _mapping.copy(), _latest.copy(), _oneh.copy()
    results = []

    for item_id, price_info in latest.items():
        b, s = buy_price(price_info), sell_price(price_info)
        gp, pc = profit_gp(b, s), profit_pct(b, s)
        if None in (b, s, gp, pc): 
            continue

        vol = (
            (ffloat(oneh.get(item_id, {}).get("highPriceVolume")) or 0) +
            (ffloat(oneh.get(item_id, {}).get("lowPriceVolume")) or 0)
        )

        if filters.get("volume_mode") == "daily":
            vol *= 24

        try:
            if filters.get("max_price") and b > float(filters["max_price"]): continue
            if filters.get("min_profit_gp") and gp < float(filters["min_profit_gp"]): continue
            if filters.get("min_profit_pct") and pc < float(filters["min_profit_pct"]): continue
            if filters.get("min_volume") and vol < float(filters["min_volume"]): continue
        except:
            continue

        name = mapping.get(item_id, {}).get("name", item_id)

        # Skill tags
        if filters.get("skill") and filters["skill"] in SKILL_TAGS:
            tags = SKILL_TAGS.get(filters["skill"])
            if not any(tag.lower() in name.lower() for tag in tags):
                continue

        if filters.get("search") and filters["search"].lower() not in name.lower():
            continue

        results.append({
            "id": int(item_id),
            "name": name,
            "buy": b,
            "sell": s,
            "profit": gp,
            "profit_pct": pc,
            "volume": vol
        })

    key = {"cost": "buy", "profit_pct": "profit_pct"}.get(filters.get("sort"), "profit")
    results.sort(key=lambda x: x.get(key), reverse=(key != "buy"))

    return {
        "type": "update",
        "mode": filters.get("volume_mode", "hourly"),
        "items": results[:int(filters.get("max_results", DEFAULT_MAX_RESULTS))]
    }


# ==========================
# WebSocket Endpoint
# ==========================
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    default_filters = {
        "skill": None, "max_price": None, "min_profit_gp": None,
        "min_profit_pct": None, "min_volume": DEFAULT_MIN_VOLUME,
        "sort": "profit", "max_results": DEFAULT_MAX_RESULTS,
        "search": "", "volume_mode": "hourly"
    }

    clients[ws] = default_filters
    await ws.send_text(json.dumps(await build_payload(default_filters)))

    try:
        while True:
            msg = json.loads(await ws.receive_text())

            if msg.get("type") == "set_filters":
                filters = {**clients.get(ws, {}), **{k: msg.get(k) for k in default_filters}}
                clients[ws] = filters
                await ws.send_text(json.dumps(await build_payload(filters)))

            elif msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        clients.pop(ws, None)


# ==========================
# SINGLE Correct Refresher Loop
# ==========================
async def refresher_loop():
    while True:
        try:
            _mapping.update(await fetch_mapping())
            latest_new = await fetch_latest()
            _latest.update(latest_new)
            _oneh.update(await fetch_oneh())

            now = time.time()
            for item_id, info in latest_new.items():
                b = buy_price(info)
                s = sell_price(info)
                if b is None or s is None:
                    continue

                history.setdefault(item_id, []).append({
                    "ts": now,
                    "buy": b,
                    "sell": s
                })

            # Push updated data to connected clients
            for ws in list(clients):
                await ws.send_text(json.dumps(await build_payload(clients[ws])))

        except Exception as e:
            log.exception("Refresher error: %s", e)

        await asyncio.sleep(FETCH_INTERVAL_SECONDS)


# ==========================
# Description Fetcher
# ==========================
async def fetch_item_description(item_id: int) -> str:
    name = _mapping.get(str(item_id), {}).get("name")
    if not name:
        return "Description not available."

    wiki_title = name.replace(" ", "_")
    url = "https://oldschool.runescape.wiki/w/api.php"
    params = {
        "action": "parse",
        "page": wiki_title,
        "prop": "text",
        "format": "json",
        "section": 0
    }

    try:
        async with http_session.get(url, params=params) as resp:
            data = await resp.json()

        html = data.get("parse", {}).get("text", {}).get("*", "")
        match = re.search(r"Examine:\s*</span>(.*?)<", html)
        if match:
            return match.group(1).strip()

    except Exception as e:
        log.warning(f"Failed to fetch description for {name}: {e}")

    return "Description not available."


# ==========================
# Item Detail Page Route
# ==========================
@app.get("/item/{item_id}", response_class=HTMLResponse)
async def item_page(request: Request, item_id: int = Path(...)):
    name = _mapping.get(str(item_id), {}).get("name", f"Item {item_id}")
    description = await fetch_item_description(item_id)
    return templates.TemplateResponse("item.html", {
        "request": request,
        "item_id": item_id,
        "name": name,
        "description": description
    })


# ==========================
# History Data API
# ==========================
@app.get("/api/item/{item_id}/history")
async def item_history(item_id: int, timeframe: str = "7d"):
    now = time.time()
    cutoff = {
        "1h": now - 3600,
        "24h": now - 86400,
        "7d": now - 7 * 86400,
        "30d": now - 30 * 86400,
        "1y": now - 365 * 86400,
        "all": 0
    }.get(timeframe, 0)

    raw = history.get(str(item_id), [])
    filtered = [d for d in raw if d["ts"] >= cutoff]

    return {"item_id": item_id, "timeframe": timeframe, "data": filtered}


# ==========================
# Lifespan Management
# ==========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_session
    http_session = aiohttp.ClientSession()
    task = asyncio.create_task(refresher_loop())

    yield

    task.cancel()
    await http_session.close()

app.router.lifespan_context = lifespan


# ==========================
# HTML Frontend (Main Page)
# ==========================
HTML = r"""<!DOCTYPE html><html><head><meta charset=utf-8><title>OSRS GE Tracker</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
body { background:#0b0b0b; color:#f0d070; font-family:sans-serif; margin:0; padding:0 }
.container { max-width:1000px; margin:40px auto; background:#2a1a0f; padding:20px; border-radius:12px }
label { display:block; font-size:12px; margin-bottom:4px; color:#ffcc66 }
input, select { width:100%; padding:6px; margin-bottom:10px; background:#1a1a1a; color:#f9d57e; border:none; border-radius:6px }
button { padding:10px; background:#503820; color:#f9d57e; border:none; border-radius:6px; cursor:pointer; margin-top:10px }
table { width:100%; border-collapse:collapse; margin-top:20px }
th, td { padding:8px; border-bottom:1px solid #333; text-align:left }
th { background:#1e1208; color:#f0c060 }
.flex-row { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:10px }
.flex-col { flex:1; min-width:120px }
</style></head><body><div class=container><h1>OSRS GE Tracker</h1><div class=flex-row>
<div class=flex-col><label>Max price</label><input id=max_price type=number></div>
<div class=flex-col><label>Min GP</label><input id=min_profit_gp type=number></div>
<div class=flex-col><label>Min %</label><input id=min_profit_pct type=number></div>
<div class=flex-col><label>Min volume</label><input id=min_volume type=number value=10></div>
<div class=flex-col><label>Volume mode</label><select id=volume_mode><option value=hourly>Hourly</option><option value=daily>Daily</option></select></div>
<div class=flex-col><label>Sort</label><select id=sort><option value=profit>Profit (gp)</option><option value=profit_pct>Profit %</option><option value=cost>Cost</option></select></div>
<div class=flex-col><label>Results</label><input id=max_results type=number value=30></div>
<div class=flex-col><label>Skill</label><select id=skill_filter><option value="">All</option><option value=sailing>Sailing</option><option value=herblore>Herblore</option><option value=fletching>Fletching</option><option value=cooking>Cooking</option><option value=smithing>Smithing</option><option value=mining>Mining</option><option value=fishing>Fishing</option><option value=woodcutting>Woodcutting</option><option value=runecrafting>Runecrafting</option><option value=crafting>Crafting</option><option value=farming>Farming</option></select></div>
<div class=flex-col><label>Live search</label><input id=search placeholder="Type item name..."></div>
</div><button onclick=sendFilters()>Apply</button><div id=status style="margin-top:10px">Connecting...</div>
<table><thead><tr><th>ID</th><th>Name</th><th>Buy</th><th>Sell</th><th>Profit</th><th>%</th><th>Volume</th></tr></thead>
<tbody id=items_body><tr><td colspan=7>Loading...</td></tr></tbody></table></div>
<script>
const ws = new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/ws");
ws.onopen = () => { document.getElementById("status").textContent = "Connected"; sendFilters(); };
ws.onmessage = evt => {
  const data = JSON.parse(evt.data);
  if (data.type === "update") {
    const rows = data.items.filter(it => it.name.toLowerCase().includes(
  (document.getElementById("search").value || "").toLowerCase()
)).map(it =>
  `<tr><td>${it.id}</td><td><a href="/item/${it.id}" style="color:#f0d070;text-decoration:none">${it.name}</a></td><td>${it.buy}</td><td>${it.sell}</td><td>${it.profit}</td><td>${it.profit_pct.toFixed(1)}%</td><td>${Math.round(it.volume)}</td></tr>`
).join("\n");

    document.getElementById("items_body").innerHTML =
      rows || "<tr><td colspan=7>No results</td></tr>";
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
    skill:get("skill_filter"),
    search:get("search")
  }));
}
document.getElementById("search").addEventListener("input", () => sendFilters());
setInterval(() => {
  if (ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({type:"ping"}))
}, 30000);
</script></body></html>"""

# ==========================
# Root Route
# ==========================
@app.get("/", response_class=HTMLResponse)
async def serve_home(request: Request):
    return HTMLResponse(HTML)

# ==========================
# Server Startup
# ==========================
if __name__ == "__main__":
    uvicorn.run("ge_tracker_full:app", host="0.0.0.0", port=8000, reload=False)
