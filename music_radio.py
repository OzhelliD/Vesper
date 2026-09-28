"""
music_radio.py — Modo radio de Vesper (v2.2)

"Pon música country" → sin crear playlist, Vesper elige canciones y se las entrega a Spotify
como una cola. La reproducción ocurre EN EL DISPOSITIVO de Spotify (celular, compu, bocina),
así que sigue sonando mientras Vesper piensa, responde o habla: no depende del servidor.

Un "DJ" en segundo plano (tick() cada ~15 s desde el scheduler) vigila qué suena y, cuando
quedan pocas canciones, genera más del mismo estilo sin repetir y las agrega a la cola.

Estado en la tabla music_session (una fila) → funciona con varios workers de gunicorn:
el relleno se "reclama" con UPDATE ... RETURNING para que solo un proceso lo haga.

Extra: duck(on) baja el volumen mientras Vesper habla y lo restaura al terminar
(solo en dispositivos que permiten controlar volumen; en iPhone Spotify no lo permite).
"""

import json, time, threading, re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

API = "https://api.spotify.com/v1"
FIRST_BATCH  = 14      # canciones al arrancar
REFILL_BATCH = 8       # canciones por relleno
REFILL_WHEN  = 2       # rellenar cuando queden ≤ N canciones por sonar
DUCK_PERCENT = 25      # volumen mientras Vesper habla
DUCK_MAX_S   = 90      # si el navegador no avisa que terminó, se restaura solo

_db_q = None
_headers = None
_client = None
_pool = ThreadPoolExecutor(max_workers=8)

TABLE_SQL = """CREATE TABLE IF NOT EXISTS music_session (
    id INTEGER PRIMARY KEY DEFAULT 1,
    active BOOLEAN DEFAULT FALSE,
    mood TEXT,
    device_id TEXT,
    tracks JSONB DEFAULT '[]'::jsonb,          -- [{uri, name, artist}] en orden
    refill_lock TIMESTAMPTZ,
    duck_prev INTEGER,
    duck_until TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW())"""


def init(db_q, spotify_headers, anthropic_client):
    global _db_q, _headers, _client
    _db_q, _headers, _client = db_q, spotify_headers, anthropic_client
    db_q(TABLE_SQL)
    db_q("INSERT INTO music_session (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


# ── Spotify helpers ──────────────────────────────────────────────────────────
def _h():
    h = _headers()
    if not h:
        raise RuntimeError("spotify_login_required")
    return h


def _req(method, path, **kw):
    r = requests.request(method, API + path, headers=_h(), timeout=10, **kw)
    if r.status_code == 429:
        time.sleep(min(int(r.headers.get("Retry-After", "1")), 5))
        r = requests.request(method, API + path, headers=_h(), timeout=10, **kw)
    return r


def pick_device(preferred=None):
    """Dispositivo activo > el que usaba la radio > el primero disponible."""
    r = _req("GET", "/me/player/devices")
    devices = r.json().get("devices", []) if r.status_code == 200 else []
    if not devices:
        return None
    for d in devices:
        if d.get("is_active"):
            return d
    for d in devices:
        if preferred and d.get("id") == preferred:
            return d
    for d in devices:            # compu antes que celular: suele ser donde está abierto Vesper
        if d.get("type") == "Computer":
            return d
    return devices[0]


def player_state():
    r = _req("GET", "/me/player")
    if r.status_code != 200 or not r.text:
        return None
    return r.json()


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def search_track(title, artist):
    """Búsqueda precisa (track:/artist:) y, si falla, búsqueda libre."""
    for q in (f'track:"{title}" artist:"{artist}"', f"{title} {artist}"):
        try:
            r = _req("GET", "/search", params={"q": q, "type": "track", "limit": 3})
            items = r.json().get("tracks", {}).get("items", []) if r.status_code == 200 else []
            for it in items:
                if it.get("is_playable", True) is not False:
                    return {"uri": it["uri"], "name": it["name"], "artist": it["artists"][0]["name"]}
        except Exception as e:
            print(f"[RADIO] búsqueda '{title}': {e}")
    return None


# ── El "DJ": elige canciones ────────────────────────────────────────────────
def _taste():
    try:
        rows = _db_q("""SELECT key, value FROM memories WHERE category IN ('musica','música','preferencias')
                        ORDER BY updated_at DESC LIMIT 12""", fetch="all") or []
        return "; ".join(f"{r['key']}: {r['value']}" for r in rows)
    except Exception:
        return ""


def ai_pick_songs(mood, n, exclude):
    ex = "\n".join(f"- {t}" for t in exclude[-80:]) or "(ninguna)"
    res = _client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=1200,
        system=("Eres un DJ experto armando una radio continua. Elige canciones REALES que existan en Spotify, "
                "con buena transición entre ellas, variando artistas (máximo 2 por artista), mezclando clásicos "
                "y actuales salvo que se pida otra cosa. Responde SOLO JSON válido, sin texto extra."),
        messages=[{"role": "user", "content":
                   f"Estación: {mood}\nGustos del oyente: {_taste() or 'sin datos'}\n"
                   f"NO repitas ninguna de estas:\n{ex}\n\n"
                   f'Dame {n} canciones: {{"songs":[{{"title":"...","artist":"..."}}]}}'}])
    raw = res.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    raw = raw[raw.find("{"): raw.rfind("}") + 1]
    return json.loads(raw).get("songs", [])


