"""
preferences.py — Aprendizaje de preferencias por conteo + confirmación.

Cómo funciona:
  1. Después de cada respuesta, el extractor de memoria (en segundo plano) detecta
     "señales" de preferencia en lo que dijo el señor. Ej: rechazó un pan por el sodio
     → señal {key: "bajo_sodio", value: "Prefiere productos bajos en sodio"}.
  2. Cada señal suma 1 a su contador, pero solo si pasaron al menos PREF_MIN_GAP_MIN
     minutos desde la última vez que contó (así 3 panes en la misma visita al súper
     cuentan como 1, no como 3).
  3. Al llegar a PREF_THRESHOLD veces, pasa a "por confirmar".
  4. Vesper le pregunta al señor (máximo una vez cada PREF_ASK_COOLDOWN_H horas)
     si la guarda como regla fija. Con su respuesta usa la tool confirm_preference:
       - aceptada  → se guarda en memories (categoría 'preferencias', source 'confirmed')
                     y aparece siempre en el contexto como REGLA FIJA.
       - rechazada → no se vuelve a contar ni a preguntar.
"""

import os

PREF_THRESHOLD      = int(os.environ.get("PREF_THRESHOLD", "3"))
PREF_MIN_GAP_MIN    = int(os.environ.get("PREF_MIN_GAP_MIN", "20"))
PREF_ASK_COOLDOWN_H = int(os.environ.get("PREF_ASK_COOLDOWN_H", "12"))

TABLE_SQL = """CREATE TABLE IF NOT EXISTS preference_candidates (
    id SERIAL PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'general',
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    evidence TEXT,
    occurrences INTEGER DEFAULT 1,
    status TEXT DEFAULT 'observing',   -- observing | pending_confirm | confirmed | rejected
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_counted TIMESTAMPTZ DEFAULT NOW(),
    asked_at TIMESTAMPTZ)"""

CONFIRM_TOOL = {
    "name": "confirm_preference",
    "description": ("Registra la respuesta del señor cuando le preguntaste si quiere guardar una "
                    "preferencia detectada como regla fija. Úsala SOLO después de que él conteste."),
    "input_schema": {"type": "object", "properties": {
        "key": {"type": "string", "description": "La clave exacta de la preferencia por confirmar"},
        "accepted": {"type": "boolean", "description": "true si dijo que sí la guarde, false si no"},
        "value": {"type": "string", "description": "Opcional: redacción corregida si él la ajustó"}
    }, "required": ["key", "accepted"]}
}


def _norm_key(key):
    key = (key or "").strip().lower().replace(" ", "_").replace("-", "_")
    return "".join(ch for ch in key if ch.isalnum() or ch == "_")[:60]


def init_table(db_q):
    db_q(TABLE_SQL)


def known_candidate_keys(db_q, limit=40):
    """Claves existentes, para que el extractor reutilice la misma clave y el conteo funcione."""
    rows = db_q("""SELECT key, value, occurrences, status FROM preference_candidates
                   WHERE status IN ('observing','pending_confirm','confirmed')
                   ORDER BY last_counted DESC LIMIT %s""", (limit,), fetch="all") or []
    return [f"{r['key']} = {r['value']} ({r['status']}, {r['occurrences']}x)" for r in rows]


def record_signal(db_q, category, key, value, evidence=""):
    """Suma una ocurrencia respetando la separación mínima. Regresa el estado resultante."""
    key = _norm_key(key)
    if not key or not value:
        return None
    row = db_q("SELECT id, occurrences, status, last_counted FROM preference_candidates WHERE key=%s",
               (key,), fetch="one")
    if not row:
        status = "pending_confirm" if PREF_THRESHOLD <= 1 else "observing"
        db_q("""INSERT INTO preference_candidates (category, key, value, evidence, status)
                VALUES (%s,%s,%s,%s,%s)""", (category or "general", key, value, evidence[:300], status))
        return status
    if row["status"] in ("confirmed", "rejected"):
        return row["status"]
    counted = db_q(f"""UPDATE preference_candidates
                SET occurrences = occurrences + 1, last_counted = NOW(), evidence = %s,
                    status = CASE WHEN occurrences + 1 >= %s THEN 'pending_confirm' ELSE status END
                WHERE id = %s AND last_counted < NOW() - INTERVAL '{PREF_MIN_GAP_MIN} minutes'
                RETURNING status""", (evidence[:300], PREF_THRESHOLD, row["id"]), fetch="one")
    return counted["status"] if counted else row["status"]


