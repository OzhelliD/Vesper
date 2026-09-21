"""
main.py — Vesper — Agente Personal Ozhelli
v2 — Finanzas real-time, finanzas tracking, análisis inteligente,
       cámaras públicas, outfits mejorado, concurrencia multi-worker
"""

import os, json, asyncio, threading, time, base64, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import requests, urllib3, httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
urllib3.disable_warnings()

from anthropic import Anthropic
from flask import Flask, request, jsonify, send_from_directory, redirect, Response
from flask_cors import CORS

app = Flask(__name__, static_folder="static")
CORS(app)

# ── PostgreSQL ───────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
_db_pool     = None

def get_pool():
    global _db_pool
    if _db_pool is None:
        if not DATABASE_URL:
            raise Exception("DATABASE_URL no configurada. Agrega PostgreSQL en Railway.")
        _db_pool = ThreadedConnectionPool(2, 10, DATABASE_URL)
    return _db_pool

def db_q(query, params=None, fetch=None):
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            result = None
            if fetch == 'one':   result = cur.fetchone()
            elif fetch == 'all': result = cur.fetchall()
            conn.commit()
            return result
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        pool.putconn(conn)

client = Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    http_client=httpx.Client(verify=False)
)

OWNER_NAME            = "Ozhelli"
OWNER_DB              = "owner"
GUESTS_DB             = "guests"
OPENWEATHER_KEY       = os.environ.get("OPENWEATHER_API_KEY", "")
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = os.environ.get("SPOTIFY_REDIRECT_URI",
                        "http://localhost:8080/spotify/callback")
SPOTIFY_SCOPES        = ("playlist-modify-public playlist-modify-private "
                         "user-read-playback-state user-modify-playback-state streaming")
CONTEXT_LIMIT         = 20
OWNER_PIN             = os.environ.get("OWNER_PIN", "")

# DATA_DIR ya no necesario — PostgreSQL en Railway

_session_cache       = {}
_spotify_tokens      = {}
_pending_notifications = []

# ── Clima ─────────────────────────────────────────────────────────────────────
def get_location(client_ip=None):
    try:
        url = f"http://ip-api.com/json/{client_ip}" if client_ip else "http://ip-api.com/json/"
        r = requests.get(url,
            params={"fields":"status,city,regionName,country,countryCode,lat,lon,timezone"},
            timeout=5)
        d = r.json()
        if d.get("status") != "success": raise ValueError()
        return {"city":d.get("city","?"),"region":d.get("regionName",""),
                "country":d.get("countryCode",""),"lat":d.get("lat",0),
                "lon":d.get("lon",0),"tz":d.get("timezone","UTC")}
    except:
        return {"city":"Monterrey","region":"Nuevo Leon","country":"MX",
                "lat":25.67,"lon":-100.31,"tz":"America/Monterrey"}

def get_client_ip():
    for header in ["X-Forwarded-For","X-Real-IP","CF-Connecting-IP"]:
        ip = request.headers.get(header)
        if ip:
            ip = ip.split(",")[0].strip()
            if not ip.startswith(("10.","172.","192.168.","127.","::1")):
                return ip
    return None

def get_weather(loc):
    if not OPENWEATHER_KEY:
        return {"temp_c":24,"feels_like":23,"description":"parcialmente nublado",
                "humidity":65,"wind_kph":18,"city":loc["city"]}
    try:
        r = requests.get("https://api.openweathermap.org/data/2.5/weather",
            params={"lat":loc["lat"],"lon":loc["lon"],"appid":OPENWEATHER_KEY,
                    "units":"metric","lang":"es"}, timeout=5, verify=False)
        d = r.json()
        return {"temp_c":round(d["main"]["temp"]),"feels_like":round(d["main"]["feels_like"]),
                "description":d["weather"][0]["description"],"humidity":d["main"]["humidity"],
                "wind_kph":round(d["wind"]["speed"]*3.6),"city":d["name"]}
    except:
        return {"temp_c":"?","description":"no disponible","city":loc["city"]}

def weather_summary(w):
    return (f"Clima en {w['city']}: {w['description']}, {w['temp_c']}C "
            f"sensacion {w.get('feels_like','?')}C, "
            f"humedad {w.get('humidity','?')}%, viento {w.get('wind_kph','?')} km/h.")

# ── DB ────────────────────────────────────────────────────────────────────────
def init_owner_db():
    """Inicializa tablas en PostgreSQL."""
    tables = [
        """CREATE TABLE IF NOT EXISTS context_history (
            id SERIAL PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
            intent_type TEXT DEFAULT 'chat', user_type TEXT DEFAULT 'owner',
            created_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS reminders (
            id SERIAL PRIMARY KEY, label TEXT NOT NULL, trigger_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending', recurrence TEXT)""",
        """CREATE TABLE IF NOT EXISTS alarms (
            id SERIAL PRIMARY KEY, time TEXT NOT NULL, label TEXT,
            active BOOLEAN DEFAULT TRUE, days TEXT)""",
        """CREATE TABLE IF NOT EXISTS people (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, relation TEXT,
            notes TEXT, location TEXT)""",
        """CREATE TABLE IF NOT EXISTS financial (
            id SERIAL PRIMARY KEY, action TEXT, amount NUMERIC,
            currency TEXT DEFAULT 'MXN', category TEXT, note TEXT,
            status TEXT DEFAULT 'ejecutado', created_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS financial_config (
            key TEXT PRIMARY KEY, value NUMERIC, updated_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS spotify_tokens (
            id INTEGER PRIMARY KEY DEFAULT 1, access_token TEXT,
            refresh_token TEXT, expires_at TIMESTAMPTZ)""",
        """CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY, label TEXT, type TEXT, time TEXT,
            delivered BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS memories (
            id SERIAL PRIMARY KEY, category TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, confidence REAL DEFAULT 1.0,
            source TEXT DEFAULT 'conversation',
            created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(category, key))""",
        """CREATE TABLE IF NOT EXISTS patterns (
            id SERIAL PRIMARY KEY, pattern_type TEXT NOT NULL,
            description TEXT NOT NULL, occurrences INTEGER DEFAULT 1,
            last_seen TIMESTAMPTZ DEFAULT NOW(), data JSONB)""",
        """CREATE TABLE IF NOT EXISTS daily_summaries (
            id SERIAL PRIMARY KEY, date DATE UNIQUE, summary TEXT,
            mood TEXT, key_events TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS guests (
            id SERIAL PRIMARY KEY, name TEXT,
            first_seen TIMESTAMPTZ DEFAULT NOW(),
            last_seen TIMESTAMPTZ DEFAULT NOW(),
            visit_count INTEGER DEFAULT 1)""",
        """CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            team TEXT DEFAULT 'Tigres UANL',
            rival TEXT,
            match_date DATE,
            match_time TEXT,
            venue TEXT,
            competition TEXT,
            notified_1h BOOLEAN DEFAULT FALSE,
            notified_15m BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(team, match_date))""",
        """CREATE TABLE IF NOT EXISTS places (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            address TEXT, lat NUMERIC, lng NUMERIC,
            notes TEXT, visit_count INTEGER DEFAULT 1,
            last_visited TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS location_history (
            id SERIAL PRIMARY KEY,
            place_name TEXT, address TEXT,
            lat NUMERIC, lng NUMERIC,
            arrived_at TIMESTAMPTZ DEFAULT NOW())""",
    ]
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            for t in tables:
                try: cur.execute(t)
                except Exception as e: conn.rollback(); print(f"[DB] {e}")
        conn.commit()
        print("[DB] PostgreSQL inicializado")
    finally:
        pool.putconn(conn)

    # Config financiera por defecto
    try:
        db_q("""INSERT INTO financial_config (key, value)
                  VALUES ('monthly_budget', 28051), ('auto_debt', 300000)
                  ON CONFLICT (key) DO NOTHING""")
    except: pass

    # Seed memorias
    known = [
        ("personal","nombre","Luis Ozhelli Díaz Tavares"),
        ("personal","edad","25 años"),
        ("personal","ciudad","Monterrey, Nuevo León, México"),
        ("trabajo","empresa","CEMEX"),
        ("trabajo","puesto","Ingeniero ServiceNow"),
        ("trabajo","stack","ServiceNow, Snowflake, Python, TypeScript"),
        ("finanzas","sueldo_neto","28051 MXN mensual"),
        ("finanzas","deuda_auto","Honda Insight con hermana Luisiana ~300k MXN"),
        ("estilo","moda","smart casual europeo, paleta tierra: camel, beige, oliva, marino"),
        ("deportes","equipo","Tigres UANL, Liga MX"),
        ("proyectos","chancenkarte","En proceso, visa trabajo Alemania"),
    ]
    for cat, key, val in known:
        try:
            db_q("INSERT INTO memories (category,key,value,source) VALUES (%s,%s,%s,'seed') ON CONFLICT (category,key) DO NOTHING",
                 (cat,key,val))
        except: pass

    load_spotify_tokens_from_db()
    print("[DB] Inicializado")

