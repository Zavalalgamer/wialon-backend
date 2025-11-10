# wialon_geocercas.py
# FastAPI para exponer unidades y geocercas de Wialon
# listo para Render + endpoint /wialon/snapshot

import os
import time
import json
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ------------------------------------------------------------
# Cargar .env (local); en Render usa las env del dashboard
# ------------------------------------------------------------
load_dotenv()

WIALON_BASE = os.getenv("WIALON_BASE", "https://hst-api.wialon.com/wialon/ajax.html")
WIALON_TOKEN = os.getenv("WIALON_TOKEN", "")

# cache de sesión
SESSION_SID: Optional[str] = None
SESSION_TS: float = 0

# ------------------------------------------------------------
# App
# ------------------------------------------------------------
app = FastAPI(title="Wialon Backend", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # si quieres más estricto, cámbialo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------
# Helpers de autenticación
# ------------------------------------------------------------
def _login_with_token(token: str) -> str:
    """Intenta loguearse con token/login."""
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
    """
    Devuelve un SID válido.
    1) usa cache
    2) intenta token/login
    3) si eso falla por WRONG_PARAMS, asumimos que WIALON_TOKEN ya es un SID
    """
    global SESSION_SID, SESSION_TS

    if not WIALON_TOKEN:
        raise HTTPException(status_code=400, detail="Falta WIALON_TOKEN en entorno")

    # usa cache 4 min
    if SESSION_SID and (time.time() - SESSION_TS) < 240:
        return SESSION_SID

    # reintenta login
    try:
        sid = _login_with_token(WIALON_TOKEN)
        SESSION_SID = sid
        SESSION_TS = time.time()
        return sid
    except HTTPException as e:
        # si el token no era de API sino un SID directo:
        if "WRONG_PARAMS" in str(e.detail) or "invalid token" in str(e.detail).lower():
            SESSION_SID = WIALON_TOKEN
            SESSION_TS = time.time()
            return SESSION_SID
        raise


def wialon_call(svc: str, params: Dict[str, Any]) -> Any:
    """
    Llama a Wialon con un SID válido. Si expira, reintenta una vez.
    """
    sid = _ensure_sid()
    r = requests.get(
        WIALON_BASE,
        params={"svc": svc, "params": json.dumps(params), "sid": sid},
        timeout=40,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"Wialon HTTP {r.status_code}: {r.text}")

    data = r.json()
    # errores típicos de sesión
    if isinstance(data, dict) and data.get("error") in (1, 2, 3, 4, 5, 8):
        # limpiar cache y reintentar
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
# Utils geométricos
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
    # aproximación rápida (no haversine exacta pero suficiente)
    dy = (lat2 - lat1) * 111_000
    dx = (lon2 - lon1) * 111_000
    return (dx * dx + dy * dy) ** 0.5


# ------------------------------------------------------------
# Endpoints básicos
# ------------------------------------------------------------
@app.get("/", summary="Endpoints disponibles")
def root():
    return {
        "ok": True,
        "endpoints": [
            "/health",
            "/debug/routes",
            "/wialon/units",
            "/wialon/resources",
            "/wialon/resources/{resource_id}/geofences",
            "/wialon/units/in-geofences/local",
            "/wialon/snapshot?resource_id=18891825",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/routes")
def debug_routes():
    return {"routes": [r.path for r in app.routes]}


# ------------------------------------------------------------
# /wialon/units
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
            "flags": 1025,  # incluye pos
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


# ------------------------------------------------------------
# /wialon/resources
# ------------------------------------------------------------
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
    return {
        "count": len(items),
        "resources": [{"id": r["id"], "name": r["nm"]} for r in items],
    }


# ------------------------------------------------------------
# /wialon/resources/{id}/geofences
# interpreta el formato nativo de Wialon
# ------------------------------------------------------------
@app.get(
    "/wialon/resources/{resource_id}/geofences",
    summary="Geocercas del recurso (polígonos y círculos)",
)
def geofences_of_resource(resource_id: int):
    # resource/get_zone_data con flags "grandes"
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

        # 1) polígono
        if jp.get("points"):
            # ya viene como [{lat,lon},...]
            item["points"] = [
                {"lat": float(p["lat"]), "lon": float(p["lon"])} for p in jp["points"]
            ]
        elif z.get("p"):
            norm = []
            for p in z["p"]:
                # p puede venir como dict {x,y}
                if isinstance(p, dict):
                    norm.append({"lat": float(p["y"]), "lon": float(p["x"])})
                else:
                    # o como [x,y]
                    norm.append({"lat": float(p[1]), "lon": float(p[0])})
            item["points"] = norm

        # 2) círculo
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
# /wialon/units/in-geofences/local
# cruza todas las unidades con todas las geocercas que haya en todos los recursos
# ------------------------------------------------------------
@app.get(
    "/wialon/units/in-geofences/local",
    summary="Cruce local: qué unidades están dentro de qué geocercas",
)
def cross_units_local():
    # 1) unidades
    units_resp = list_units()
    units = units_resp["units"]

    # 2) recursos
    resources_resp = list_resources()
    resources = resources_resp["resources"]

    # 3) traer geocercas de cada recurso
    resource_geos: Dict[int, List[Dict[str, Any]]] = {}
    for r in resources:
        rid = r["id"]
        geos = geofences_of_resource(rid)["geofences"]
        resource_geos[rid] = geos

    # 4) por cada unidad, probar contra todas las geocercas del recurso correspondiente
    result: Dict[str, Dict[str, List[int]]] = {}  # {resource_id: {unit_id: [zone_id,...]}}

    for r in resources:
        rid = r["id"]
        geos = resource_geos.get(rid, [])
        result[str(rid)] = {}
        for u in units:
            lat = u.get("lat")
            lon = u.get("lon")
            if lat is None or lon is None:
                continue

            hits: List[int] = []
            for g in geos:
                # polígono
                if g.get("points"):
                    if _point_in_polygon(lat, lon, g["points"]):
                        hits.append(int(g["id"]))
                        continue
                # círculo
                if g.get("center") and g.get("radius"):
                    c = g["center"]
                    d = _dist_m(lat, lon, c["lat"], c["lon"])
                    if d <= float(g["radius"]):
                        hits.append(int(g["id"]))
                        continue

            if hits:
                result[str(rid)][str(u["id"])] = hits

    return {"ok": True, "result": result}


# ------------------------------------------------------------
# /wialon/snapshot
# pensado para el frontend rápido: trae todo de una
# ------------------------------------------------------------
@app.get("/wialon/snapshot", summary="Unidades + recursos + geocercas de un recurso")
def snapshot(resource_id: int):
    """
    Devuelve:
      - units: todas las unidades (con lat/lon)
      - resources: todos los recursos
      - geofences_by_resource: solo del recurso pedido (interpretadas)
    Así el frontend ya no tiene que pegarle 3 veces.
    """
    units = list_units()["units"]
    resources = list_resources()["resources"]
    geos = geofences_of_resource(resource_id)["geofences"]
    return {
        "ok": True,
        "units": units,
        "resources": resources,
        "geofences_by_resource": {str(resource_id): geos},
    }
