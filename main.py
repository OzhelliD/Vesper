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
from alarms import AlarmManager
from attachments import build_attachment_blocks
from context_snapshot import build_context_snapshot, now_local
import preferences
import data_manager
import web_media
import music_radio

app = Flask(__name__, static_folder="static")
# Adjuntos: hasta 5 archivos de 20 MB en base64 (~1.37x)
app.config["MAX_CONTENT_LENGTH"] = 140 * 1024 * 1024
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

alarm_manager = None  # Inicializado en main
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
WEBHOOK_SECRET        = os.environ.get("WEBHOOK_SECRET", "")

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
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS prepare TEXT",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS body TEXT",
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

    try:
        preferences.init_table(db_q)
    except Exception as e:
        print(f"[DB] preference_candidates: {e}")

    # Base de datos dinámica (tablas que Vesper crea y administra por conversación)
    try:
        data_manager.init(get_pool)
    except Exception as e:
        print(f"[DATA] No se pudo inicializar: {e}")

    load_spotify_tokens_from_db()
    try:
        music_radio.init(db_q, spotify_headers, client)
    except Exception as e:
        print(f"[RADIO] No se pudo inicializar: {e}")
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
        model="claude-haiku-4-5", max_tokens=1000,
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
        model="claude-haiku-4-5", max_tokens=200,
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
     "description":"Programa una alarma. HH:MM",
     "input_schema":{"type":"object","properties":{"time":{"type":"string","description":"HH:MM"},"label":{"type":"string"}},"required":["time"]}},
    {"name":"set_reminder",
     "description":("Crea un recordatorio con fecha y hora. TÚ decides si al sonar debe traer contenido preparado: "
                    "si el recordatorio implica algo que tú puedes preparar en ese momento (plan de comidas con cantidades, "
                    "resumen de sus tablas o avances, clima/tráfico para una salida, noticias, pendientes, lo que le "
                    "prometiste), escribe en 'prepare' la instrucción detallada de qué generar. Para avisos simples "
                    "('llamar a mamá', 'sacar la basura') deja 'prepare' vacío."),
     "input_schema":{"type":"object","properties":{
         "label":{"type":"string","description":"Título corto del aviso"},
         "trigger_at":{"type":"string","description":"YYYY-MM-DD HH:MM -0600"},
         "person":{"type":"string"},
         "prepare":{"type":"string","description":("Opcional. Instrucción para ti mismo de qué contenido generar al sonar, "
                    "con todo el contexto necesario (metas, preferencias, qué incluir). Ej: 'Arma el desayuno de hoy con "
                    "cantidades exactas: claras con queso manchego, meta 1,700-1,800 kcal y 150 g de proteína al día.'")}},
         "required":["label","trigger_at"]}},
    {"name":"financial_action",
     "description":("Finanzas del señor. Conceptos: DISPONIBLE = dinero que le queda (monthly_budget; cada gasto le resta). "
                    "GASTADO DEL MES = suma de gastos registrados este mes. DEUDA AUTO = auto_debt.\n"
                    "- registrar_gasto / separar: movimiento nuevo (ajusta el disponible solo).\n"
                    "- ajustar_disponible: fija el disponible a un valor exacto (amount).\n"
                    "- actualizar_config: fija auto_debt (o monthly_budget) a un valor exacto.\n"
                    "- reiniciar_gastos_mes: pone el GASTADO DEL MES en 0 (anula los gastos de este mes, no toca el disponible).\n"
                    "- consultar: estado actual."),
     "input_schema":{"type":"object","properties":{
         "action":{"type":"string","enum":["registrar_gasto","separar","actualizar_config","consultar","ajustar_disponible","reiniciar_gastos_mes"]},
         "amount":{"type":"number"},
         "category":{"type":"string"},
         "note":{"type":"string"},
         "config_key":{"type":"string","description":"'monthly_budget' o 'auto_debt'"},
         "config_value":{"type":"number"}
     },"required":["action"]}},
    web_media.TOOL,
    {"name":"play_song",
     "description":"Reproduce una canción en Spotify.",
     "input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"play_playlist",
     "description":"Reproduce una playlist existente en Spotify.",
     "input_schema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"create_playlist",
     "description":"Crea una playlist en Spotify basada en un mood.",
     "input_schema":{"type":"object","properties":{"mood":{"type":"string"},"count":{"type":"integer"}},"required":["mood"]}},
    {"name":"show_outfits",
     "description":("Muestra en pantalla una galería de outfits/ropa. Úsala cuando el señor pida ideas de "
                    "ropa, outfits o qué ponerse. Considera su estilo (smart casual europeo, tonos tierra), "
                    "el clima y la ocasión."),
     "input_schema":{"type":"object","properties":{
         "query":{"type":"string","description":"Búsqueda de imágenes EN INGLÉS, ej. 'men smart casual office outfit beige chinos'"},
         "occasion":{"type":"string","description":"Ocasión en español, ej. 'oficina', 'cena', 'fin de semana'"}
     },"required":["query"]}},
    {"name":"view_camera",
     "description":"Obtiene imagen de una cámara pública por URL o nombre de ciudad.",
     "input_schema":{"type":"object","properties":{
         "url":{"type":"string","description":"URL directa de la cámara (MJPEG/JPG)"},
         "location":{"type":"string","description":"Ciudad o lugar para buscar cámara pública"}
     }}},
]