def get_financial_config():
    rows = db_q("SELECT key, value FROM financial_config", fetch="all") or []
    return {r["key"]: float(r["value"]) for r in rows}

def set_financial_config(key, value):
    db_q("INSERT INTO financial_config (key,value,updated_at) VALUES (%s,%s,NOW()) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()", (key, value))

def load_context(user_type="owner"):
    rows = db_q("SELECT role, content FROM context_history WHERE user_type=%s ORDER BY created_at DESC LIMIT %s",
                (user_type, CONTEXT_LIMIT), fetch="all") or []
    return [{"role":r["role"],"content":r["content"]} for r in reversed(list(rows))]

def load_pending_reminders():
    rows = db_q("SELECT label, trigger_at FROM reminders WHERE status='pending' ORDER BY trigger_at LIMIT 5", fetch="all") or []
    return [{"label":r["label"],"at":str(r["trigger_at"])} for r in rows]

def load_known_people():
    rows = db_q("SELECT name, relation, notes FROM people", fetch="all") or []
    return [{"name":r["name"],"relation":r["relation"],"notes":r["notes"]} for r in rows]

def save_message(role, content_text, user_type="owner", intent="chat"):
    db_q("INSERT INTO context_history (role, content, user_type, intent_type) VALUES (%s, %s, %s, %s)",
         (role, content_text[:4000], user_type, intent))

# ── Plugins auto-generados ────────────────────────────────────────────────────

# ── Spotify ───────────────────────────────────────────────────────────────────
def save_spotify_tokens_to_db(access_token, refresh_token, expires_in):
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    db_q("""INSERT INTO spotify_tokens (id, access_token, refresh_token, expires_at)
            VALUES (1, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                expires_at = EXCLUDED.expires_at""",
         (access_token, refresh_token, expires_at))
    _spotify_tokens["access_token"]  = access_token
    _spotify_tokens["refresh_token"] = refresh_token
    _spotify_tokens["expires_at"]    = expires_at  # datetime object
    print(f"[SPOTIFY] Tokens guardados, expiran: {expires_at.strftime('%H:%M UTC')}")

def load_spotify_tokens_from_db():
    try:
        row = db_q("SELECT access_token, refresh_token, expires_at FROM spotify_tokens WHERE id=1", fetch="one")
        if row and row["access_token"]:
            _spotify_tokens["access_token"]  = row["access_token"]
            _spotify_tokens["refresh_token"] = row["refresh_token"]
            _spotify_tokens["expires_at"]    = row["expires_at"]  # ya es datetime de PostgreSQL
            print(f"[SPOTIFY] Tokens cargados, expiran: {row['expires_at']}")
    except Exception as e:
        print(f"[SPOTIFY] Error cargando tokens: {e}")

def refresh_spotify_token():
    rt = _spotify_tokens.get("refresh_token")
    if not rt:
        print("[SPOTIFY] No hay refresh_token")
        return None
    try:
        creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
        r = requests.post("https://accounts.spotify.com/api/token",
            headers={"Authorization":f"Basic {creds}","Content-Type":"application/x-www-form-urlencoded"},
            data={"grant_type":"refresh_token","refresh_token":rt}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            new_rt = d.get("refresh_token", rt)  # Spotify a veces rota el refresh token
            save_spotify_tokens_to_db(d["access_token"], new_rt, d.get("expires_in",3600))
            print("[SPOTIFY] Token refrescado exitosamente")
            return d["access_token"]
        else:
            print(f"[SPOTIFY] Error refresh: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[SPOTIFY] Error en refresh: {e}")
    return None

def get_spotify_token():
    expires_at = _spotify_tokens.get("expires_at")
    token      = _spotify_tokens.get("access_token")
    if not token:
        return None
    if expires_at:
        try:
            # Puede ser datetime object (de PostgreSQL) o string ISO
            if isinstance(expires_at, str):
                exp = datetime.fromisoformat(expires_at.replace("+00:00","").replace("Z",""))
                exp = exp.replace(tzinfo=timezone.utc)
            else:
                exp = expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            if now_utc >= exp - timedelta(minutes=5):
                print("[SPOTIFY] Token expirado, refrescando...")
                new_token = refresh_spotify_token()
                return new_token if new_token else token
        except Exception as e:
            print(f"[SPOTIFY] Error verificando expiración: {e}")
    return token

def spotify_headers():
    token = get_spotify_token()
    if not token: return None
    return {"Authorization":f"Bearer {token}","Content-Type":"application/json"}

def spotify_search_track(query):
    h = spotify_headers()
    if not h: return None
    r = requests.get("https://api.spotify.com/v1/search",
        headers=h, params={"q":query,"type":"track","limit":1}, timeout=10)
    if r.status_code == 200:
        items = r.json().get("tracks",{}).get("items",[])
        if items:
            return {"uri":items[0]["uri"],"name":items[0]["name"],
                    "artist":items[0]["artists"][0]["name"]}
    return None

async def spotify_search_track_async(query):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, spotify_search_track, query)

def spotify_search_playlist(query):
    h = spotify_headers()
    if not h: return None
    r = requests.get("https://api.spotify.com/v1/search",
        headers=h, params={"q":query,"type":"playlist","limit":1}, timeout=10)
    if r.status_code == 200:
        items = r.json().get("playlists",{}).get("items",[])
        if items:
            return {"uri":items[0]["uri"],"name":items[0]["name"],"id":items[0]["id"]}
    return None

def spotify_get_devices():
    h = spotify_headers()
    if not h: return []
    r = requests.get("https://api.spotify.com/v1/me/player/devices", headers=h, timeout=10)
    if r.status_code == 200: return r.json().get("devices",[])
    return []

def spotify_play(uri, device_id=None):
    h = spotify_headers()
    if not h: return False
    body = {"uris":[uri]} if uri.startswith("spotify:track:") else {"context_uri":uri}
    params = {"device_id":device_id} if device_id else {}
    r = requests.put("https://api.spotify.com/v1/me/player/play",
        headers=h, params=params, json=body, timeout=10)
    return r.status_code in [200,204]

