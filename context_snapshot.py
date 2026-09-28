"""
context_snapshot.py — La "foto de contexto" que Vesper arma antes de cada respuesta.

Cuatro bloques, baratos y siempre presentes:
  1. TEMPORAL   → fecha, hora local, día, laboral / fin de semana, oficina / home office
  2. ESPACIAL   → dónde está el señor (GPS del navegador → dirección con OpenStreetMap)
                  y si va en movimiento (velocidad y rumbo)
  3. PERSONAL   → hace cuánto habló por última vez, próximos recordatorios y alarmas
  4. AMBIENTAL  → qué suena en Spotify y el clima donde está

Todo lo que sale a internet (dirección, clima, Spotify) va con caché y timeout corto,
y se consulta en paralelo, para no alentar la respuesta.
"""

import os, math, time, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

USER_TZ      = ZoneInfo(os.environ.get("VESPER_TZ", "America/Monterrey"))
# Días de oficina presencial (0 = lunes ... 6 = domingo). Por defecto lunes y martes.
OFFICE_DAYS  = {int(d) for d in os.environ.get("VESPER_OFFICE_DAYS", "0,1").split(",") if d.strip().isdigit()}
OFFICE_HOURS = os.environ.get("VESPER_OFFICE_HOURS", "8:00 a 15:00")
MOVING_KMH   = 8          # arriba de esto se considera "en movimiento"

DIAS  = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
RUMBOS = ["norte", "noreste", "este", "sureste", "sur", "suroeste", "oeste", "noroeste"]

_pool = ThreadPoolExecutor(max_workers=4)
_lock = threading.Lock()
_cache = {}   # key → (expires_ts, value)


def now_local():
    return datetime.now(USER_TZ)


def _cached(key, ttl, fn):
    t = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > t:
            return hit[1]
    value = fn()
    with _lock:
        if len(_cache) > 300:
            _cache.clear()
        _cache[key] = (t + ttl, value)
    return value


# ── 1. Temporal ──────────────────────────────────────────────────────────────
def temporal_block(now=None):
    now = now or now_local()
    wd = now.weekday()
    if wd >= 5:
        jornada = "fin de semana"
    elif wd in OFFICE_DAYS:
        jornada = f"día laboral PRESENCIAL en oficina CEMEX ({OFFICE_HOURS})"
    else:
        jornada = "día laboral en HOME OFFICE"
    h = now.hour
    momento = ("madrugada" if h < 6 else "mañana" if h < 12 else
               "tarde" if h < 19 else "noche")
    return (f"- {DIAS[wd].capitalize()} {now.day} de {MESES[now.month-1]} de {now.year}, "
            f"{now.strftime('%H:%M')} hora local ({momento}).\n"
            f"- Jornada: {jornada}.")


# ── 2. Espacial ──────────────────────────────────────────────────────────────
def reverse_geocode(lat, lon):
    """Dirección legible con Nominatim (OpenStreetMap). Gratis, sin llave; caché ~100 m."""
    key = ("geo", round(lat, 3), round(lon, 3))

    def fetch():
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 17,
                        "addressdetails": 1, "accept-language": "es"},
                headers={"User-Agent": "Vesper/2.1 (asistente personal)"},
                timeout=4)
            if r.status_code != 200:
                return None
            a = r.json().get("address", {})
            parts = [a.get("road"), a.get("suburb") or a.get("neighbourhood") or a.get("quarter"),
                     a.get("city") or a.get("town") or a.get("village") or a.get("municipality"),
                     a.get("state")]
            return ", ".join(p for p in parts if p) or None
        except Exception as e:
            print(f"[CONTEXT] Nominatim: {e}")
            return None

    return _cached(key, 24 * 3600, fetch)