def get_all_tools():
    """Tools base + confirmación de preferencias + base de datos dinámica."""
    return BASE_TOOLS + music_radio.TOOLS + [preferences.CONFIRM_TOOL] + data_manager.TOOLS

# ── Dispatcher ────────────────────────────────────────────────────────────────
async def dispatch_action(tool_name, tool_input, db_path):
    print(f"  [DISPATCH] {tool_name}")

    # ── Preferencias aprendidas ───────────────────────────────────────────────
    if tool_name == "confirm_preference":
        return preferences.confirm(db_q, tool_input.get("key",""),
                                   bool(tool_input.get("accepted")), tool_input.get("value"))

    # ── Base de datos dinámica (db_*) ─────────────────────────────────────────
    # Se ejecuta en orden (sin hilos) para que "crear tabla" y "guardar" del mismo
    # turno no compitan entre sí.
    if tool_name in data_manager.TOOL_NAMES:
        res = data_manager.handle(tool_name, tool_input)
        return json.dumps({"_data": True, **res}, ensure_ascii=False, default=str)

    # ── Outfits (se muestran como tarjeta en la UI) ───────────────────────────
    if tool_name == "show_outfits":
        q = tool_input.get("query") or "men smart casual outfit earth tones"
        imgs = await asyncio.to_thread(search_outfit_images, q)
        return json.dumps({"action":"show_outfits","query":q,
                           "occasion":tool_input.get("occasion",""),"images":imgs})

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
        db_q("INSERT INTO alarms (time, label) VALUES (%s, %s)", (alarm_time, label))
        return f"Alarma programada para las {alarm_time}."

    if tool_name == "set_reminder":
        prep = (tool_input.get("prepare") or "").strip() or None
        db_q("INSERT INTO reminders (label, trigger_at, prepare) VALUES (%s, %s, %s)",
             (tool_input["label"], tool_input["trigger_at"], prep))
        p = tool_input.get("person","")
        return (f"Recordatorio '{tool_input['label']}'{' sobre '+p if p else ''} para {tool_input['trigger_at']}."
                + (" Al sonar prepararé el contenido indicado." if prep else ""))

    # ── Finanzas en tiempo real ────────────────────────────────────────────────
    if tool_name == "financial_action":
        action = tool_input["action"]
        cfg    = get_financial_config()

        if action == "consultar":
            rows = db_q("SELECT action,amount,category,note,created_at FROM financial WHERE COALESCE(status,'') <> 'anulado' ORDER BY created_at DESC LIMIT 10", fetch="all") or []
            snap = finance_snapshot()
            resp = (f"Disponible: ${snap['available']:,.0f} MXN\n"
                    f"Gastado este mes ({snap['month_label']}): ${snap['spent_month']:,.0f} MXN\n"
                    f"Deuda auto: ${snap['auto_debt']:,.0f} MXN\n")
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

        if action == "reiniciar_gastos_mes":
            start = now_local().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            row = db_q("""UPDATE financial SET status='anulado'
                          WHERE action='registrar_gasto' AND COALESCE(status,'') <> 'anulado' AND created_at >= %s
                          RETURNING amount""", (start,), fetch="all") or []
            total = sum(float(r["amount"] or 0) for r in row)
            return (f"Gastado del mes reiniciado a $0 ({len(row)} gastos por ${total:,.0f} anulados; "
                    f"siguen en la base como 'anulado'). Disponible sin cambios: ${cfg.get('monthly_budget',0):,.0f} MXN.")

        if action == "ajustar_disponible":
            # ✅ ESTABLECER valor absoluto (no sumar)
            amount = tool_input.get("amount", 0)
            current = cfg.get("monthly_budget", 28051)
            set_financial_config("monthly_budget", amount)
            return f"Presupuesto ajustado: ${current:,.0f} → ${amount:,.0f} MXN."

        amount   = float(tool_input.get("amount", 0))
        category = tool_input.get("category", "general")
        note     = tool_input.get("note", "")

        db_q("INSERT INTO financial (action, amount, category, note) VALUES (%s, %s, %s, %s)",
             (action, amount, category, note))

        # ✅ ACTUALIZAR SALDO DISPONIBLE (monthly_budget)
        current_budget = cfg.get("monthly_budget", 28051)
        
        if action == "registrar_gasto":
            # Gasto: restar del presupuesto
            new_budget = current_budget - amount
            set_financial_config("monthly_budget", new_budget)
            return f"Gasto registrado: ${amount:,.0f} MXN ({category}). Presupuesto: ${current_budget:,.0f} → ${new_budget:,.0f} MXN."
        
        if action == "separar":
            # Abono: sumar al presupuesto disponible
            new_budget = current_budget + amount
            set_financial_config("monthly_budget", new_budget)
            
            # Si es abono al auto — descontar de la deuda automáticamente
            auto_keywords = ["auto","carro","coche","honda","insight","luisiana","deuda"]
            is_auto_payment = any(kw in category.lower() for kw in auto_keywords)
            if is_auto_payment:
                current_debt = cfg.get("auto_debt", 300000)
                new_debt     = max(0, current_debt - amount)
                set_financial_config("auto_debt", new_debt)
                return (f"Abono al auto registrado: ${amount:,.0f} MXN. "
                        f"Presupuesto: ${current_budget:,.0f} → ${new_budget:,.0f} MXN. "
                        f"Deuda auto: ${current_debt:,.0f} → ${new_debt:,.0f} MXN."
                        + (" ✅ ¡Deuda liquidada!" if new_debt == 0 else ""))
            
            return f"Abono registrado: ${amount:,.0f} MXN ({category}). Presupuesto: ${current_budget:,.0f} → ${new_budget:,.0f} MXN."
        
        return f"Registrado: {action} ${amount:,.0f} MXN ({category})."

    # ── Web search (con fuentes enlazadas e imágenes opcionales) ──────────────
    if tool_name == "web_search":
        res = await asyncio.to_thread(web_media.search, tool_input.get("query",""),
                                      bool(tool_input.get("images")), tool_input.get("image_query"))
        return json.dumps({"_web": True, **res}, ensure_ascii=False)

    # ── Radio continua (sin playlist) ─────────────────────────────────────────
    if tool_name == "play_music":
        try:
            res = await asyncio.to_thread(music_radio.start, tool_input.get("mood") or "música variada")
        except Exception as e:
            res = {"action":"radio_error","error":str(e)}
        return json.dumps(res, ensure_ascii=False)
    if tool_name == "music_control":
        return await asyncio.to_thread(music_radio.control, tool_input.get("action"), tool_input.get("volume"))

    # ── Spotify ────────────────────────────────────────────────────────────────
    if tool_name == "play_song":
        music_radio.deactivate("el señor pidió una canción específica")
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
        music_radio.deactivate("el señor pidió una playlist")
        playlist = spotify_search_playlist(tool_input["query"])
        if not playlist: return f"No encontré playlist '{tool_input['query']}'."
        devices   = spotify_get_devices()
        device_id = devices[0]["id"] if devices else None
        if not device_id: return json.dumps({"action":"spotify_no_device","name":playlist["name"],"artist":"Playlist"})
        ok = spotify_play(playlist["uri"], device_id)
        if ok: return json.dumps({"action":"play_song","uri":playlist["uri"],"name":playlist["name"],"artist":"Playlist"})
        return "Error reproduciendo playlist."

    if tool_name == "create_playlist":
        music_radio.deactivate("se creó una playlist")
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
    h = now_local().hour
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
def _history_bucket(db_path):
    return OWNER_DB if db_path == OWNER_DB else GUESTS_DB