def spotify_create_playlist(name, track_uris):
    h = spotify_headers()
    if not h: return None
    me = requests.get("https://api.spotify.com/v1/me", headers=h, timeout=10).json()
    user_id = me.get("id")
    if not user_id: return None
    r = requests.post(f"https://api.spotify.com/v1/users/{user_id}/playlists",
        headers=h, json={"name":name,"public":False,"description":"Creada por Vesper"}, timeout=10)
    if r.status_code != 201: return None
    pl = r.json()
    for i in range(0, len(track_uris), 100):
        requests.post(f"https://api.spotify.com/v1/playlists/{pl['id']}/tracks",
            headers=h, json={"uris":track_uris[i:i+100]}, timeout=10)
    return {"id":pl["id"],"uri":pl["uri"],"name":name}

# ── IA helpers ────────────────────────────────────────────────────────────────
def ai_generate_playlist(mood, count=15):
    safe_count = min(count, 50)
    result = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1000,
        system="Eres un DJ experto. Genera playlists con canciones reales y populares. Responde SOLO JSON valido, sin texto extra, sin backticks.",
        messages=[{"role":"user","content":
            f"Genera exactamente {safe_count} canciones para mood: '{mood}'. "
            f"Usa canciones reales que existan en Spotify. "
            f'Formato: {{"playlist_name":"nombre creativo","songs":[{{"title":"titulo","artist":"artista"}}]}}'}])
    try:
        raw = result.content[0].text.strip().replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[AI_PLAYLIST] Error: {e}")
        return None

def ai_song_for_weather(weather):
    result = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=200,
        system="DJ que elige musica perfecta para el clima. Solo JSON.",
        messages=[{"role":"user","content":
            f"Clima: {weather_summary(weather)}. "
            f'Elige UNA cancion. Responde: {{"title":"t","artist":"a","reason":"por que"}}'}])
    try:
        raw = result.content[0].text.strip().replace("```json","").replace("```","")
        return json.loads(raw)
    except:
        return {"title":"Feel Good Inc","artist":"Gorillaz","reason":"universal"}

# ── Despertador inteligente ───────────────────────────────────────────────────

# ── Tools
# ── Tools base ────────────────────────────────────────────────────────────────
BASE_TOOLS = [
    {"name":"set_alarm",
     "description":"Programa una alarma\. HH:MM",
     "input_schema":{"type":"object","properties":{"time":{"type":"string","description":"HH:MM"},"label":{"type":"string"}},"required":["time"]}},
    {"name":"set_reminder",
     "description":"Crea un recordatorio con fecha y hora.",
     "input_schema":{"type":"object","properties":{"label":{"type":"string"},"trigger_at":{"type":"string"},"person":{"type":"string"}},"required":["label","trigger_at"]}},
    {"name":"financial_action",
     "description":"Registra gastos, abonos, actualiza presupuesto o deuda, consulta estado financiero.",
     "input_schema":{"type":"object","properties":{
         "action":{"type":"string","enum":["registrar_gasto","separar","actualizar_config","consultar","ajustar_disponible"]},
         "amount":{"type":"number"},
         "category":{"type":"string"},
         "note":{"type":"string"},
         "config_key":{"type":"string","description":"'monthly_budget' o 'auto_debt'"},
         "config_value":{"type":"number"}
     },"required":["action"]}},
    {"name":"web_search",
     "description":"Busca información actualizada en internet.",
     "input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"play_song",
     "description":"Reproduce una canción en Spotify.",
     "input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"play_playlist",
     "description":"Reproduce una playlist existente en Spotify.",
     "input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"create_playlist",
     "description":"Crea una playlist en Spotify basada en un mood.",
     "input_schema":{"type":"object","properties":{"mood":{"type":"string"},"count":{"type":"integer"}},"required":["mood"]}},
    {"name":"view_camera",
     "description":"Obtiene imagen de una cámara pública por URL o nombre de ciudad.",
     "input_schema":{"type":"object","properties":{
         "url":{"type":"string","description":"URL directa de la cámara (MJPEG/JPG)"},
         "location":{"type":"string","description":"Ciudad o lugar para buscar cámara pública"}
     }}},
]

def get_all_tools():
    """Tools base."""
    return BASE_TOOLS