def _resolve(songs, exclude_uris=()):
    futures = [_pool.submit(search_track, s.get("title", ""), s.get("artist", "")) for s in songs]
    out, seen = [], set(exclude_uris)
    for f in futures:
        try:
            t = f.result(timeout=15)
        except Exception:
            t = None
        if t and t["uri"] not in seen:
            seen.add(t["uri"])
            out.append(t)
    return out


# ── Sesión ───────────────────────────────────────────────────────────────────
def _session():
    row = _db_q("SELECT * FROM music_session WHERE id=1", fetch="one") or {}
    row = dict(row)
    row["tracks"] = row.get("tracks") or []
    return row


def _save(**fields):
    sets = ", ".join(f"{k}=%s" for k in fields) + ", updated_at=NOW()"
    vals = [json.dumps(v) if k == "tracks" else v for k, v in fields.items()]
    _db_q(f"UPDATE music_session SET {sets} WHERE id=1", vals)


def deactivate(reason=""):
    try:
        _db_q("UPDATE music_session SET active=FALSE, updated_at=NOW() WHERE id=1 AND active=TRUE")
        if reason:
            print(f"[RADIO] detenida: {reason}")
    except Exception:
        pass


def start(mood):
    """Arranca la radio. Devuelve dict para la UI / el modelo."""
    try:
        _h()
    except RuntimeError:
        return {"action": "spotify_login_required"}
    songs = ai_pick_songs(mood, FIRST_BATCH + 2, [])
    tracks = _resolve(songs)
    if not tracks:
        return {"action": "radio_error", "error": f"No encontré canciones para '{mood}' en Spotify."}
    prev = _session().get("device_id")
    dev = pick_device(prev)
    if not dev:
        return {"action": "spotify_no_device", "name": tracks[0]["name"], "artist": tracks[0]["artist"]}
    params = {"device_id": dev["id"]}
    r = _req("PUT", "/me/player/play", params=params, json={"uris": [t["uri"] for t in tracks]})
    if r.status_code not in (200, 202, 204):
        return {"action": "radio_error", "error": f"Spotify rechazó la reproducción ({r.status_code})."}
    # Orden tal cual (sin aleatorio) y sin repetir la lista: el DJ se encarga de que no se acabe
    for path, st in (("/me/player/shuffle", "false"), ("/me/player/repeat", "off")):
        try:
            _req("PUT", path, params={"state": st, "device_id": dev["id"]})
        except Exception:
            pass
    _save(active=True, mood=mood, device_id=dev["id"], tracks=tracks, refill_lock=None,
          started_at=datetime.now(timezone.utc))
    return {"action": "radio", "mood": mood, "device": dev.get("name"),
            "name": tracks[0]["name"], "artist": tracks[0]["artist"],
            "upcoming": [f"{t['name']} — {t['artist']}" for t in tracks[1:5]], "total": len(tracks)}