def next_to_confirm(db_q):
    """Una preferencia lista para preguntar (respeta el enfriamiento). Marca asked_at."""
    row = db_q(f"""UPDATE preference_candidates SET asked_at = NOW()
                   WHERE id = (SELECT id FROM preference_candidates
                               WHERE status='pending_confirm'
                                 AND (asked_at IS NULL OR asked_at < NOW() - INTERVAL '{PREF_ASK_COOLDOWN_H} hours')
                               ORDER BY occurrences DESC, last_counted DESC LIMIT 1)
                   RETURNING key, value, occurrences, evidence""", fetch="one")
    return row


def confirmed_rules(db_q):
    rows = db_q("""SELECT key, value FROM memories
                   WHERE category='preferencias' AND source='confirmed'
                   ORDER BY updated_at DESC LIMIT 30""", fetch="all") or []
    return [f"{r['value']}" for r in rows]


def prompt_block(db_q):
    """Texto para el system prompt: reglas fijas + (si toca) una preferencia por confirmar."""
    out = ""
    try:
        rules = confirmed_rules(db_q)
        if rules:
            out += "\nREGLAS FIJAS DEL SEÑOR (confirmadas por él, aplícalas siempre sin repetírselas):\n"
            out += "\n".join(f"  • {r}" for r in rules) + "\n"
        pending = next_to_confirm(db_q)
        if pending:
            out += (f"\nPREFERENCIA POR CONFIRMAR: he notado {pending['occurrences']} veces que "
                    f"\"{pending['value']}\" (clave: {pending['key']}; último ejemplo: {pending['evidence'] or 'n/d'}).\n"
                    "Si encaja de forma natural en esta respuesta, pregúntale en una frase corta si quiere "
                    "que la guardes como regla fija. Cuando conteste, usa confirm_preference. "
                    "Si no encaja ahora, no la menciones.\n")
    except Exception as e:
        print(f"[PREFS] prompt_block: {e}")
    return out


def confirm(db_q, key, accepted, value=None):
    key = _norm_key(key)
    row = db_q("SELECT value, category FROM preference_candidates WHERE key=%s", (key,), fetch="one")
    if not row:
        return f"No encontré la preferencia '{key}'."
    final_value = (value or row["value"]).strip()
    if accepted:
        db_q("UPDATE preference_candidates SET status='confirmed', value=%s WHERE key=%s", (final_value, key))
        db_q("""INSERT INTO memories (category, key, value, source, updated_at)
                VALUES ('preferencias', %s, %s, 'confirmed', NOW())
                ON CONFLICT (category, key) DO UPDATE
                SET value=EXCLUDED.value, source='confirmed', updated_at=NOW()""", (key, final_value))
        return f"Regla guardada: {final_value}."
    db_q("UPDATE preference_candidates SET status='rejected' WHERE key=%s", (key,))
    return "Entendido, no la guardaré ni volveré a preguntar."


def list_all(db_q):
    rows = db_q("""SELECT category, key, value, occurrences, status, first_seen, last_counted
                   FROM preference_candidates ORDER BY status, occurrences DESC""", fetch="all") or []
    return [{**{k: r[k] for k in ("category", "key", "value", "occurrences", "status")},
             "first_seen": str(r["first_seen"]), "last_counted": str(r["last_counted"])} for r in rows]