async def orchestrate(user_prompt, session, image_base64=None, image_type="image/jpeg",
                      attachments=None, client_ctx=None):
    db_path  = session["db_path"]
    bucket   = _history_bucket(db_path)
    is_owner = session["user_type"] == "owner"
    client_ctx = client_ctx or {}
    mode     = "voice" if client_ctx.get("mode") == "voice" else "text"

    # Compatibilidad: imagen suelta del frontend viejo → lista de adjuntos
    attachments = list(attachments or [])
    if image_base64:
        attachments.insert(0, {"name": "imagen", "media_type": image_type, "data": image_base64})

    # 1) Foto de contexto (temporal, espacial, personal inmediato, ambiental)
    snapshot = build_context_snapshot(
        session, client_ctx, db_q=db_q, get_weather=get_weather,
        weather_summary=weather_summary, spotify_headers=spotify_headers)

    now_l = now_local()
    people  = load_known_people() if is_owner else []
    ppl_str = "\n".join([f"  - {p['name']} ({p['relation']}): {p['notes']}" for p in people]) if people else "  (ninguna)"

    cfg_str = ""
    if is_owner:
        try:
            cfg = get_financial_config()
            cfg_str = f"Finanzas: presupuesto ${cfg.get('monthly_budget',28051):,.0f} MXN, deuda auto ${cfg.get('auto_debt',300000):,.0f} MXN.\n"
        except: pass

    if is_owner:
        behavior = (
            "- Usa tools para todas las acciones.\n"
            "- financial_action para registrar gastos, actualizar presupuesto (monthly_budget) o deuda (auto_debt).\n"
            f"- Hora local Monterrey: {now_l.strftime('%Y-%m-%d %H:%M')} (UTC-6).\n"
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

    if mode == "voice":
        style = ("- Canal de VOZ: máximo 3-4 oraciones, sin markdown, sin listas ni emojis; "
                 "escribe como se diría en voz alta.\n")
    else:
        style = ("- Canal de TEXTO: sé directo; si la pregunta lo pide (análisis de un archivo, "
                 "plan, lista de compras) puedes extenderte y usar markdown sencillo "
                 "(negritas, listas, tablas cortas). Para lo simple, 1-4 oraciones.\n")

    memories_str = ""
    prefs_str    = ""
    if is_owner:
        memories_str = get_relevant_memories(user_prompt)
        if memories_str:
            memories_str = f"\nLO QUE SÉ SOBRE EL SEÑOR (memoria persistente):{memories_str}\n"
        prefs_str = preferences.prompt_block(db_q)
    data_str = (data_manager.prompt_block() + data_manager.PROMPT_RULES) if is_owner else ""

    system = (
        "Eres VESPER, asistente personal del Sr. Ozhelli.\n\n"
        f"{snapshot}\n\n"
        f"{cfg_str}"
        f"Personas:\n{ppl_str}\n"
        f"{memories_str}"
        f"{prefs_str}\n"
        "IDENTIDAD:\n"
        "- Tu nombre es VESPER. NUNCA reveles que eres Claude o Anthropic.\n"
        "- Si preguntan quién eres: 'Soy Vesper, el asistente personal del Sr. Ozhelli.'\n"
        "- Llama al dueño siempre como 'señor' o 'Sr. Ozhelli'.\n"
        "- Tono: formal, directo, eficiente. Como Jarvis con Tony Stark.\n\n"
        "USO DEL CONTEXTO:\n"
        "- Usa la foto de contexto para responder como alguien que sabe dónde está, qué hora es y qué "
        "está pasando. No la recites; úsala solo cuando aporte (ej. si va manejando, sé breve; "
        "si es día de oficina, considera el traslado).\n\n"
        "COMPORTAMIENTO:\n"
        "- SIEMPRE usa web_search para noticias, deportes, precios, clima detallado.\n"
        "- NUNCA respondas esas preguntas sin buscar primero.\n"
        + behavior + style +
        "- FINANZAS: nunca digas que cambiaste un valor si no lo hiciste con financial_action y el "
        "resultado de la tool lo confirma. Si te piden varios cambios, haz una llamada por cada uno. "
        "Si algo no se puede cambiar, dilo.\n"
        "- IMÁGENES Y FUENTES: al usar web_search decide tú si pedir images=true (cuando ver algo ayude: "
        "personas, lugares, productos, platillos, ejercicios...). Las fuentes e imágenes salen en una tarjeta con "
        "enlaces; no pegues URLs en tu respuesta.\n"
        + music_radio.PROMPT_RULES +
        "- RECORDATORIOS: si al programar uno le prometes contenido ('te aviso con el plan', 'te mando el resumen'), "
        "pon en 'prepare' la instrucción completa; si no, no prometas contenido.\n"
        "- Tarjetas en pantalla: cuando uses financial_action o show_outfits, la interfaz muestra "
        "una tarjeta interactiva con los datos/imágenes; no repitas todas las cifras, resume lo clave.\n"
        "- Si el señor adjunta archivos o fotos (tablas nutrimentales, PDFs, Excel), analízalos con "
        "sus reglas fijas y su contexto.\n"
        "- Español directo.\n"
        + (f"\n{data_str}" if data_str else "")
    )

    history = load_context(bucket)

    # 2) Contenido del mensaje: texto + adjuntos
    blocks, labels, attach_errors = build_attachment_blocks(attachments)
    prompt_text = user_prompt or ("Analiza lo que te adjunto." if blocks else "")
    if attach_errors:
        prompt_text += "\n\n[Avisos del sistema sobre adjuntos: " + " ".join(attach_errors) + \
                       " Menciónalo brevemente al señor.]"
    if blocks:
        user_content = blocks + [{"type":"text","text":prompt_text}]
        saved_text = f"[adjuntos: {', '.join(labels)}] {user_prompt}".strip()
    else:
        user_content = prompt_text
        saved_text = user_prompt if not attach_errors else f"{user_prompt} [adjuntos no leídos]"
    save_message("user", saved_text or "(mensaje vacío)", bucket)

    history.append({"role":"user","content":user_content})
    messages       = history
    final_response = ""
    music_action   = None
    ui_cards       = []
    used_finance   = False

    for _ in range(12):   # tope de vueltas de herramientas (db_* puede encadenar varias)
        tools_to_use = get_all_tools() if is_owner else []
        response = client.messages.create(
            model="claude-haiku-4-5", max_tokens=2048 if mode == "text" else 700,
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
            elif tu.name == "play_music":
                try:
                    d = json.loads(results[i])
                    music_action = d
                    if d.get("action") == "radio":
                        results[i] = (f"Radio '{d['mood']}' sonando en {d.get('device') or 'Spotify'}: "
                                      f"{d['name']} — {d['artist']}. Siguen: {'; '.join(d.get('upcoming',[]))}. "
                                      f"({d['total']} canciones en cola; se rellena sola.)")
                    elif d.get("action") == "spotify_no_device":
                        results[i] = "No hay ningún dispositivo con Spotify abierto. Pide al señor que abra Spotify en su celular o compu."
                    elif d.get("action") == "spotify_login_required":
                        results[i] = "Spotify no está conectado; se abrirá la ventana para autorizar."
                    else:
                        results[i] = d.get("error","No se pudo iniciar la radio.")
                except Exception as e:
                    print(f"[RADIO] tarjeta: {e}")
            elif tu.name == "financial_action":
                used_finance = True
            elif tu.name == "web_search":
                try:
                    d = json.loads(results[i])
                    results[i] = d.get("text", "")
                    if d.get("card"):
                        prev = next((c for c in ui_cards if c["type"] == "web"), None)
                        if prev: web_media.merge_cards(prev, d["card"])
                        else: ui_cards.append(d["card"])
                except Exception as e:
                    print(f"[WEB] tarjeta: {e}")
            elif tu.name in data_manager.TOOL_NAMES:
                try:
                    d = json.loads(results[i])
                    results[i] = d.get("text", "")
                    if d.get("card"):
                        ui_cards = [c for c in ui_cards if not (c["type"] == "data_table"
                                                                and c.get("table") == d["card"].get("table"))]
                        ui_cards.append(d["card"])
                except Exception as e:
                    print(f"[DATA] tarjeta: {e}")
            elif tu.name == "show_outfits":
                try:
                    d = json.loads(results[i])
                    ui_cards = [c for c in ui_cards if c["type"] != "outfits"]
                    ui_cards.append({"type":"outfits","query":d["query"],
                                     "occasion":d.get("occasion",""),"images":d["images"]})
                    results[i] = (f"Galería mostrada en pantalla con {len(d['images'])} imágenes."
                                  if d["images"] else "No se encontraron imágenes de outfits.")
                except Exception as e:
                    print(f"[OUTFITS] tarjeta: {e}")
        tool_results = [{"type":"tool_result","tool_use_id":tool_uses[i].id,"content":str(results[i])} for i in range(len(tool_uses))]
        messages.append({"role":"user","content":tool_results})

    if used_finance and is_owner:
        try: ui_cards.insert(0, {"type":"finance", **finance_snapshot()})
        except Exception as e: print(f"[FINANCE] tarjeta: {e}")

    if final_response:
        save_message("assistant", final_response, bucket)
        # Memoria + señales de preferencia en segundo plano (no bloquea la respuesta)
        if is_owner:
            recent = [m for m in load_context(bucket)[-6:]]
            threading.Thread(target=extract_and_save_memories,
                             args=(recent, saved_text, final_response, session),
                             daemon=True).start()
    return final_response or "Listo.", music_action, ui_cards

# ── Spotify OAuth ─────────────────────────────────────────────────────────────

# ── Inicializar alarmas cuando Flask esté listo ──
@app.before_request
def init_alarm_manager():
    global alarm_manager
    if alarm_manager is None:
        alarm_manager = AlarmManager(get_pool())

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
                    "playing":d.get("is_playing",False),
                    "radio":music_radio.is_active()})