def _current_index(sess, state):
    item = (state or {}).get("item") or {}
    uris = {item.get("uri"), (item.get("linked_from") or {}).get("uri")}
    for i, t in enumerate(sess["tracks"]):
        if t["uri"] in uris:
            return i
    return None


def tick():
    """Llamar cada ~15 s. Rellena la cola y restaura el volumen si quedó bajo."""
    try:
        sess = _session()
        if not sess.get("active"):
            return
        # Seguro del "duck": si el navegador nunca avisó que terminó de hablar
        if sess.get("duck_until") and sess["duck_until"] < datetime.now(timezone.utc):
            duck(False)
        state = player_state()
        if not state or not state.get("item"):
            return
        idx = _current_index(sess, state)
        if idx is None:
            # Algo que no es de la radio está sonando → el señor cambió de música
            started = sess.get("started_at")
            if started and (datetime.now(timezone.utc) - started).total_seconds() > 60:
                deactivate("sonando algo fuera de la radio")
            return
        remaining = len(sess["tracks"]) - idx - 1
        if remaining > REFILL_WHEN:
            return
        claimed = _db_q("""UPDATE music_session SET refill_lock = NOW() + INTERVAL '2 minutes'
                           WHERE id=1 AND active AND (refill_lock IS NULL OR refill_lock < NOW())
                           RETURNING id""", fetch="one")
        if not claimed:
            return
        threading.Thread(target=_refill, args=(sess,), daemon=True).start()
    except RuntimeError:
        pass
    except Exception as e:
        print(f"[RADIO] tick: {e}")


def _refill(sess):
    try:
        played = [f"{t['name']} — {t['artist']}" for t in sess["tracks"]]
        songs = ai_pick_songs(sess["mood"], REFILL_BATCH + 2, played)
        new = _resolve(songs, [t["uri"] for t in sess["tracks"]])[:REFILL_BATCH]
        dev = (player_state() or {}).get("device", {}).get("id") or sess.get("device_id")
        added = []
        for t in new:
            r = _req("POST", "/me/player/queue", params={"uri": t["uri"], "device_id": dev})
            if r.status_code in (200, 202, 204):
                added.append(t)
        cur = _session()
        if cur.get("active") and cur.get("mood") == sess["mood"]:
            _save(tracks=cur["tracks"] + added, refill_lock=None)
        print(f"[RADIO] +{len(added)} canciones ({sess['mood']})")
    except Exception as e:
        print(f"[RADIO] relleno falló: {e}")
        _db_q("UPDATE music_session SET refill_lock=NULL WHERE id=1")


# ── Controles ────────────────────────────────────────────────────────────────
def control(action, volume=None):
    try:
        _h()
    except RuntimeError:
        return "Spotify no está conectado."
    sess = _session()
    state = player_state() or {}
    dev = (state.get("device") or {}).get("id") or sess.get("device_id")
    p = {"device_id": dev} if dev else {}
    a = (action or "").lower()
    if a in ("pausar", "pause"):
        _req("PUT", "/me/player/pause", params=p); return "Música en pausa."
    if a in ("reanudar", "play", "resume"):
        _req("PUT", "/me/player/play", params=p); return "Música reanudada."
    if a in ("siguiente", "next"):
        _req("POST", "/me/player/next", params=p); return "Siguiente canción."
    if a in ("anterior", "previous"):
        _req("POST", "/me/player/previous", params=p); return "Canción anterior."
    if a in ("detener", "stop"):
        _req("PUT", "/me/player/pause", params=p); deactivate("detenida por el señor")
        return "Radio detenida."
    if a in ("volumen", "volume"):
        if volume is None:
            return "Indica el volumen (0-100)."
        if not (state.get("device") or {}).get("supports_volume", True):
            return "Este dispositivo no permite cambiar el volumen desde Vesper (p. ej. iPhone)."
        _req("PUT", "/me/player/volume", params={**p, "volume_percent": max(0, min(100, int(volume)))})
        return f"Volumen al {int(volume)}%."
    if a in ("que_suena", "estado", "status"):
        return now_playing_text(sess, state)
    return f"Acción '{action}' no reconocida."


