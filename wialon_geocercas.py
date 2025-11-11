# wialon_geocercas.py
# FastAPI para exponer unidades y geocercas de Wialon
# versión ligera para Render: el cruce acepta resource_id y límite

import os
import time
import json
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ------------------------------------------------------------
# .env (local) / variables de entorno (Render)
# ------------------------------------------------------------
load_dotenv()

WIALON_BASE = os.getenv("WIALON_BASE", "https://hst-api.wialon.com/wialon/ajax.html")
WIALON_TOKEN = os.getenv("WIALON_TOKEN", "")

SESSION_SID: Optional[str] = None
SESSION_TS: float = 0

# ------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------
app = FastAPI(title="Wialon Backend", version="1.1.0")

# CORS abierto para que Vercel pueda llamar
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # si quieres, aquí pones tu dominio de vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
# helpers de sesión
# ------------------------------------------------------------
def _login_with_token(token: str) -> str:
    r = requests.get(
        WIALON_BASE,
        params={
            "svc": "token/login",
            "params": json.dumps({"token": token, "fl": 8}),
        },
        timeout=20,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"token/login HTTP {r.status_code}")
    data = r.json()
    sid = data.get("eid") or data.get("sid")
    if not sid:
        raise HTTPException(status_code=400, detail=f"token/login falló: {data}")
    return sid


def _ensure_sid() -> str:
    global SESSION_SID, SESSION_TS
    if not WIALON_TOKEN:
        raise HTTPException(status_code=400, detail="Falta WIALON_TOKEN en entorno")

    # usa cache 4 minutos
    if SESSION_SID and (time.time() - SESSION_TS) < 240:
        return SESSION_SID

    try:
        sid = _login_with_token(WIALON_TOKEN)
        SESSION_SID = sid
        SESSION_TS = time.time()
        return sid
    except HTTPException as e:
        # si el token en realidad ya es un sid
        if "WRONG_PARAMS" in str(e.detail) or "invalid token" in str(e.detail).lower():
            SESSION_SID = WIALON_TOKEN
            SESSION_TS = time.time()
            return SESSION_SID
        raise


def wialon_call(svc: str, params: Dict[str, Any]) -> Any:
    sid = _ensure_sid()
    r = requests.get(
        WIALON_BASE,
        params={"svc": svc, "params": json.dumps(params), "sid": sid},
        timeout=40,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Wialon HTTP {r.status_code}: {r.text}")

    data = r.json()
    # errores de sesión → reintenta una vez
    if isinstance(data, dict) and data.get("error") in (1, 2, 3, 4, 5, 8):
        global SESSION_SID
        SESSION_SID = None
        sid = _ensure_sid()
        r2 = requests.get(
            WIALON_BASE,
            params={"svc": svc, "params": json.dumps(params), "sid": sid},
            timeout=40,
        )
        if not r2.ok:
            raise HTTPException(status_code=502, detail=f"Wialon HTTP {r2.status_code}: {r2.text}")
        return r2.json()
    return data


# ------------------------------------------------------------
# utilidades de geometría
# ------------------------------------------------------------
def _point_in_polygon(lat: float, lon: float, ring: List[Dict[str, float]]) -> bool:
    inside = False
    n = len(ring)
    if n < 3:
        return False
    for i in range(n):
        j = (i - 1) % n
        xi, yi = ring[i]["lon"], ring[i]["lat"]
        xj, yj = ring[j]["lon"], ring[j]["lat"]
        intersect = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersect:
            inside = not inside
    return inside


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat2 - lat1) * 111_000
    dx = (lon2 - lon1) * 111_000
    return (dx * dx + dy * dy) ** 0.5