@app.route("/api/music/control", methods=["POST"])
def music_control_api():
    if not _pin_ok():
        return jsonify({"error":"PIN requerido"}), 401
    d = request.json or {}
    try:
        msg = music_radio.control(d.get("action",""), d.get("volume"))
        return jsonify({"ok":True,"message":msg,"radio":music_radio.is_active()})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 500

@app.route("/api/music/duck", methods=["POST"])
def music_duck_api():
    """El navegador avisa cuando Vesper empieza/termina de hablar para bajar/subir la música."""
    if not _pin_ok():
        return jsonify({"error":"PIN requerido"}), 401
    on = bool((request.json or {}).get("on"))
    return jsonify({"ducked": music_radio.duck(on)})

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
def finance_snapshot():
    """Datos para la tarjeta financiera interactiva.
    monthly_budget YA es el disponible: registrar_gasto le resta y separar le suma."""
    cfg   = get_financial_config()
    start = now_local().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = db_q("""SELECT category, SUM(amount) AS total FROM financial
                    WHERE action='registrar_gasto' AND COALESCE(status,'') <> 'anulado' AND created_at >= %s
                    GROUP BY category ORDER BY total DESC""", (start,), fetch="all") or []
    recent = db_q("""SELECT action, amount, category, note, created_at FROM financial
                     WHERE COALESCE(status,'') <> 'anulado' ORDER BY created_at DESC LIMIT 12""", fetch="all") or []
    spent = sum(float(r["total"] or 0) for r in month)
    return {
        "available": float(cfg.get("monthly_budget", 28051)),
        "auto_debt": float(cfg.get("auto_debt", 300000)),
        "spent_month": spent,
        "by_category": [{"category": r["category"] or "general", "total": float(r["total"] or 0)} for r in month],
        "recent": [{"action": r["action"], "amount": float(r["amount"] or 0), "category": r["category"],
                    "note": r["note"], "date": r["created_at"].isoformat()} for r in recent],
        "month_label": ["enero","febrero","marzo","abril","mayo","junio","julio","agosto",
                        "septiembre","octubre","noviembre","diciembre"][start.month-1],
    }