def now_playing_text(sess=None, state=None):
    sess = sess or _session()
    state = state if state is not None else (player_state() or {})
    item = state.get("item")
    if not item:
        return "No suena nada en este momento."
    txt = f"Suena: {item['name']} — {', '.join(a['name'] for a in item.get('artists', []))}"
    if not state.get("is_playing"):
        txt += " (en pausa)"
    if sess.get("active"):
        idx = _current_index(sess, state)
        txt += f". Radio activa: {sess['mood']}."
        if idx is not None:
            nxt = sess["tracks"][idx + 1: idx + 4]
            if nxt:
                txt += " Siguen: " + "; ".join(f"{t['name']} — {t['artist']}" for t in nxt) + "."
    return txt


def duck(on):
    """Baja el volumen mientras Vesper habla (si la radio está activa y el dispositivo lo permite)."""
    try:
        sess = _session()
        if on:
            if not sess.get("active") or sess.get("duck_prev") is not None:
                return False
            state = player_state() or {}
            dev = state.get("device") or {}
            if not state.get("is_playing") or not dev.get("supports_volume"):
                return False
            prev = dev.get("volume_percent")
            if prev is None or prev <= DUCK_PERCENT:
                return False
            _db_q("""UPDATE music_session SET duck_prev=%s, duck_until=NOW() + (%s || ' seconds')::interval
                     WHERE id=1""", (prev, str(DUCK_MAX_S)))
            _req("PUT", "/me/player/volume", params={"volume_percent": DUCK_PERCENT, "device_id": dev.get("id")})
            return True
        # RETURNING devuelve el valor NUEVO; el anterior se toma de la subconsulta (bloqueada)
        row = _db_q("""UPDATE music_session m SET duck_prev=NULL, duck_until=NULL
                       FROM (SELECT duck_prev AS old FROM music_session WHERE id=1 FOR UPDATE) o
                       WHERE m.id=1 AND m.duck_prev IS NOT NULL RETURNING o.old""", fetch="one")
        if row and row["old"] is not None:
            _req("PUT", "/me/player/volume", params={"volume_percent": row["old"]})
            return True
    except Exception as e:
        print(f"[RADIO] duck: {e}")
    return False


def is_active():
    try:
        return bool(_session().get("active"))
    except Exception:
        return False


# ── Tools ────────────────────────────────────────────────────────────────────
TOOLS = [
    {"name": "play_music",
     "description": ("Pone música continua SIN crear playlist (modo radio): tú eliges canciones según lo pedido "
                     "(género, mood, artista, década, actividad) y Spotify las encadena en su dispositivo; se rellena "
                     "sola con más del mismo estilo y sigue sonando mientras conversan. Úsala para 'pon música de X', "
                     "'algo para concentrarme', 'más como esta'. Para UNA canción exacta usa play_song; para guardar "
                     "una playlist usa create_playlist."),
     "input_schema": {"type": "object", "properties": {
         "mood": {"type": "string", "description": "Descripción rica de la estación, ej. 'country moderno y clásico, "
                                                    "energético para el gym, estilo Morgan Wallen, Luke Combs, Johnny Cash'"}},
         "required": ["mood"]}},
    {"name": "music_control",
     "description": "Controla la música que suena: pausar, reanudar, siguiente, anterior, detener (apaga la radio), "
                    "volumen (0-100), que_suena (qué suena y qué sigue).",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["pausar", "reanudar", "siguiente", "anterior", "detener", "volumen", "que_suena"]},
         "volume": {"type": "integer"}},
         "required": ["action"]}},
]

PROMPT_RULES = (
    "- MÚSICA: 'pon música de X' → play_music (radio continua, sin playlist). La música suena en el dispositivo de "
    "Spotify y sigue sola mientras conversan; no tienes que hacer nada para que continúe ni la detengas al responder. "
    "Controles con music_control. Solo crea playlist si la pide explícitamente.\n"
)