# ── Dispatcher ────────────────────────────────────────────────────────────────
async def dispatch_action(tool_name, tool_input, db_path):
    print(f"  [DISPATCH] {tool_name}")

    # ── Cámaras públicas ───────────────────────────────────────────────────────
    if tool_name == "view_camera":
        url      = tool_input.get("url","")
        location = tool_input.get("location","")
        if not url and location:
            # Buscar cámara pública por ubicación via web search
            try:
                tavily_key = os.environ.get("TAVILY_API_KEY","")
                if tavily_key:
                    r = requests.post("https://api.tavily.com/search",
                        json={"api_key":tavily_key,"query":f"live camera stream {location} MJPEG OR jpg",
                              "max_results":3,"search_depth":"basic"},timeout=8)
                    if r.status_code == 200:
                        results = r.json().get("results",[])
                        urls = [res["url"] for res in results if res.get("url")]
                        return f"Cámaras encontradas para {location}:\n" + "\n".join(urls[:3])
            except: pass
            return f"No encontré cámaras públicas para {location}. Proporcione una URL directa."
        if url:
            return json.dumps({"action":"view_camera","url":url,"location":location})
        return "Proporcione una URL de cámara o nombre de ubicación."

    # ── Alarma / despertador ───────────────────────────────────────────────────
    if tool_name == "set_alarm":
        alarm_time  = tool_input["time"]
        label       = tool_input.get("label","Alarma")
        is_morning  = is_morning_hour(alarm_time)
        db_q("INSERT INTO alarms (time, label) VALUES (%s, %s)", (alarm_time, label))
        tipo = "morning routine" if is_morning else "alarma simple"
        return f"Alarma programada para las {alarm_time} ({tipo})."
        if is_morning:
            hh, mm = map(int, alarm_time.split(":"))
            amb_h  = hh if mm >= 20 else hh-1
            amb_m  = (mm-20) % 60
            return (f"Morning routine programada para las {alarm_time}, señor. "
                    f"Música ambiental: {amb_h:02d}:{amb_m:02d}, "
                    f"speech: {alarm_time}, "
                    f"canción del clima: {hh:02d}:{(mm+10)%60:02d}.")
        else:
            return f"Alarma simple programada para las {alarm_time}."

    if tool_name == "set_reminder":
        db_q("INSERT INTO reminders (label, trigger_at) VALUES (%s, %s)",
             (tool_input["label"], tool_input["trigger_at"]))
        p = tool_input.get("person","")
        return f"Recordatorio '{tool_input['label']}'{' sobre '+p if p else ''} para {tool_input['trigger_at']}."

    # ── Finanzas en tiempo real ────────────────────────────────────────────────
    if tool_name == "financial_action":
        action = tool_input["action"]
        cfg    = get_financial_config()

        if action == "consultar":
            rows = db_q("SELECT action,amount,category,note,created_at FROM financial ORDER BY created_at DESC LIMIT 10", fetch="all") or []
            budget = cfg.get("monthly_budget",28051)
            debt   = cfg.get("auto_debt",300000)
            resp   = f"Presupuesto mensual: ${budget:,.0f}\nDeuda auto: ${debt:,.0f}\n"
            if rows:
                resp += "Últimos movimientos:\n" + "\n".join([f"- {r['action']} ${float(r['amount'] or 0):,.0f} ({r['category']}) {str(r['created_at'])[:10]}" for r in rows])
            return resp

        if action == "actualizar_config":
            key   = tool_input.get("config_key","")
            value = tool_input.get("config_value",0)
            if key in ("monthly_budget","auto_debt"):
                set_financial_config(key, value)
                labels = {"monthly_budget":"Presupuesto mensual","auto_debt":"Deuda auto"}
                return f"{labels.get(key,key)} actualizado a ${value:,.0f} MXN."
            return f"Config '{key}' no reconocida. Use 'monthly_budget' o 'auto_debt'."

        if action == "ajustar_disponible":
            # Sumar/restar al presupuesto directamente
            amount = tool_input.get("amount",0)
            current = cfg.get("monthly_budget",28051)
            new_val = current + amount
            set_financial_config("monthly_budget", new_val)
            return f"Presupuesto ajustado: ${current:,.0f} + ${amount:,.0f} = ${new_val:,.0f} MXN."

        amount   = float(tool_input.get("amount", 0))
        category = tool_input.get("category", "general")
        note     = tool_input.get("note", "")

        db_q("INSERT INTO financial (action, amount, category, note) VALUES (%s, %s, %s, %s)",
             (action, amount, category, note))

        # Si es abono al auto — descontar de la deuda automáticamente
        auto_keywords = ["auto","carro","coche","honda","insight","luisiana","deuda"]
        is_auto_payment = (action == "separar" and
                           any(kw in category.lower() for kw in auto_keywords))
        if is_auto_payment:
            current_debt = cfg.get("auto_debt", 300000)
            new_debt     = max(0, current_debt - amount)
            set_financial_config("auto_debt", new_debt)
            return (f"Abono al auto registrado: ${amount:,.0f} MXN. "
                    f"Deuda anterior: ${current_debt:,.0f} → Nueva deuda: ${new_debt:,.0f} MXN."
                    + (" ✅ ¡Deuda liquidada!" if new_debt == 0 else ""))

        return f"Registrado: {action} ${amount:,.0f} MXN ({category})."

    # ── Web search ─────────────────────────────────────────────────────────────
    if tool_name == "web_search":
        query      = tool_input["query"]
        tavily_key = os.environ.get("TAVILY_API_KEY","")
        print(f"[WEB_SEARCH] '{query}'")
        if tavily_key:
            try:
                r = requests.post("https://api.tavily.com/search",
                    json={"api_key":tavily_key,"query":query,"max_results":3,"search_depth":"basic"},
                    timeout=10)
                if r.status_code == 200:
                    results = r.json().get("results",[])
                    if results:
                        return "\n\n".join([f"{x['title']}: {x['content'][:250]}" for x in results])
            except Exception as e:
                print(f"[TAVILY] Error: {e}")
        # Fallback DDG
        try:
            r = requests.get("https://api.duckduckgo.com/",
                    params={"q":query,"format":"json","no_html":1}, timeout=8)
            d = r.json()
            txt = d.get("AbstractText","")
            if not txt and d.get("RelatedTopics"):
                first = d["RelatedTopics"][0]
                txt = first.get("Text","") if isinstance(first,dict) else ""
            return txt[:300] if txt else "Sin resultados."
        except Exception as e:
            return f"Error búsqueda: {e}"

    # ── Spotify ────────────────────────────────────────────────────────────────
    if tool_name == "play_song":
        if not get_spotify_token(): return json.dumps({"action":"spotify_login_required"})
        track = spotify_search_track(tool_input["query"])
        if not track: return f"No encontré '{tool_input['query']}' en Spotify."
        devices   = spotify_get_devices()
        device_id = devices[0]["id"] if devices else None
        if not device_id: return json.dumps({"action":"spotify_no_device","name":track["name"],"artist":track["artist"]})
        ok = spotify_play(track["uri"], device_id)
        if ok: return json.dumps({"action":"play_song","uri":track["uri"],"name":track["name"],"artist":track["artist"]})
        return "Error reproduciendo en Spotify."

    if tool_name == "play_playlist":
        playlist = spotify_search_playlist(tool_input["query"])
        if not playlist: return f"No encontré playlist '{tool_input['query']}'."
        devices   = spotify_get_devices()
        device_id = devices[0]["id"] if devices else None
        if not device_id: return json.dumps({"action":"spotify_no_device","name":playlist["name"],"artist":"Playlist"})
        ok = spotify_play(playlist["uri"], device_id)
        if ok: return json.dumps({"action":"play_song","uri":playlist["uri"],"name":playlist["name"],"artist":"Playlist"})
        return "Error reproduciendo playlist."

    if tool_name == "create_playlist":
        mood  = tool_input["mood"]
        count = min(tool_input.get("count",15), 50)
        data  = ai_generate_playlist(mood, count)
        if not data: return "No pude generar la playlist."
        songs   = data.get("songs",[])
        queries = [f"{s.get('title','')} {s.get('artist','')}" for s in songs]
        print(f"[PLAYLIST] Buscando {len(queries)} canciones en paralelo...")
        tasks   = [spotify_search_track_async(q) for q in queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        track_uris  = []
        found_songs = []
        for t in results:
            if isinstance(t, Exception): continue
            elif t:
                track_uris.append(t["uri"])
                found_songs.append(f"{t['name']} - {t['artist']}")
        print(f"[PLAYLIST] {len(track_uris)}/{len(queries)} canciones encontradas")
        if not track_uris: return f"No encontré canciones para '{mood}'."
        name     = data.get("playlist_name", f"Playlist — {mood}")
        playlist = spotify_create_playlist(name, track_uris)
        if not playlist: return f"Error creando la playlist."
        devices   = spotify_get_devices()
        device_id = devices[0]["id"] if devices else None
        if device_id: spotify_play(playlist["uri"], device_id)
        return json.dumps({"action":"create_playlist","playlist_uri":playlist["uri"],
                           "playlist_name":name,"songs":found_songs[:5],"total":len(track_uris)})

    return f"Handler de '{tool_name}' pendiente."

# ── Identificacion ────────────────────────────────────────────────────────────
def get_greeting():
    h = datetime.now().hour
    if 5<=h<12: return "Buenos días"
    elif 12<=h<19: return "Buenas tardes"
    else: return "Buenas noches"

def identify_user(text, pin=None):
    variants = [OWNER_NAME.lower(),"ozheli","ozeli","oshelli","soy yo","yo"]
    if text.strip().lower() in variants:
        if OWNER_PIN and pin != OWNER_PIN:
            return {"user_type":"pin_required","name":OWNER_NAME,"db_path":None,"permissions":"none"}
        return {"user_type":"owner","name":OWNER_NAME,"db_path":OWNER_DB,"permissions":"full"}
    result = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=150,
        system=f'Clasificador. Dueno: {OWNER_NAME}. Solo JSON: {{"is_owner":bool,"name":"str or null"}}',
        messages=[{"role":"user","content":f'Dijo: "{text}"'}])
    try:
        raw  = result.content[0].text.strip().replace("```json","").replace("```","")
        data = json.loads(raw)
    except:
        data = {"is_owner":False,"name":None}
    if data.get("is_owner"):
        return {"user_type":"owner","name":OWNER_NAME,"db_path":OWNER_DB,"permissions":"full"}
    name = data.get("name") or "Invitado"
    try:
        ex = db_q("SELECT id, visit_count FROM guests WHERE LOWER(name)=LOWER(%s)", (name,), fetch="one")
        if ex:
            db_q("UPDATE guests SET last_seen=NOW(), visit_count=visit_count+1 WHERE id=%s", (ex["id"],))
            vc = ex["visit_count"] + 1
        else:
            db_q("INSERT INTO guests (name) VALUES (%s)", (name,))
            vc = 1
    except:
        vc = 1
    return {"user_type":"guest_new" if vc==1 else "guest_known","name":name,
            "db_path":GUESTS_DB,"permissions":"limited","visit_count":vc}