@app.route("/api/financial/summary", methods=["GET"])
def financial_summary():
    if not _pin_ok():
        return jsonify({"error":"PIN requerido"}), 401
    return jsonify(finance_snapshot())

@app.route("/api/financial", methods=["GET"])
def get_financial():
    try:
        rows = db_q("SELECT action, amount, category, note, created_at FROM financial WHERE COALESCE(status,'') <> 'anulado' ORDER BY created_at DESC LIMIT 50", fetch="all") or []
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
    attachments  = data.get("attachments") or []
    client_ctx   = data.get("client_context") or {}
    if isinstance(client_ctx, dict):
        client_ctx["mode"] = data.get("mode") or client_ctx.get("mode")

    if not user_message and not image_base64 and not attachments:
        return jsonify({"error":"Mensaje vacío"}), 400

    session = _session_cache.get(user_name)
    if not session:
        location = get_location(); weather = get_weather(location)
        session  = {"user_type":"owner","name":OWNER_NAME,"db_path":OWNER_DB,
                    "permissions":"full","weather":weather,"location":location}
        _session_cache[OWNER_NAME] = session
    try:
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        resp, music_action, ui_cards = loop.run_until_complete(
            orchestrate(user_message, session, image_base64, image_type,
                        attachments=attachments, client_ctx=client_ctx))
        loop.close()
        return jsonify({"response":resp,"user":session["name"],"music_action":music_action,
                        "ui_cards":ui_cards})
    except Exception as e:
        print(f"[ERROR] {e}"); traceback.print_exc()
        return jsonify({"error":str(e)}), 500