# ------------------------------------------------------------
# endpoints base
# ------------------------------------------------------------
@app.get("/")
def root():
    return {
        "ok": True,
        "endpoints": [
            "/health",
            "/wialon/units",
            "/wialon/resources",
            "/wialon/resources/{id}/geofences",
            "/wialon/units/in-geofences/local?resource_id=...&max_units=...",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------
# unidades / recursos
# ------------------------------------------------------------
@app.get("/wialon/units", summary="Lista de unidades")
def list_units():
    data = wialon_call(
        "core/search_items",
        {
            "spec": {
                "itemsType": "avl_unit",
                "propName": "sys_name",
                "propValueMask": "*",
                "sortType": "sys_name",
            },
            "force": 1,
            "flags": 1025,
            "from": 0,
            "to": 0,
        },
    )
    out = []
    for it in data.get("items", []):
        pos = it.get("pos") or {}
        out.append(
            {
                "id": it.get("id"),
                "name": it.get("nm"),
                "lat": pos.get("y"),
                "lon": pos.get("x"),
                "t": pos.get("t"),
                "speed": pos.get("s"),
            }
        )
    return {"count": len(out), "units": out}


@app.get("/wialon/resources", summary="Lista de recursos")
def list_resources():
    data = wialon_call(
        "core/search_items",
        {
            "spec": {
                "itemsType": "avl_resource",
                "propName": "sys_name",
                "propValueMask": "*",
                "sortType": "sys_name",
            },
            "force": 1,
            "flags": 1,
            "from": 0,
            "to": 0,
        },
    )
    items = data.get("items", [])
    return {"count": len(items), "resources": [{"id": r["id"], "name": r["nm"]} for r in items]}


# ------------------------------------------------------------
# geocercas por recurso
# ------------------------------------------------------------
@app.get("/wialon/resources/{resource_id}/geofences", summary="Geocercas del recurso")
def geofences_of_resource(resource_id: int):
    raw = wialon_call("resource/get_zone_data", {"itemId": resource_id, "flags": 0x1F})
    iterable = raw.values() if isinstance(raw, dict) else (raw or [])
    zones = []
    for z in iterable:
        name = z.get("n") or z.get("name") or ""
        jp = z.get("jp") or {}
        item: Dict[str, Any] = {
            "id": z.get("id") or z.get("i"),
            "name": name,
            "type": z.get("t"),
            "color_argb": z.get("c") or jp.get("color_argb"),
            "points": None,
            "center": None,
            "radius": None,
        }

        # polígono
        if jp.get("points"):
            item["points"] = [
                {"lat": float(p["lat"]), "lon": float(p["lon"])} for p in jp["points"]
            ]
        elif z.get("p"):
            pts = []
            for p in z["p"]:
                if isinstance(p, dict):
                    pts.append({"lat": float(p["y"]), "lon": float(p["x"])})
                else:
                    pts.append({"lat": float(p[1]), "lon": float(p[0])})
            item["points"] = pts

        # círculo
        if jp.get("center") and jp.get("radius"):
            c = jp["center"]
            item["center"] = {"lat": float(c["lat"]), "lon": float(c["lon"])}
            item["radius"] = float(jp["radius"])
        elif z.get("ct") and z.get("r"):
            c = z["ct"]
            item["center"] = {"lat": float(c["y"]), "lon": float(c["x"])}
            item["radius"] = float(z["r"])

        zones.append(item)

    return {"resource_id": resource_id, "count": len(zones), "geofences": zones}


# ------------------------------------------------------------
# cruce local (ligero)
# ------------------------------------------------------------
@app.get(
    "/wialon/units/in-geofences/local",
    summary="Cruce local limitado (para Render)",
)
def cross_units_local(
    resource_id: Optional[int] = Query(None, description="ID de recurso wialon (recomendado)"),
    max_units: int = Query(200, description="máximo de unidades a considerar"),
):
    # 1) unidades
    units_resp = list_units()
    units = units_resp["units"][:max_units]

    # 2) recursos
    if resource_id is not None:
      resources = [{"id": resource_id, "name": ""}]
    else:
      resources = list_resources()["resources"]

    result: Dict[str, Dict[str, List[int]]] = {}

    for r in resources:
        rid = r["id"]
        geos = geofences_of_resource(rid)["geofences"]
        result[str(rid)] = {}

        for u in units:
            lat = u.get("lat")
            lon = u.get("lon")
            if lat is None or lon is None:
                continue

            hits: List[int] = []
            for g in geos:
                if g.get("points"):
                    if _point_in_polygon(lat, lon, g["points"]):
                        hits.append(int(g["id"]))
                        continue
                if g.get("center") and g.get("radius"):
                    c = g["center"]
                    d = _dist_m(lat, lon, c["lat"], c["lon"])
                    if d <= float(g["radius"]):
                        hits.append(int(g["id"]))
                        continue

            if hits:
                result[str(rid)][str(u["id"])] = hits

    return {"ok": True, "result": result}