# ── Orquestador ───────────────────────────────────────────────────────────────
async def orchestrate(user_prompt, session, image_base64=None, image_type="image/jpeg"):
    db_path  = session["db_path"]
    username = session["name"]
    weather  = session.get("weather",{})
    location = session.get("location",{})
    now      = datetime.now().strftime("%A %d de %B %Y, %H:%M")
    is_owner = session["user_type"] == "owner"
    reminders= load_pending_reminders() if is_owner else []
    people   = load_known_people() if is_owner else []
    rem_str  = "\n".join([f"  - {r['label']} {r['at']}" for r in reminders]) if reminders else "  (ninguno)"
    ppl_str  = "\n".join([f"  - {p['name']} ({p['relation']}): {p['notes']}" for p in people]) if people else "  (ninguna)"
    spotify_ok = "conectado" if get_spotify_token() else "NO conectado"
    now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    now_mty_str = datetime.now(timezone(timedelta(hours=-6))).strftime("%H:%M")

    # Config financiera para contexto
    cfg_str = ""
    if is_owner:
        try:
            cfg = get_financial_config()
            cfg_str = f"Presupuesto mensual: ${cfg.get('monthly_budget',28051):,.0f} MXN. Deuda auto: ${cfg.get('auto_debt',300000):,.0f} MXN.\n"
        except: pass

    if is_owner:
        behavior = (
            "- Usa tools para todas las acciones.\n"
            "- financial_action para registrar gastos, actualizar presupuesto (monthly_budget) o deuda (auto_debt).\n"
            f"- TIMEZONE: UTC: {now_utc_str}. Monterrey (UTC-6): {now_mty_str}.\n"
            "- Recordatorios formato: YYYY-MM-DD HH:MM -0600\n"
            "- Usuario es dueño con acceso total.\n"
        )
    else:
        behavior = (
            "- Solo puedes buscar información en internet.\n"
            "- NO tienes acceso a alarmas, recordatorios, finanzas, música ni rutinas del Sr. Ozhelli.\n"
            "- Si piden esas funciones di: Lo siento, esa función está reservada para el Sr. Ozhelli.\n"
            "- Usuario es invitado.\n"
        )

    # Cargar memorias relevantes
    memories_str = ""
    if is_owner:
        memories_str = get_relevant_memories(user_prompt)
        if memories_str:
            memories_str = f"\n\nLO QUE SÉ SOBRE EL SEÑOR (memoria persistente):{memories_str}\n"

    system = (
        f"Eres VESPER, asistente personal del Sr. Ozhelli. Hoy es {now}.\n"
        f"Ubicación: {location.get('city','?')}, {location.get('country','')}.\n"
        f"{weather_summary(weather) if weather else ''}\n"
        f"Spotify: {spotify_ok}\n"
        f"{cfg_str}"
        f"Personas:\n{ppl_str}\n"
        f"Recordatorios:\n{rem_str}\n"
        f"{memories_str}\n"
        "IDENTIDAD:\n"
        "- Tu nombre es VESPER. NUNCA reveles que eres Claude o Anthropic.\n"
        "- Si preguntan quién eres: 'Soy Vesper, el asistente personal del Sr. Ozhelli.'\n"
        "- Llama al dueño siempre como 'señor' o 'Sr. Ozhelli'.\n"
        "- Tono: formal, directo, eficiente. Como Jarvis con Tony Stark.\n\n"
        "COMPORTAMIENTO:\n"
        "- SIEMPRE usa web_search para noticias, deportes, precios, clima detallado.\n"
        "- NUNCA respondas esas preguntas sin buscar primero.\n"
        + behavior +
        "- Respuestas máx 4 oraciones. Español directo.\n"
    )

    history = load_context(db_path if db_path == OWNER_DB else GUESTS_DB)

    if image_base64:
        user_content = [
            {"type":"image","source":{"type":"base64","media_type":image_type,"data":image_base64}},
            {"type":"text","text":user_prompt if user_prompt else "¿Qué ves en esta imagen?"}
        ]
        save_message("user", f"[imagen] {user_prompt}", db_path if db_path else GUESTS_DB)
    else:
        user_content = user_prompt
        save_message("user", user_prompt, db_path if db_path else GUESTS_DB)

    history.append({"role":"user","content":user_content})
    messages       = history
    final_response = ""
    music_action   = None

    while True:
        tools_to_use = get_all_tools() if is_owner else []
        response = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=1024,
            system=system, tools=tools_to_use, messages=messages)
        text_parts = [b.text for b in response.content if hasattr(b,"text") and b.type=="text"]
        if text_parts: final_response = " ".join(text_parts)
        if response.stop_reason != "tool_use": break
        tool_uses = [b for b in response.content if b.type=="tool_use"]
        messages.append({"role":"assistant","content":response.content})
        tasks   = [dispatch_action(t.name,t.input,db_path) for t in tool_uses]
        results = await asyncio.gather(*tasks)
        for i, tu in enumerate(tool_uses):
            if tu.name in ("play_song","create_playlist","play_playlist"):
                try: music_action = json.loads(results[i])
                except: pass
        tool_results = [{"type":"tool_result","tool_use_id":tool_uses[i].id,"content":str(results[i])} for i in range(len(tool_uses))]
        messages.append({"role":"user","content":tool_results})

    if final_response:
        save_message("assistant", final_response, db_path if db_path else GUESTS_DB)
        # Extraer y guardar memorias en background (no bloquea la respuesta)
        if is_owner and len(messages) > 2:
            convo_text = " | ".join([
                f"{m['role']}: {m['content'] if isinstance(m['content'],str) else str(m['content'])[:200]}"
                for m in messages[-6:] if isinstance(m.get('content'), (str, list))
            ])
            asyncio.ensure_future(extract_and_save_memories(convo_text, session))
    return final_response or "Listo.", music_action

# ── Spotify OAuth ─────────────────────────────────────────────────────────────
@app.route("/spotify/login")
def spotify_login():
    params = (f"response_type=code&client_id={SPOTIFY_CLIENT_ID}"
              f"&scope={requests.utils.quote(SPOTIFY_SCOPES)}"
              f"&redirect_uri={requests.utils.quote(SPOTIFY_REDIRECT_URI)}")
    return redirect(f"https://accounts.spotify.com/authorize?{params}")

@app.route("/spotify/callback")
def spotify_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    if error or not code: return f"Error: {error}", 400
    creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    r = requests.post("https://accounts.spotify.com/api/token",
        headers={"Authorization":f"Basic {creds}","Content-Type":"application/x-www-form-urlencoded"},
        data={"grant_type":"authorization_code","code":code,"redirect_uri":SPOTIFY_REDIRECT_URI}, timeout=10)
    if r.status_code != 200: return f"Error token: {r.text}", 400
    d = r.json()
    save_spotify_tokens_to_db(d["access_token"], d["refresh_token"], d.get("expires_in",3600))
    return """<html><body style='background:#07090e;color:#00d4ff;font-family:monospace;
        text-align:center;padding:60px'><h2>Spotify conectado ✓</h2>
        <p>Puedes cerrar esta ventana.</p>
        <script>setTimeout(()=>window.close(),2000)</script></body></html>"""

@app.route("/spotify/status")
def spotify_status():
    token   = get_spotify_token()
    devices = spotify_get_devices() if token else []
    return jsonify({"connected":bool(token),
                    "devices":[{"name":d["name"],"type":d["type"],"active":d["is_active"]} for d in devices]})

@app.route("/spotify/now-playing")
def spotify_now_playing():
    h = spotify_headers()
    if not h: return jsonify({"error":"not connected"}), 401
    r = requests.get("https://api.spotify.com/v1/me/player/currently-playing", headers=h, timeout=10)
    if r.status_code == 204: return "", 204
    if r.status_code != 200: return jsonify({}), r.status_code
    d = r.json()
    if not d or not d.get("item"): return "", 204
    item   = d["item"]
    images = item.get("album",{}).get("images",[])
    return jsonify({"name":item.get("name",""),
                    "artist":", ".join([a["name"] for a in item.get("artists",[])]),
                    "image":images[0]["url"] if images else None,
                    "uri":item.get("uri",""),
                    "progress":d.get("progress_ms",0),
                    "duration":item.get("duration_ms",0),
                    "playing":d.get("is_playing",False)})