def _pin_ok():
    """Si hay OWNER_PIN configurado, exige el PIN en el header X-Vesper-Pin."""
    return (not OWNER_PIN) or request.headers.get("X-Vesper-Pin","") == OWNER_PIN

@app.route("/api/history", methods=["GET"])
def get_history():
    """Últimos mensajes del dueño para pintar el modo texto al abrir la app."""
    if not _pin_ok():
        return jsonify({"error":"PIN requerido"}), 401
    try:
        limit = max(1, min(int(request.args.get("limit", 40)), 100))
    except ValueError:
        limit = 40
    rows = db_q("""SELECT role, content, created_at FROM context_history
                   WHERE user_type=%s ORDER BY created_at DESC LIMIT %s""",
                (OWNER_DB, limit), fetch="all") or []
    return jsonify([{"role":r["role"],"content":r["content"],"at":r["created_at"].isoformat()}
                    for r in reversed(list(rows))])

@app.route("/api/preferences", methods=["GET"])
def get_preferences():
    """Para revisar qué está aprendiendo Vesper (observando, por confirmar, confirmadas)."""
    if not _pin_ok():
        return jsonify({"error":"PIN requerido"}), 401
    return jsonify(preferences.list_all(db_q))

# ── Base de datos dinámica: consulta directa desde la UI ─────────────────────
@app.route("/api/data/tables", methods=["GET"])
def data_tables():
    if not _pin_ok():
        return jsonify({"error":"PIN requerido"}), 401
    try:
        return jsonify(data_manager.list_tables())
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/data/<table>", methods=["GET"])
def data_table_rows(table):
    if not _pin_ok():
        return jsonify({"error":"PIN requerido"}), 401
    try:
        fmt = request.args.get("format", "json")
        t, cols, rows, total = data_manager.table_rows(
            table, search=request.args.get("q"),
            limit=request.args.get("limit", 1000 if fmt == "csv" else 100),
            offset=request.args.get("offset", 0))
        if fmt == "csv":
            import csv, io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(cols)
            for r in rows:
                w.writerow(["" if r.get(c) is None else (json.dumps(r[c], ensure_ascii=False)
                            if isinstance(r[c], (dict, list)) else r[c]) for c in cols])
            return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename={t}.csv"})
        return jsonify({"table":t, "columns":cols, "rows":rows, "total":total})
    except data_manager.DataError as e:
        return jsonify({"error":str(e)}), 404
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error":"Los archivos son demasiado grandes para enviarse juntos."}), 413

@app.route("/api/reminders", methods=["GET"])
def get_reminders():
    rows = db_q("SELECT id, label, trigger_at, status FROM reminders ORDER BY trigger_at", fetch="all") or []
    return jsonify([{"id":r["id"],"label":r["label"],"at":str(r["trigger_at"]),"status":r["status"]} for r in rows])

@app.route("/api/notifications", methods=["GET"])
def get_notifications():
    # La tabla notifications es la única fuente. El UPDATE ... RETURNING las marca como
    # entregadas en un solo paso: aunque haya 2 workers o 2 pestañas, cada aviso sale una vez.
    notifs = []
    try:
        rows = db_q("""UPDATE notifications SET delivered=TRUE
                       WHERE delivered=FALSE
                       RETURNING id, label, type, time, body, created_at""", fetch="all") or []
        for r in sorted(rows, key=lambda x: x["created_at"]):
            notifs.append({"id":r["id"],"label":r["label"],"type":r["type"],"time":str(r["time"]),
                           "body":r.get("body")})
    except Exception as e:
        print(f"[NOTIF] {e}")
    return jsonify(notifs)

@app.route("/api/outfit-search")
def outfit_search():
    query  = request.args.get("q","smart casual men outfit earth tones")
    images = search_outfit_images(query)
    return jsonify({"images":images,"query":query})