def _rumbo(heading):
    if heading is None:
        return None
    try:
        return RUMBOS[int(((float(heading) % 360) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return None


def spatial_block(geo, fallback_location):
    if geo and geo.get("lat") is not None and geo.get("lon") is not None:
        lat, lon = float(geo["lat"]), float(geo["lon"])
        place = reverse_geocode(lat, lon)
        acc = geo.get("accuracy")
        acc_txt = f" (precisión ~{int(acc)} m)" if acc else ""
        line = f"- Ubicación actual (GPS): {place or 'sin dirección'} [{lat:.5f}, {lon:.5f}]{acc_txt}."
        speed = geo.get("speed_kmh")
        if speed is not None and speed >= MOVING_KMH:
            rumbo = _rumbo(geo.get("heading"))
            line += f"\n- EN MOVIMIENTO a ~{round(speed)} km/h" + (f" rumbo al {rumbo}." if rumbo else ".")
        else:
            line += "\n- Parece estar detenido o caminando."
        return line
    loc = fallback_location or {}
    return (f"- Ubicación aproximada (por IP, sin GPS): {loc.get('city','?')}, "
            f"{loc.get('region','')} {loc.get('country','')}.")


# ── 3. Personal inmediato ────────────────────────────────────────────────────
def _ago(delta):
    mins = int(delta.total_seconds() // 60)
    if mins < 2:   return "hace un momento"
    if mins < 60:  return f"hace {mins} min"
    hours = mins // 60
    if hours < 24: return f"hace {hours} h"
    return f"hace {hours // 24} días"


def personal_block(db_q, user_type):
    lines = []
    try:
        row = db_q("""SELECT created_at FROM context_history
                      WHERE user_type=%s AND role='user'
                      ORDER BY created_at DESC LIMIT 1""", (user_type,), fetch="one")
        if row and row.get("created_at"):
            last = row["created_at"]
            if last.tzinfo is None:
                last = last.replace(tzinfo=USER_TZ)
            lines.append(f"- Último mensaje del señor antes de este: {_ago(now_local() - last)}.")
        else:
            lines.append("- Es la primera conversación registrada.")
    except Exception as e:
        print(f"[CONTEXT] historial: {e}")
    if user_type != "owner":
        return "\n".join(lines)
    try:
        rems = db_q("""SELECT label, trigger_at FROM reminders WHERE status='pending'
                       ORDER BY trigger_at LIMIT 3""", fetch="all") or []
        if rems:
            lines.append("- Próximos recordatorios: " +
                         "; ".join(f"{r['label']} ({r['trigger_at']})" for r in rems) + ".")
        else:
            lines.append("- Sin recordatorios pendientes.")
    except Exception as e:
        print(f"[CONTEXT] recordatorios: {e}")
    try:
        alarms = db_q("SELECT time, label FROM alarms WHERE active=TRUE ORDER BY time LIMIT 3",
                      fetch="all") or []
        if alarms:
            lines.append("- Alarmas activas: " +
                         "; ".join(f"{a['time']} {a['label'] or ''}".strip() for a in alarms) + ".")
    except Exception as e:
        print(f"[CONTEXT] alarmas: {e}")
    return "\n".join(lines)


# ── 4. Ambiental ─────────────────────────────────────────────────────────────
def now_playing(spotify_headers):
    def fetch():
        h = spotify_headers()
        if not h:
            return "Spotify no conectado"
        try:
            r = requests.get("https://api.spotify.com/v1/me/player/currently-playing",
                             headers=h, timeout=3)
            if r.status_code == 204 or not r.content:
                return "nada sonando"
            d = r.json()
            item = d.get("item") or {}
            if not item:
                return "nada sonando"
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            state = "sonando" if d.get("is_playing") else "en pausa"
            return f"{item.get('name','?')} — {artists} ({state})"
        except Exception as e:
            print(f"[CONTEXT] Spotify: {e}")
            return "no disponible"
    return _cached(("spotify",), 20, fetch)


def weather_for(geo, fallback_location, get_weather, weather_summary):
    if geo and geo.get("lat") is not None:
        loc = dict(fallback_location or {})
        loc.update({"lat": float(geo["lat"]), "lon": float(geo["lon"])})
        key = ("weather", round(loc["lat"], 1), round(loc["lon"], 1))
    else:
        loc = fallback_location or {}
        key = ("weather", loc.get("city"))
    try:
        w = _cached(key, 15 * 60, lambda: get_weather(loc))
        return weather_summary(w) if w else "no disponible"
    except Exception as e:
        print(f"[CONTEXT] clima: {e}")
        return "no disponible"


# ── Armado final ─────────────────────────────────────────────────────────────
def build_context_snapshot(session, client_ctx, *, db_q, get_weather, weather_summary,
                           spotify_headers):
    """Regresa el bloque de texto listo para inyectarse en el system prompt."""
    client_ctx = client_ctx or {}
    geo = client_ctx.get("geo") if isinstance(client_ctx.get("geo"), dict) else None
    location = session.get("location") or {}
    user_type = "owner" if session.get("user_type") == "owner" else "guests"
    is_owner = user_type == "owner"

    f_spatial  = _pool.submit(spatial_block, geo, location)
    f_weather  = _pool.submit(weather_for, geo, location, get_weather, weather_summary)
    f_music    = _pool.submit(now_playing, spotify_headers) if is_owner else None
    f_personal = _pool.submit(personal_block, db_q, user_type)

    def safe(f, default, timeout=6):
        if f is None:
            return default
        try:
            return f.result(timeout=timeout)
        except Exception as e:
            print(f"[CONTEXT] bloque falló: {e}")
            return default

    mode = client_ctx.get("mode")
    mode_line = ("- Canal: VOZ (lo escucha por bocina; puede ir manejando)." if mode == "voice"
                 else "- Canal: TEXTO en pantalla.")

    ambient = [f"- Clima: {safe(f_weather, 'no disponible')}"]
    if is_owner:
        ambient.append(f"- Spotify: {safe(f_music, 'no disponible')}")

    return (
        "FOTO DE CONTEXTO (se actualiza en cada mensaje):\n"
        "[TEMPORAL]\n" + temporal_block() + "\n"
        "[ESPACIAL]\n" + safe(f_spatial, spatial_block(None, location)) + "\n"
        "[PERSONAL INMEDIATO]\n" + safe(f_personal, "- (sin datos)") + "\n" + mode_line + "\n"
        "[AMBIENTAL]\n" + "\n".join(ambient)
    )