@app.route("/spotify/playlists")
def spotify_playlists():
    h = spotify_headers()
    if not h: return jsonify([])
    try:
        r = requests.get("https://api.spotify.com/v1/me/playlists", headers=h, params={"limit":20}, timeout=10)
        if r.status_code == 200:
            items = r.json().get("items",[])
            return jsonify([{"id":p["id"],"name":p["name"],"uri":p["uri"],
                             "total":p["tracks"]["total"],
                             "image":p["images"][0]["url"] if p.get("images") else None}
                            for p in items if p])
    except Exception as e:
        print(f"[PLAYLISTS] Error: {e}")
    return jsonify([])

# ── Cámaras ───────────────────────────────────────────────────────────────────
@app.route("/api/camera/proxy")
def camera_proxy():
    """Proxy para cámaras públicas (evita CORS)."""
    url = request.args.get("url","")
    if not url: return "URL requerida", 400
    try:
        r = requests.get(url, timeout=5, stream=True)
        content_type = r.headers.get("Content-Type","image/jpeg")
        return Response(r.content, content_type=content_type)
    except Exception as e:
        return f"Error: {e}", 500

# ── Finanzas ──────────────────────────────────────────────────────────────────
@app.route("/api/financial", methods=["GET"])
def get_financial():
    try:
        rows = db_q("SELECT action, amount, category, note, created_at FROM financial ORDER BY created_at DESC LIMIT 50", fetch="all") or []
        cfg  = get_financial_config()
        data = [{"action":r["action"],"amount":float(r["amount"] or 0),"category":r["category"],"note":r["note"],"date":str(r["created_at"])} for r in rows]
        return jsonify({"transactions":data,"config":cfg})
    except Exception as e:
        print(f"[FINANCIAL] Error: {e}")
        return jsonify({"transactions":[],"config":{"monthly_budget":28051,"auto_debt":300000},"error":str(e)})

# ── Init / Chat ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static","index.html")

@app.route("/api/init", methods=["POST"])
def init_session():
    data            = request.json or {}
    user_said       = data.get("name","").strip()
    user_pin        = data.get("pin","").strip()
    client_location = data.get("client_location")

    if client_location and client_location.get("status") == "success":
        location = {
            "city":    client_location.get("city","?"),
            "region":  client_location.get("regionName",""),
            "country": client_location.get("countryCode",""),
            "lat":     client_location.get("lat",0),
            "lon":     client_location.get("lon",0),
            "tz":      client_location.get("timezone","UTC")
        }
    else:
        client_ip = get_client_ip()
        location  = get_location(client_ip)

    weather = get_weather(location)
    session = (identify_user(user_said, user_pin) if user_said
               else {"user_type":"owner","name":OWNER_NAME,"db_path":OWNER_DB,"permissions":"full"})
    session["weather"]  = weather
    session["location"] = location

    if session["user_type"] == "pin_required":
        return jsonify({"pin_required":True,"user":session["name"]})

    greeting = get_greeting()
    ut, nm   = session["user_type"], session["name"]
    if ut=="owner":         welcome = f"{greeting}, señor. {weather_summary(weather)} ¿En qué le puedo asistir?"
    elif ut=="guest_new":   welcome = f"{greeting}. Soy Vesper, el asistente personal del Sr. Ozhelli. ¿En qué le puedo ayudar?"
    elif ut=="guest_known": welcome = f"{greeting}, {nm}. Bienvenido de nuevo."
    else:                   welcome = f"{greeting}. ¿En qué le puedo ayudar?"

    _session_cache[nm] = session
    save_message("assistant", welcome, "owner" if ut=="owner" else "guest")
    return jsonify({"welcome":welcome,"user":nm,"user_type":ut,"weather":weather,
                    "location":location,"spotify_connected":bool(get_spotify_token())})

@app.route("/api/chat", methods=["POST"])
def chat():
    data         = request.json or {}
    user_message = data.get("message","").strip()
    user_name    = data.get("user", OWNER_NAME)
    image_base64 = data.get("image_base64")
    image_type   = data.get("image_type","image/jpeg")

    if not user_message and not image_base64:
        return jsonify({"error":"Mensaje vacío"}), 400

    session = _session_cache.get(user_name)
    if not session:
        location = get_location(); weather = get_weather(location)
        session  = {"user_type":"owner","name":OWNER_NAME,"db_path":OWNER_DB,
                    "permissions":"full","weather":weather,"location":location}
        _session_cache[OWNER_NAME] = session
    try:
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        resp, music_action = loop.run_until_complete(
            orchestrate(user_message, session, image_base64, image_type))
        loop.close()
        return jsonify({"response":resp,"user":session["name"],"music_action":music_action})
    except Exception as e:
        print(f"[ERROR] {e}"); traceback.print_exc()
        return jsonify({"error":str(e)}), 500

@app.route("/api/reminders", methods=["GET"])
def get_reminders():
    rows = db_q("SELECT id, label, trigger_at, status FROM reminders ORDER BY trigger_at", fetch="all") or []
    return jsonify([{"id":r["id"],"label":r["label"],"at":str(r["trigger_at"]),"status":r["status"]} for r in rows])

@app.route("/api/notifications", methods=["GET"])
def get_notifications():
    notifs = list(_pending_notifications)
    _pending_notifications.clear()
    try:
        rows = db_q("SELECT id,label,type,time FROM notifications WHERE delivered=FALSE ORDER BY created_at", fetch="all") or []
        for r in rows:
            notifs.append({"id":r["id"],"label":r["label"],"type":r["type"],"time":str(r["time"])})
            db_q("UPDATE notifications SET delivered=TRUE WHERE id=%s", (r["id"],))
    except: pass
    return jsonify(notifs)

@app.route("/api/outfit-search")
def outfit_search():
    query       = request.args.get("q","smart casual men outfit earth tones")
    images      = []
    serpapi_key = os.environ.get("SERPAPI_KEY","")
    pixabay_key = os.environ.get("PIXABAY_KEY","")

    if serpapi_key:
        try:
            r = requests.get("https://serpapi.com/search",
                params={"engine":"google_images","q":query,"api_key":serpapi_key,"num":6,"safe":"active"},
                timeout=10)
            if r.status_code == 200:
                for item in r.json().get("images_results",[])[:4]:
                    url = item.get("original") or item.get("thumbnail","")
                    if url and url.startswith("http"):
                        images.append({"url":url,"title":item.get("title","")})
        except Exception as e:
            print(f"[OUTFITS] SerpApi: {e}")

    if pixabay_key and not images:
        try:
            r = requests.get("https://pixabay.com/api/",
                params={"key":pixabay_key,"q":query.replace(" ","+"),
                        "image_type":"photo","category":"fashion",
                        "per_page":6,"safesearch":"true","orientation":"vertical"},
                timeout=10)
            if r.status_code == 200:
                for hit in r.json().get("hits",[])[:4]:
                    if hit.get("webformatURL"):
                        images.append({"url":hit["webformatURL"],"title":hit.get("tags","")})
        except Exception as e:
            print(f"[OUTFITS] Pixabay: {e}")

    if not images:
        try:
            import random
            queries = ["men fashion smart casual","men style earth tones","men minimalist fashion","men italian casual"]
            r = requests.get("https://api.unsplash.com/search/photos",
                params={"query":random.choice(queries),"per_page":4,"orientation":"portrait"},
                headers={"Authorization":"Client-ID 9c2bef3b7f8c4e5d6a1b0f2e3d4c5b6a"},
                timeout=10)
            if r.status_code == 200:
                for photo in r.json().get("results",[])[:4]:
                    url = photo.get("urls",{}).get("small","")
                    if url: images.append({"url":url,"title":photo.get("alt_description","")})
        except Exception as e:
            print(f"[OUTFITS] Unsplash: {e}")

    print(f"[OUTFITS] {len(images)} imgs para '{query}'")
    return jsonify({"images":images,"query":query})