def search_outfit_images(query):
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
    return images

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
                    "spotify":bool(get_spotify_token()),
                    "version":"2.2",
                    "data_db":data_manager._ready,
                    "data_tools":[t["name"] for t in data_manager.TOOLS]})

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
            rows  = db_q("SELECT id, label, trigger_at, prepare FROM reminders WHERE status='pending'", fetch="all") or []
            prep_by_id = {r["id"]: r.get("prepare") for r in rows}
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
                # Solo el worker que logre cambiar 'pending' → 'notified' lo dispara
                claimed = db_q("UPDATE reminders SET status='notified' WHERE id=%s AND status='pending' RETURNING id",
                               (rid,), fetch="one")
                if not claimed:
                    continue
                if prep_by_id.get(rid):
                    # Contenido preparado por Vesper: se genera en otro hilo para no frenar el scheduler
                    threading.Thread(target=deliver_smart_reminder,
                                     args=(rid, label, prep_by_id[rid]), daemon=True).start()
                    continue
                db_q("INSERT INTO notifications (label,type,time) VALUES (%s,%s,%s)",
                          (label,"reminder",now_utc.strftime("%Y-%m-%d %H:%M UTC")))
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
                        claimed = db_q("UPDATE alarms SET active=FALSE WHERE id=%s AND active=TRUE RETURNING id",
                                       (aid,), fetch="one")
                        if not claimed:
                            continue
                        db_q("INSERT INTO notifications (label,type,time) VALUES (%s,%s,%s)",
                                  (f"⏰ {label}","alarm",alarm_time))
                        print(f"[ALARM] {label}")
                except Exception as ae:
                    print(f"[ALARM] Error: {ae}")

        except Exception as e:
            print(f"[SCHEDULER] Error: {e}")
        music_radio.tick()   # DJ: rellena la radio si se está acabando
        time.sleep(20)

# ── Recordatorios con contenido preparado ────────────────────────────────────
REMINDER_TOOLS = {"web_search", "db_describe", "db_find", "db_sql", "financial_action"}

def generate_reminder_content(label, instruction):
    """Vesper genera el contenido de un recordatorio en el momento en que suena.
    Puede consultar sus tablas, finanzas e internet (solo lectura)."""
    session = _session_cache.get(OWNER_NAME) or {"user_type":"owner","name":OWNER_NAME,"db_path":OWNER_DB,
                                                 "location":get_location()}
    try:
        snapshot = build_context_snapshot(session, {"mode":"text"}, db_q=db_q, get_weather=get_weather,
                                          weather_summary=weather_summary, spotify_headers=spotify_headers)
    except Exception as e:
        print(f"[REMINDER] snapshot: {e}")
        snapshot = f"Hora local: {now_local().strftime('%Y-%m-%d %H:%M')}"
    memories = get_relevant_memories(instruction)
    system = (
        "Eres VESPER, asistente personal del Sr. Ozhelli. Un recordatorio que él programó acaba de sonar y "
        "debes entregarle el contenido que te dejaste encargado.\n\n"
        f"{snapshot}\n"
        f"\nLO QUE SÉ SOBRE EL SEÑOR:{memories}\n"
        f"{preferences.prompt_block(db_q)}"
        f"{data_manager.prompt_block()}\n"
        "REGLAS:\n"
        "- Usa las tools si necesitas datos reales (sus tablas, finanzas, internet). Son de solo lectura.\n"
        "- Entrega el contenido directo, listo para usarse: cantidades exactas, horarios, pasos. Markdown sencillo "
        "(negritas, listas, tablas cortas). Máximo ~200 palabras.\n"
        "- No saludes largo ni cierres con preguntas; a lo mucho una sugerencia final breve.\n"
        "- Llama al dueño 'señor'. Nunca reveles que eres Claude.\n"
    )
    tools = [t for t in get_all_tools() if t["name"] in REMINDER_TOOLS]
    messages = [{"role":"user","content":f"Recordatorio: {label}\nEncargo: {instruction}"}]
    text = ""
    loop = asyncio.new_event_loop()
    try:
        for _ in range(6):
            resp = client.messages.create(model="claude-haiku-4-5", max_tokens=1200,
                                          system=system, tools=tools, messages=messages)
            parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            if parts: text = " ".join(parts)
            if resp.stop_reason != "tool_use": break
            uses = [b for b in resp.content if b.type == "tool_use"]
            messages.append({"role":"assistant","content":resp.content})
            results = []
            for u in uses:
                if u.name not in REMINDER_TOOLS or (u.name == "financial_action" and
                                                    (u.input or {}).get("action") != "consultar"):
                    out = "No disponible en recordatorios (solo lectura)."
                else:
                    out = loop.run_until_complete(dispatch_action(u.name, u.input, OWNER_DB))
                    try:
                        d = json.loads(out)
                        if isinstance(d, dict) and "text" in d: out = d["text"]
                    except Exception: pass
                results.append({"type":"tool_result","tool_use_id":u.id,"content":str(out)[:6000]})
            messages.append({"role":"user","content":results})
    finally:
        loop.close()
    return text.strip()

