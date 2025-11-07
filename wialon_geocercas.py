# wialon_geocercas.py
# FastAPI + Wialon backend optimizado para Render

import os, time, json, requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

WIALON_BASE = os.getenv("WIALON_BASE", "https://hst-api.wialon.com/wialon/ajax.html")
WIALON_TOKEN = os.getenv("WIALON_TOKEN", "")

SESSION_SID: Optional[str] = None
SESSION_TS: float = 0

app = FastAPI(title="Wialon API Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Wialon:
    @staticmethod
    def _get_sid() -> str:
        global SESSION_SID, SESSION_TS
        if SESSION_SID and time.time() - SESSION_TS < 240:
            return SESSION_SID
        if not WIALON_TOKEN:
            raise HTTPException(500, "Falta WIALON_TOKEN en entorno (.env)")
        r = requests.get(WIALON_BASE, params={
            "svc": "token/login",
            "params": json.dumps({"token": WIALON_TOKEN})
        }, timeout=15)
        data = r.json()
        if "eid" not in data:
            raise HTTPException(502, f"token/login falló: {data}")
        SESSION_SID = data["eid"]
        SESSION_TS = time.time()
        return SESSION_SID

    @staticmethod
    def call(svc: str, params: dict) -> dict:
        sid = Wialon._get_sid()
        r = requests.get(WIALON_BASE, params={
            "svc": svc,
            "params": json.dumps(params),
            "sid": sid
        }, timeout=30)
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise HTTPException(502, f"Error {data['error']} en {svc}")
        return data

@app.get("/")
def root():
    return {"ok": True, "endpoints": ["/wialon/units", "/wialon/resources", "/wialon/resources/{id}/geofences"]}

@app.get("/wialon/units")
def list_units():
    params = {
        "spec": {"itemsType": "avl_unit", "propName": "sys_name", "propValueMask": "*", "sortType": "sys_name"},
        "force": 1, "flags": 1025, "from": 0, "to": 0
    }
    data = Wialon.call("core/search_items", params)
    units = []
    for u in data.get("items", []):
        pos = u.get("pos") or {}
        units.append({
            "id": u.get("id"),
            "name": u.get("nm"),
            "lat": pos.get("y"),
            "lon": pos.get("x"),
            "t": pos.get("t"),
            "speed": pos.get("s")
        })
    return {"count": len(units), "units": units}

@app.get("/wialon/resources")
def list_resources():
    params = {
        "spec": {"itemsType": "avl_resource", "propName": "sys_name", "propValueMask": "*", "sortType": "sys_name"},
        "force": 1, "flags": 1, "from": 0, "to": 0
    }
    data = Wialon.call("core/search_items", params)
    items = data.get("items", [])
    return {"count": len(items), "resources": [{"id": r.get("id"), "name": r.get("nm")} for r in items]}

@app.get("/wialon/resources/{resource_id}/geofences")
def geofences_of_resource(resource_id: int):
    raw = Wialon.call("resource/get_zone_data", {"itemId": resource_id, "flags": 0x1F})
    iterable = (raw.values() if isinstance(raw, dict) else raw) or []
    zones = []
    for z in iterable:
        jp = z.get("jp") or {}
        name = z.get("n") or z.get("name") or ""
        tipo = z.get("t")
        item = {
            "id": z.get("id") or z.get("i"),
            "name": name,
            "type": tipo,
            "color_argb": z.get("c") or jp.get("color_argb"),
        }
        zones.append(item)
    return {"resource_id": resource_id, "count": len(zones), "geofences": zones}