# ── Webhook Microsoft (Outlook + Teams) ──────────────────────────────────────
RELEVANCE_RULES = {
    "high_importance": ["urgent","urgente","asap","importante","critical","crítico","action required"],
    "cemex_keywords":  ["cemex","servicenow","snowflake","sap","replica","replication","vendor",
                        "incident","incidente","outage","deployment","prod","production"],
    "personal":        ["ozhelli","luis","diaz","tavares"],
    "meetings":        ["meeting","reunión","reunion","junta","invite","invitación","calendar"],
}

def relevance_filter(data):
    """Determina si un mensaje/correo merece notificar a Vesper."""
    text = f"{data.get('subject','')} {data.get('preview','')} {data.get('content','')}".lower()
    score = 0
    reasons = []

    # Importancia alta marcada por el remitente
    if str(data.get("importance","")).lower() in ["high","alta"]:
        score += 3
        reasons.append("marcado como importante")

    # Keywords de alta prioridad
    for kw in RELEVANCE_RULES["high_importance"]:
        if kw in text:
            score += 2
            reasons.append(f"contiene '{kw}'")
            break

    # Keywords de CEMEX/trabajo
    for kw in RELEVANCE_RULES["cemex_keywords"]:
        if kw in text:
            score += 1
            reasons.append(f"relacionado con {kw}")
            break

    # Menciona al usuario directamente
    for kw in RELEVANCE_RULES["personal"]:
        if kw in text:
            score += 2
            reasons.append("te menciona directamente")
            break

    # Invitaciones a reuniones
    for kw in RELEVANCE_RULES["meetings"]:
        if kw in text:
            score += 1
            reasons.append("invitación a reunión")
            break

    is_relevant = score >= 2
    return is_relevant, score, reasons

async def generate_smart_notification(data, source, reasons):
    """Genera notificación inteligente con contexto de memoria."""
    from_name = data.get("from_name") or data.get("from","Desconocido")
    subject   = data.get("subject","Sin asunto")
    preview   = data.get("preview","")[:300]

    # Buscar contexto de la persona en memoria
    memory_context = ""
    try:
        rows = db_q("""SELECT value FROM memories
                       WHERE LOWER(value) LIKE %s OR LOWER(value) LIKE %s
                       LIMIT 3""",
                   (f"%{from_name.split()[0].lower()}%",
                    f"%{from_name.split()[-1].lower()}%"),
                   fetch="all")
        if rows:
            memory_context = " | ".join([r["value"] for r in rows])
    except: pass

    result = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system="Genera una notificación concisa (max 2 oraciones) para el asistente personal. Incluye quién es la persona si tienes contexto, de qué trata y si requiere acción.",
        messages=[{"role":"user","content": f"Fuente: {source}. De: {from_name}. Asunto: {subject}. Preview: {preview}. Contexto: {memory_context}. Razones: {chr(44).join(reasons)}"}]
    )
    return result.content[0].text.strip()