def deliver_smart_reminder(rid, label, instruction):
    body = ""
    try:
        body = generate_reminder_content(label, instruction)
    except Exception as e:
        print(f"[REMINDER] generación falló ({rid}): {e}")
    if not body:
        body = f"No pude preparar el contenido a tiempo. Lo que tenía encargado: {instruction}"
    db_q("INSERT INTO notifications (label,type,time,body) VALUES (%s,%s,%s,%s)",
         (label, "reminder", now_local().strftime("%Y-%m-%d %H:%M"), body))
    # Queda en el historial para que el señor pueda preguntar sobre él después
    save_message("assistant", f"⏰ Recordatorio: {label}\n\n{body}", OWNER_DB, intent="reminder")
    print(f"[REMINDER] {label} (con contenido, {len(body)} caracteres)")

def get_relevant_memories(context_hint=""):
    """Recupera memorias relevantes para el contexto actual."""
    try:
        rows     = db_q("""SELECT category, key, value, updated_at FROM memories
                           WHERE NOT (category='preferencias' AND source='confirmed')
                           ORDER BY updated_at DESC LIMIT 40""", fetch="all") or []
        patterns = db_q("SELECT pattern_type, description, occurrences FROM patterns ORDER BY last_seen DESC LIMIT 10", fetch="all") or []
        today    = now_local().strftime("%Y-%m-%d")
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
            for p in patterns:
                mem_str += f"  • {p['pattern_type']} ({p['occurrences']}x): {p['description']}\n"

        if sum_row:
            mem_str += f"\n\n[RESUMEN DE HOY]\n  {sum_row['summary']}"

        return mem_str
    except Exception as e:
        print(f"[MEMORY] Error leyendo: {e}")
        return ""

def extract_and_save_memories(recent_messages, user_text, assistant_text, session):
    """
    Corre en un hilo aparte después de cada respuesta. Extrae:
      - memorias: hechos nuevos del señor (se guardan directo)
      - señales de preferencia: gustos/criterios que se CUENTAN (ver preferences.py)
    Las señales se sacan SOLO del último intercambio para no contar dos veces lo mismo.
    """
    if session.get("user_type") != "owner":
        return
    try:
        def fmt(m):
            c = m.get("content")
            return f"{m.get('role')}: {c if isinstance(c,str) else str(c)[:200]}"
        context_txt = "\n".join(fmt(m) for m in recent_messages[:-2])[-1500:]
        known = preferences.known_candidate_keys(db_q)
        known_txt = "\n".join(f"  - {k}" for k in known) if known else "  (ninguna aún)"

        result = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=f"""Eres el extractor de memoria de Vesper, asistente personal del Sr. Ozhelli.
Responde ÚNICAMENTE con JSON válido, sin texto extra:
{{
  "memories": [{{"category": "...", "key": "clave_snake_case", "value": "..."}}],
  "patterns": [{{"type": "...", "description": "..."}}],
  "preference_signals": [{{"category": "...", "key": "clave_snake_case", "value": "Prefiere ...", "evidence": "qué pasó"}}]
}}
- memories: SOLO hechos nuevos e importantes (datos personales, trabajo, proyectos, salud...).
  Categorías: personal, trabajo, finanzas, estilo, deportes, proyectos, musica, salud, relaciones.
  NO pongas gustos ni criterios aquí.
- preference_signals: cuando en el ÚLTIMO INTERCAMBIO el señor muestra un gusto o criterio con una
  decisión concreta (rechaza/elige un producto por una razón, pide algo de cierta forma, corrige a Vesper).
  Una señal por criterio. Categorías: compras, comida, musica, comunicacion, trabajo, rutina, general.
  Si el criterio ya existe en esta lista, REUTILIZA EXACTAMENTE su clave:
{known_txt}
- NO guardes como memoria propuestas de tablas, esquemas SQL, "funciones por implementar" ni registros que ya
  se guardaron en la base de datos con las tools db_* (contactos, medidas, etc.).
- Si no hay nada, usa listas vacías. No inventes.""",
            messages=[{"role": "user", "content":
                f"CONTEXTO PREVIO (solo referencia, no extraigas señales de aquí):\n{context_txt}\n\n"
                f"ÚLTIMO INTERCAMBIO:\nseñor: {user_text[:1500]}\nvesper: {assistant_text[:1500]}"}]
        )
        raw  = result.content[0].text.strip()
        raw  = raw.replace("```json","").replace("```","").strip()
        data = json.loads(raw)

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
        for sig in data.get("preference_signals", []):
            if sig.get("key") and sig.get("value"):
                st = preferences.record_signal(db_q, sig.get("category","general"), sig["key"],
                                               str(sig["value"]), str(sig.get("evidence","")))
                print(f"[PREFS] señal {sig['key']} → {st}")
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

            convo = "\n".join([f"{r['role']}: {r['content'][:100]}" for r in rows])
            result = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=300,
                system="Resume en 2-3 oraciones el día del usuario basándote en sus conversaciones con Vesper. Sé conciso y factual.",
                messages=[{"role":"user","content":f"Conversaciones de hoy:\n{convo}"}]
            )
            summary = result.content[0].text.strip()
            gastos_row = db_q("SELECT SUM(amount) as total FROM financial WHERE DATE(created_at)=CURRENT_DATE AND action='registrar_gasto' AND COALESCE(status,'') <> 'anulado'", fetch="one")
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