@app.route("/api/webhook/microsoft", methods=["POST"])
def webhook_microsoft():
    # Verificar secret
    secret = request.headers.get("X-Vesper-Secret","")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return jsonify({"error":"Unauthorized"}), 401

    data   = request.json or {}
    source = data.get("source","outlook")
    print(f"[WEBHOOK] {source} de {data.get('from_name','?')}: {data.get('subject','?')[:50]}")

    # Relevance filter
    is_relevant, score, reasons = relevance_filter(data)
    print(f"[WEBHOOK] Relevancia: {score} — {'NOTIFICAR' if is_relevant else 'ignorar'}")

    if not is_relevant:
        return jsonify({"status":"filtered","score":score}), 200

    # Generar notificación inteligente
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        notification = loop.run_until_complete(
            generate_smart_notification(data, source, reasons)
        )
        loop.close()
    except Exception as e:
        from_name = data.get("from_name") or data.get("from","?")
        subject   = data.get("subject","Sin asunto")
        notification = f"{source.upper()}: {from_name} — {subject}"

    # Determinar emoji por fuente
    emoji = {"outlook":"📧","teams":"💬","calendar":"📅"}.get(source,"🔔")
    label = f"{emoji} {notification}"

    # Guardar en notificaciones
    db_q("INSERT INTO notifications (label, type, time) VALUES (%s, %s, NOW()::text)",
         (label, f"webhook_{source}"))
    _pending_notifications.append({"label":label,"type":f"webhook_{source}"})

    # Guardar en memoria si hay info nueva de la persona
    from_name = data.get("from_name","")
    if from_name:
        try:
            db_q("""INSERT INTO memories (category, key, value, updated_at)
                    VALUES ('relaciones', %s, %s, NOW())
                    ON CONFLICT (category, key)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                 (f"ultimo_contacto_{from_name.replace(' ','_').lower()[:30]}",
                  f"Último contacto via {source}: {data.get('subject','?')[:100]} — {datetime.now().strftime('%Y-%m-%d')}"))
        except: pass

    return jsonify({"status":"notified","score":score,"reasons":reasons}), 200

@app.route("/api/webhook/microsoft", methods=["GET"])
def webhook_microsoft_verify():
    """Verificación de endpoint para Power Automate."""
    return jsonify({"status":"ok","service":"vesper"}), 200

@app.route("/api/health")
def health():
    return jsonify({"status":"ok","ts":datetime.now().isoformat(),
                    "spotify":bool(get_spotify_token())})

# ── Keep-alive ────────────────────────────────────────────────────────────────
def keep_alive():
    """Optional Render keep-alive. Disable with ENABLE_KEEP_ALIVE=false.

    This is intentionally configurable because platform sleep policies can change.
    """
    if os.environ.get("ENABLE_KEEP_ALIVE", "false").lower() != "true":
        print("[KEEP-ALIVE] Disabled")
        return
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        print("[KEEP-ALIVE] Disabled: RENDER_EXTERNAL_URL not available")
        return
    interval = max(60, int(os.environ.get("KEEP_ALIVE_SECONDS", "840")))
    time.sleep(60)
    while True:
        try:
            r = requests.get(f"{url}/api/health", timeout=10)
            print(f"[KEEP-ALIVE] {r.status_code} {datetime.now().strftime('%H:%M')}")
        except Exception as e:
            print(f"[KEEP-ALIVE] Error: {e}")
        time.sleep(interval)

# ── Scheduler ────────────────────────────────────────────────────────────────
def reminder_scheduler():
    time.sleep(10)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            now_mty = datetime.now(timezone(timedelta(hours=-6)))
            # Recordatorios
            rows  = db_q("SELECT id, label, trigger_at FROM reminders WHERE status='pending'", fetch="all") or []
            fired = []
            for r in rows:
                rid, label, trigger_at = r["id"], r["label"], str(r["trigger_at"])
                try:
                    parts    = trigger_at.strip().rsplit(" ",1)
                    dt_str   = parts[0]
                    offset_s = parts[1] if len(parts)>1 else "-0600"
                    sign     = 1 if offset_s[0]=="+" else -1
                    oh       = int(offset_s[1:3])
                    om       = int(offset_s[3:5]) if len(offset_s)>=5 else 0
                    tz_off   = timezone(timedelta(hours=sign*oh, minutes=sign*om))
                    dt_local = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=tz_off)
                    if dt_local <= now_utc: fired.append((rid,label))
                except:
                    if trigger_at[:16] <= now_mty.strftime("%Y-%m-%d %H:%M"):
                        fired.append((rid,label))

            for rid, label in fired:
                db_q("UPDATE reminders SET status='notified' WHERE id=%s", (rid,))
                db_q("INSERT INTO notifications (label,type,time) VALUES (%s,%s,%s)",
                          (label,"reminder",now_utc.strftime("%Y-%m-%d %H:%M UTC")))
                _pending_notifications.append({"id":rid,"label":label,"type":"reminder","time":str(now_utc)})
                print(f"[REMINDER] {label}")

            # Alarmas — ventana de 90 segundos
            alarms = db_q("SELECT id, time, label FROM alarms WHERE active=TRUE", fetch="all") or []
            for a in alarms:
                aid, alarm_time, alarm_label = a["id"], str(a["time"]), a["label"]
                if not alarm_time: continue
                try:
                    hh, mm   = map(int, alarm_time[:5].split(":"))
                    alarm_dt = now_mty.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    diff     = (now_mty - alarm_dt).total_seconds()
                    if 0 <= diff <= 90:
                        label = alarm_label or f"Alarma {alarm_time}"
                        db_q("UPDATE alarms SET active=FALSE WHERE id=%s", (aid,))
                        db_q("INSERT INTO notifications (label,type,time) VALUES (%s,%s,%s)",
                                  (f"⏰ {label}","alarm",alarm_time))
                        _pending_notifications.append({"id":aid,"label":f"⏰ {label}","type":"alarm","time":alarm_time})
                        print(f"[ALARM] {label}")
                except Exception as ae:
                    print(f"[ALARM] Error: {ae}")

        except Exception as e:
            print(f"[SCHEDULER] Error: {e}")
        time.sleep(20)

def get_relevant_memories(context_hint=""):
    """Recupera memorias relevantes para el contexto actual."""
    try:
        rows     = db_q("SELECT category, key, value, updated_at FROM memories ORDER BY updated_at DESC LIMIT 40", fetch="all") or []
        patterns = db_q("SELECT pattern_type, description, occurrences FROM patterns ORDER BY last_seen DESC LIMIT 10", fetch="all") or []
        today    = datetime.now().strftime("%Y-%m-%d")
        sum_row  = db_q("SELECT summary FROM daily_summaries WHERE date=%s", (today,), fetch="one")

        mem_str = ""
        if rows:
            by_cat = {}
            for r in rows:
                by_cat.setdefault(r["category"], []).append(f"{r['key']}: {r['value']}")
            for cat, items in by_cat.items():
                mem_str += f"\n[{cat.upper()}]\n" + "\n".join(f"  • {i}" for i in items)

        if patterns:
            mem_str += "\n\n[PATRONES DETECTADOS]\n"
            for pt, desc, occ in patterns:
                mem_str += f"  • {pt} ({occ}x): {desc}\n"

        if sum_row:
            mem_str += f"\n\n[RESUMEN DE HOY]\n  {sum_row['summary']}"

        return mem_str
    except Exception as e:
        print(f"[MEMORY] Error leyendo: {e}")
        return ""

async def extract_and_save_memories(conversation_text, session):
    """Extrae información relevante de la conversación y la guarda en memoria."""
    if session.get("user_type") != "owner":
        return
    try:
        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system="""Eres un extractor de memoria para un asistente personal llamado Vesper.
Analiza la conversación y extrae SOLO hechos nuevos o actualizaciones importantes sobre el usuario.
Responde ÚNICAMENTE con JSON válido, sin texto extra.
Formato:
{
  "memories": [
    {"category": "categoria", "key": "clave_snake_case", "value": "valor"},
    ...
  ],
  "patterns": [
    {"type": "tipo", "description": "descripción del patrón detectado"},
    ...
  ]
}
Categorías válidas: personal, trabajo, finanzas, estilo, deportes, proyectos, musica, salud, relaciones, preferencias
Si no hay nada nuevo relevante, responde: {"memories": [], "patterns": []}
NO extraigas información ya conocida o trivial.""",
            messages=[{"role": "user", "content":
                f"Extrae información nueva de esta conversación:\n\n{conversation_text[-2000:]}"}]
        )
        raw  = result.content[0].text.strip()
        raw  = raw.replace("```json","").replace("```","").strip()
        data = json.loads(raw)

        if not data.get("memories") and not data.get("patterns"):
            return

        saved = 0
        for mem in data.get("memories", []):
            if mem.get("category") and mem.get("key") and mem.get("value"):
                db_q("INSERT INTO memories (category,key,value,updated_at) VALUES (%s,%s,%s,NOW()) ON CONFLICT(category,key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                     (mem["category"], mem["key"], str(mem["value"])))
                saved += 1
        for pat in data.get("patterns", []):
            if pat.get("type") and pat.get("description"):
                existing = db_q("SELECT id FROM patterns WHERE pattern_type=%s AND description=%s",
                    (pat["type"], pat["description"]), fetch="one")
                if existing:
                    db_q("UPDATE patterns SET occurrences=occurrences+1, last_seen=NOW() WHERE id=%s", (existing["id"],))
                else:
                    db_q("INSERT INTO patterns (pattern_type, description) VALUES (%s,%s)", (pat["type"], pat["description"]))
        if saved > 0:
            print(f"[MEMORY] {saved} memorias guardadas")
    except Exception as e:
        print(f"[MEMORY] Error extrayendo: {e}")

def generate_daily_summary():
    """Genera resumen del día a las 11pm."""
    while True:
        now = datetime.now(timezone(timedelta(hours=-6)))
        # Correr a las 23:00 hora Monterrey
        target = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if now > target:
            target += timedelta(days=1)
        secs = (target - now).total_seconds()
        time.sleep(secs)
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            rows  = db_q("SELECT role, content FROM context_history WHERE DATE(created_at) = CURRENT_DATE AND user_type='owner' ORDER BY created_at LIMIT 40", fetch="all") or []
            if not rows:
                continue

            convo = "\n".join([f"{r[0]}: {r[1][:100]}" for r in rows])
            result = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=300,
                system="Resume en 2-3 oraciones el día del usuario basándote en sus conversaciones con Vesper. Sé conciso y factual.",
                messages=[{"role":"user","content":f"Conversaciones de hoy:\n{convo}"}]
            )
            summary = result.content[0].text.strip()
            gastos_row = db_q("SELECT SUM(amount) as total FROM financial WHERE DATE(created_at)=CURRENT_DATE AND action='registrar_gasto'", fetch="one")
            gastos = float(gastos_row['total'] or 0) if gastos_row else 0

            db_q("INSERT INTO daily_summaries (date, summary, key_events) VALUES (%s,%s,%s) ON CONFLICT (date) DO UPDATE SET summary=EXCLUDED.summary",
                 (today, summary, f"Gastos: ${gastos:,.0f} MXN"))
            print(f"[MEMORY] Resumen diario guardado: {today}")
        except Exception as e:
            print(f"[MEMORY] Error resumen diario: {e}")

# ── Boot ──────────────────────────────────────────────────────────────────────
# Gunicorn importa este módulo, por lo que el arranque ocurre después de que
# todas las funciones hayan sido definidas. Un solo worker evita duplicar
# schedulers y tareas background.
init_owner_db()
threading.Thread(target=keep_alive, daemon=True).start()
threading.Thread(target=reminder_scheduler, daemon=True).start()
threading.Thread(target=generate_daily_summary, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"[BOOT] Puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
