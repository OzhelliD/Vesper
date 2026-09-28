"""
data_manager.py — Base de datos dinámica de Vesper (v2.2)

Vesper administra sus propias tablas a partir de lo que el señor dice en lenguaje natural:
  "crea una tabla de contactos con nombre, teléfono y correo"
  "busca a Juan Pérez y agrégale su Instagram @juanp"   → si no existe la columna, la crea
  "crea una tabla para medir calorías, peso, % grasa y % músculo"
  "¿cuánto he bajado de peso este mes?"

Diseño:
  • Todas las tablas del señor viven en un esquema aparte (VESPER_DATA_SCHEMA, por defecto
    "vesper_data"). Las tablas del sistema (context_history, financial, reminders...) quedan
    fuera de su alcance de escritura: Vesper puede leerlas con db_sql, pero nunca alterarlas.
  • Opcional: VESPER_DATA_URL apunta a OTRA base PostgreSQL para los datos del señor.
  • El modelo NO escribe SQL de escritura. Usa tools estructuradas; aquí se arma el SQL con
    identificadores citados (psycopg2.sql) y valores parametrizados.
  • _catalogo guarda qué significa cada tabla/columna y sus alias → Vesper "entiende" qué tabla
    usar días después. _bitacora guarda cada cambio con los valores anteriores → db_undo.
  • Borrar una tabla la mueve a la papelera (se renombra), no se pierde.
"""

import os, re, json, difflib, unicodedata, threading, time
import datetime as dt
from decimal import Decimal
from contextlib import contextmanager

from psycopg2 import sql
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import ThreadedConnectionPool

SCHEMA       = os.environ.get("VESPER_DATA_SCHEMA", "vesper_data")
DATA_URL     = os.environ.get("VESPER_DATA_URL", "")
MAX_ROWS     = 200
DEFAULT_ROWS = 25
STMT_TIMEOUT = os.environ.get("VESPER_DATA_TIMEOUT", "8s")
LOCAL_TZ     = os.environ.get("VESPER_TZ", "America/Monterrey")

SYSTEM_COLS  = ("id", "creado_en", "actualizado_en")
CATALOG      = "_catalogo"
LOG          = "_bitacora"
TRASH_PREFIX = "_papelera_"

# Tipos que entiende Vesper → tipo PostgreSQL
TYPE_MAP = {
    "texto": "TEXT", "text": "TEXT", "string": "TEXT", "cadena": "TEXT",
    "email": "TEXT", "correo": "TEXT", "url": "TEXT", "telefono": "TEXT", "teléfono": "TEXT",
    "numero": "NUMERIC", "número": "NUMERIC", "number": "NUMERIC", "decimal": "NUMERIC",
    "float": "NUMERIC", "moneda": "NUMERIC", "porcentaje": "NUMERIC", "numeric": "NUMERIC",
    "entero": "BIGINT", "int": "BIGINT", "integer": "BIGINT", "bigint": "BIGINT",
    "booleano": "BOOLEAN", "boolean": "BOOLEAN", "bool": "BOOLEAN", "si_no": "BOOLEAN",
    "fecha": "DATE", "date": "DATE",
    "fecha_hora": "TIMESTAMPTZ", "datetime": "TIMESTAMPTZ", "timestamp": "TIMESTAMPTZ",
    "hora": "TIME", "time": "TIME",
    "json": "JSONB", "lista": "JSONB", "objeto": "JSONB", "jsonb": "JSONB",
}
# Tipo PostgreSQL (information_schema.data_type) → nombre que ve el modelo
PG_TO_HUMAN = {
    "text": "texto", "character varying": "texto", "numeric": "numero", "bigint": "entero",
    "integer": "entero", "smallint": "entero", "double precision": "numero", "real": "numero",
    "boolean": "booleano", "date": "fecha", "timestamp with time zone": "fecha_hora",
    "timestamp without time zone": "fecha_hora", "time without time zone": "hora", "jsonb": "json",
}
TEXTUAL = ("text", "character varying", "jsonb")

_ACC_FROM = "áàäâãéèëêíìïîóòöôõúùüûñç"
_ACC_TO   = "aaaaaeeeeiiiiooooouuuunc"


class DataError(Exception):
    """Error que se le devuelve tal cual al modelo para que corrija."""


# ── Conexión ─────────────────────────────────────────────────────────────────
_get_main_pool = None
_own_pool      = None
_ready         = False
_lock          = threading.Lock()


def init(get_pool_fn):
    """Llamar al arrancar. get_pool_fn = pool principal de Vesper (se usa si no hay VESPER_DATA_URL)."""
    global _get_main_pool, _ready
    _get_main_pool = get_pool_fn
    with _conn() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {}.{} (
                tabla TEXT PRIMARY KEY,
                descripcion TEXT DEFAULT '',
                alias JSONB DEFAULT '[]'::jsonb,
                columnas JSONB DEFAULT '{{}}'::jsonb,
                creado_en TIMESTAMPTZ DEFAULT NOW(),
                actualizado_en TIMESTAMPTZ DEFAULT NOW())""").format(
            sql.Identifier(SCHEMA), sql.Identifier(CATALOG)))
        cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {}.{} (
                id SERIAL PRIMARY KEY,
                tabla TEXT NOT NULL,
                operacion TEXT NOT NULL,
                detalle JSONB,
                deshecho BOOLEAN DEFAULT FALSE,
                creado_en TIMESTAMPTZ DEFAULT NOW())""").format(
            sql.Identifier(SCHEMA), sql.Identifier(LOG)))
    _ready = True
    print(f"[DATA] Esquema '{SCHEMA}' listo" + (" (base externa)" if DATA_URL else ""))


def _pool():
    global _own_pool
    if DATA_URL:
        with _lock:
            if _own_pool is None:
                _own_pool = ThreadedConnectionPool(1, 5, DATA_URL)
        return _own_pool
    if _get_main_pool is None:
        raise DataError("La base de datos dinámica no está inicializada.")
    return _get_main_pool()


@contextmanager
def _conn(readonly=False):
    pool = _pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if readonly:
                cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT set_config('statement_timeout', %s, true), set_config('TimeZone', %s, true)",
                        (STMT_TIMEOUT, LOCAL_TZ))
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ── Utilidades ───────────────────────────────────────────────────────────────
def strip_accents(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()


def norm_text(s):
    return strip_accents(str(s)).lower().strip()


def ident(name, what="nombre"):
    """'Porcentaje de Grasa (%)' → 'porcentaje_de_grasa_pct'. Seguro para PostgreSQL."""
    if name is None or not str(name).strip():
        raise DataError(f"El {what} está vacío.")
    s = str(name).replace("%", " pct ").replace("#", " num ")
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s:
        raise DataError(f"'{name}' no sirve como {what}.")
    if s[0].isdigit():
        s = "c_" + s
    return s[:55]


def _qt(table):
    return sql.Identifier(SCHEMA, table)


def _nexpr(col_sql):
    """Expresión SQL normalizada (minúsculas, sin acentos) de una columna."""
    return sql.SQL("translate(lower({}::text), {}, {})").format(
        col_sql, sql.Literal(_ACC_FROM), sql.Literal(_ACC_TO))


def _jsonable(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 10**15 else float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def _rows_json(rows):
    return [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows]


def _adapt(v):
    if isinstance(v, (dict, list)):
        return Json(v)
    return v


def _pg_type(t):
    if not t:
        return "TEXT"
    key = norm_text(t).replace(" ", "_")
    if key in TYPE_MAP:
        return TYPE_MAP[key]
    if t.upper() in set(TYPE_MAP.values()):
        return t.upper()
    raise DataError(f"Tipo '{t}' no reconocido. Usa: texto, numero, entero, booleano, fecha, fecha_hora, hora, json.")


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE   = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def infer_type(v):
    if isinstance(v, bool):  return "BOOLEAN"
    if isinstance(v, int):   return "BIGINT"
    if isinstance(v, (float, Decimal)): return "NUMERIC"
    if isinstance(v, (dict, list)):     return "JSONB"
    if isinstance(v, str):
        if _DATE_RE.match(v): return "DATE"
        if _TS_RE.match(v):   return "TIMESTAMPTZ"
    return "TEXT"


def _default_sql(default, pg_type):
    if default is None:
        return None
    d = norm_text(default) if isinstance(default, str) else default
    if d in ("hoy", "today", "current_date"):
        return sql.SQL("CURRENT_DATE")
    if d in ("ahora", "now", "now()", "current_timestamp"):
        return sql.SQL("NOW()") if pg_type != "DATE" else sql.SQL("CURRENT_DATE")
    if isinstance(default, (dict, list)):
        return sql.Literal(json.dumps(default))
    return sql.Literal(default)


# ── Introspección ────────────────────────────────────────────────────────────
def _tables(cur):
    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema=%s AND table_type='BASE TABLE' AND table_name NOT LIKE '\\_%%'
                   ORDER BY table_name""", (SCHEMA,))
    return [r["table_name"] for r in cur.fetchall()]


def _columns(cur, table):
    cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                   WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position""", (SCHEMA, table))
    return {r["column_name"]: r["data_type"] for r in cur.fetchall()}


def _catalog(cur, table=None):
    if table:
        cur.execute(sql.SQL("SELECT * FROM {} WHERE tabla=%s").format(_qt(CATALOG)), (table,))
        return cur.fetchone() or {"tabla": table, "descripcion": "", "alias": [], "columnas": {}}
    cur.execute(sql.SQL("SELECT * FROM {}").format(_qt(CATALOG)))
    return {r["tabla"]: r for r in cur.fetchall()}


def _variants(name):
    n = ident(name)
    out = {n}
    for suf in ("es", "s"):
        if n.endswith(suf):
            out.add(n[: -len(suf)])
    out.update({n + "s", n + "es"})
    return out


def resolve_table(cur, name):
    """Encuentra la tabla aunque la digan en singular, con acentos o por un alias."""
    tables = _tables(cur)
    if not tables:
        raise DataError("Aún no hay tablas. Crea una con db_create_table.")
    n = ident(name, "nombre de tabla")
    if n in tables:
        return n
    for v in _variants(name):
        if v in tables:
            return v
    cat = _catalog(cur)
    for t, row in cat.items():
        if t in tables and any(ident(a) in _variants(name) for a in (row.get("alias") or [])):
            return t
    # "medidas" → medidas_corporales, "corporales" → medidas_corporales (solo si hay una opción)
    stems = _variants(name)
    partial = [t for t in tables if any(v in t.split("_") or t.startswith(v + "_") for v in stems)]
    if len(partial) == 1:
        return partial[0]
    close = difflib.get_close_matches(n, tables, n=1, cutoff=0.8)
    if close:
        return close[0]
    if len(partial) > 1:
        raise DataError(f"'{name}' puede ser varias tablas: {', '.join(partial)}. Indica cuál.")
    raise DataError(f"No existe la tabla '{name}'. Tablas disponibles: {', '.join(tables)}.")


def resolve_column(cols, name, catalog_cols=None):
    """Devuelve el nombre real de la columna o None si no existe."""
    n = ident(name, "nombre de columna")
    if n in cols:
        return n
    for v in _variants(name):
        if v in cols:
            return v
    for c, meta in (catalog_cols or {}).items():
        if c in cols and isinstance(meta, dict) and n in [ident(a) for a in meta.get("alias", [])]:
            return c
    close = difflib.get_close_matches(n, [c for c in cols if c not in SYSTEM_COLS], n=1, cutoff=0.88)
    return close[0] if close else None


# ── Bitácora ─────────────────────────────────────────────────────────────────
def _log(cur, table, op, detail):
    cur.execute(sql.SQL("INSERT INTO {} (tabla, operacion, detalle) VALUES (%s,%s,%s)").format(_qt(LOG)),
                (table, op, Json(_jsonable(detail))))


def _touch_catalog(cur, table, description=None, aliases=None, col_notes=None):
    cur.execute(sql.SQL("""INSERT INTO {} (tabla, descripcion, alias, columnas) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (tabla) DO UPDATE SET
                             descripcion = CASE WHEN %s THEN EXCLUDED.descripcion ELSE {}.descripcion END,
                             alias = CASE WHEN %s THEN EXCLUDED.alias ELSE {}.alias END,
                             columnas = {}.columnas || EXCLUDED.columnas,
                             actualizado_en = NOW()""").format(
        _qt(CATALOG), sql.Identifier(CATALOG), sql.Identifier(CATALOG), sql.Identifier(CATALOG)),
        (table, description or "", Json(aliases or []), Json(col_notes or {}),
         description is not None, aliases is not None))


# ── Columnas nuevas sobre la marcha ──────────────────────────────────────────
def _ensure_columns(cur, table, cols, data, create_missing, type_hints, cat_cols):
    """Mapea las claves de `data` a columnas reales; crea las que falten si se permite.
    Devuelve (dict columna→valor, lista de columnas creadas)."""
    mapped, created = {}, []
    hints = {ident(k): v for k, v in (type_hints or {}).items()}
    for key, value in data.items():
        if ident(key) in SYSTEM_COLS:
            continue
        real = resolve_column(cols, key, cat_cols)
        if real is None:
            if not create_missing:
                raise DataError(f"La columna '{key}' no existe en {table}. Columnas: "
                                f"{', '.join(c for c in cols if c not in SYSTEM_COLS)}.")
            real = ident(key, "nombre de columna")
            pg_t = _pg_type(hints[real]) if real in hints else infer_type(value)
            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} " + pg_t).format(
                _qt(table), sql.Identifier(real)))
            cols[real] = PG_TO_HUMAN_REV.get(pg_t, pg_t.lower())
            created.append(f"{real} ({PG_TO_HUMAN.get(cols[real], pg_t.lower())})")
            _log(cur, table, "agregar_columna", {"columna": real, "tipo": pg_t})
        mapped[real] = value
    return mapped, created


PG_TO_HUMAN_REV = {"TEXT": "text", "NUMERIC": "numeric", "BIGINT": "bigint", "BOOLEAN": "boolean",
                   "DATE": "date", "TIMESTAMPTZ": "timestamp with time zone",
                   "TIME": "time without time zone", "JSONB": "jsonb"}


# ── Filtros ──────────────────────────────────────────────────────────────────
OPS = {"=": "=", "==": "=", "igual": "=", "!=": "<>", "<>": "<>", "distinto": "<>",
       ">": ">", ">=": ">=", "<": "<", "<=": "<=", "mayor": ">", "menor": "<"}


def _where(cur, table, cols, filters=None, search=None, row_id=None, cat_cols=None):
    parts, params = [], []
    if row_id is not None:
        ids = row_id if isinstance(row_id, list) else [row_id]
        parts.append(sql.SQL("{} = ANY(%s)").format(sql.Identifier("id")))
        params.append([int(i) for i in ids])
    for f in filters or []:
        if not isinstance(f, dict) or "column" not in f:
            raise DataError("Cada filtro debe ser {column, op, value}.")
        col = resolve_column(cols, f["column"], cat_cols)
        if col is None:
            raise DataError(f"Filtro sobre columna inexistente '{f['column']}'. "
                            f"Columnas: {', '.join(cols)}.")
        op = norm_text(f.get("op", "=")).replace(" ", "_")
        val = f.get("value")
        c = sql.Identifier(col)
        textual = cols[col] in TEXTUAL
        if op in ("contiene", "contains", "like", "ilike"):
            parts.append(sql.SQL("{} LIKE %s").format(_nexpr(c))); params.append(f"%{norm_text(val)}%")
        elif op in ("empieza", "empieza_con", "starts_with"):
            parts.append(sql.SQL("{} LIKE %s").format(_nexpr(c))); params.append(f"{norm_text(val)}%")
        elif op in ("entre", "between"):
            if not isinstance(val, list) or len(val) != 2:
                raise DataError("'entre' necesita value = [desde, hasta].")
            parts.append(sql.SQL("{} BETWEEN %s AND %s").format(c)); params += val
        elif op in ("en", "in"):
            vals = val if isinstance(val, list) else [val]
            if textual:
                parts.append(sql.SQL("{} = ANY(%s)").format(_nexpr(c))); params.append([norm_text(v) for v in vals])
            else:
                parts.append(sql.SQL("{} = ANY(%s)").format(c)); params.append(vals)
        elif op in ("es_nulo", "vacio", "is_null"):
            parts.append(sql.SQL("({} IS NULL OR {}::text = '')").format(c, c))
        elif op in ("no_nulo", "no_vacio", "not_null"):
            parts.append(sql.SQL("({} IS NOT NULL AND {}::text <> '')").format(c, c))
        elif op in OPS:
            o = sql.SQL(OPS[op])
            if textual and OPS[op] in ("=", "<>") and isinstance(val, str):
                parts.append(sql.SQL("{} {} %s").format(_nexpr(c), o)); params.append(norm_text(val))
            else:
                parts.append(sql.SQL("{} {} %s").format(c, o)); params.append(_adapt(val))
        else:
            raise DataError(f"Operador '{f.get('op')}' no válido. Usa =, !=, >, >=, <, <=, contiene, "
                            "empieza, entre, en, es_nulo, no_nulo.")
    if search and str(search).strip():
        data_cols = [c for c in cols if c not in SYSTEM_COLS] or list(cols)
        concat = sql.SQL("concat_ws(' ', {})").format(
            sql.SQL(", ").join(sql.SQL("{}::text").format(sql.Identifier(c)) for c in data_cols))
        for word in norm_text(search).split():
            parts.append(sql.SQL("{} LIKE %s").format(_nexpr(concat))); params.append(f"%{word}%")
    if not parts:
        return sql.SQL(""), []
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts), params


def _has_criteria(row_id, filters, search):
    return row_id is not None or bool(filters) or bool(search and str(search).strip())


def _suggest(cur, table, cols, search, limit=5):
    """Si una búsqueda no encuentra nada, sugiere filas parecidas (cualquier palabra)."""
    if not search:
        return []
    data_cols = [c for c in cols if c not in SYSTEM_COLS] or list(cols)
    concat = sql.SQL("concat_ws(' ', {})").format(
        sql.SQL(", ").join(sql.SQL("{}::text").format(sql.Identifier(c)) for c in data_cols))
    words = [w for w in norm_text(search).split() if len(w) >= 3]
    if not words:
        return []
    cond = sql.SQL(" OR ").join(sql.SQL("{} LIKE %s").format(_nexpr(concat)) for _ in words)
    cur.execute(sql.SQL("SELECT * FROM {} WHERE {} ORDER BY id DESC LIMIT %s").format(_qt(table), cond),
                [f"%{w}%" for w in words] + [limit])
    return _rows_json(cur.fetchall())


def _card(table, rows, total=None, title=None, highlight=None):
    rows = _rows_json(rows)[:50]
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return {"type": "data_table", "table": table, "title": title or table.replace("_", " "),
            "columns": cols, "rows": rows, "total": total if total is not None else len(rows),
            "highlight": highlight or []}


def _result(text, card=None):
    return {"text": text, "card": card}


def _fmt_rows(rows, limit=30):
    """Filas compactas para el modelo (la tarjeta de la UI sí lleva todo)."""
    rows = _rows_json(rows)
    for r in rows:
        r.pop("actualizado_en", None)
        if isinstance(r.get("creado_en"), str):
            r["creado_en"] = r["creado_en"][:16].replace("T", " ")
    out = json.dumps(rows[:limit], ensure_ascii=False, default=str)
    if len(rows) > limit:
        out += f"\n(... {len(rows) - limit} filas más)"
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Operaciones (cada una devuelve {"text": ..., "card": ...})
# ═════════════════════════════════════════════════════════════════════════════
def describe(table=None, include_trash=False):
    with _conn() as cur:
        if not table:
            tables = _tables(cur)
            cat = _catalog(cur)
            if not tables:
                return _result("No hay tablas todavía. Puedes crear una con db_create_table.")
            lines = []
            for t in tables:
                cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(_qt(t)))
                n = cur.fetchone()["n"]
                cols = _columns(cur, t)
                desc = (cat.get(t) or {}).get("descripcion") or ""
                human = ", ".join(f"{c}:{PG_TO_HUMAN.get(tp, tp)}" for c, tp in cols.items() if c not in SYSTEM_COLS)
                lines.append(f"- {t} ({n} filas){' — ' + desc if desc else ''}\n  columnas: {human}")
            if include_trash:
                cur.execute("""SELECT table_name FROM information_schema.tables
                               WHERE table_schema=%s AND table_name LIKE %s""", (SCHEMA, TRASH_PREFIX + "%"))
                trash = [r["table_name"] for r in cur.fetchall()]
                if trash:
                    lines.append("Papelera: " + ", ".join(trash))
            return _result("Tablas:\n" + "\n".join(lines))
        t = resolve_table(cur, table)
        cols = _columns(cur, t)
        cat = _catalog(cur, t)
        cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(_qt(t)))
        n = cur.fetchone()["n"]
        cur.execute(sql.SQL("SELECT * FROM {} ORDER BY id DESC LIMIT 5").format(_qt(t)))
        sample = cur.fetchall()
        notes = cat.get("columnas") or {}
        col_lines = []
        for c, tp in cols.items():
            note = notes.get(c, {})
            note = note.get("descripcion", "") if isinstance(note, dict) else str(note)
            col_lines.append(f"  - {c}: {PG_TO_HUMAN.get(tp, tp)}{' — ' + note if note else ''}")
        txt = (f"Tabla {t} ({n} filas). {cat.get('descripcion') or ''}\n"
               f"Alias: {', '.join(cat.get('alias') or []) or '—'}\nColumnas:\n" + "\n".join(col_lines) +
               f"\nÚltimas filas: {_fmt_rows(sample)}")
        return _result(txt, _card(t, sample, n, highlight=[]) if sample else None)


def create_table(name, columns=None, description="", aliases=None):
    t = ident(name, "nombre de tabla")
    if t.startswith("_"):
        raise DataError("El nombre de la tabla no puede empezar con guion bajo.")
    columns = columns or []
    with _conn() as cur:
        if t in _tables(cur):
            cols = _columns(cur, t)
            return _result(f"La tabla '{t}' ya existe con columnas: "
                           f"{', '.join(c for c in cols if c not in SYSTEM_COLS)}. "
                           "No la volví a crear. Usa db_alter_table para agregar columnas o db_save para registrar datos.")
        defs = [sql.SQL("id BIGSERIAL PRIMARY KEY")]
        notes, seen = {}, set(SYSTEM_COLS)
        for c in columns:
            if isinstance(c, str):
                c = {"name": c}
            cn = ident(c.get("name"), "nombre de columna")
            if cn in seen:
                continue
            seen.add(cn)
            pg_t = _pg_type(c.get("type") or "texto")
            piece = sql.SQL("{} " + pg_t).format(sql.Identifier(cn))
            if c.get("required"):
                piece += sql.SQL(" NOT NULL")
            if c.get("unique"):
                piece += sql.SQL(" UNIQUE")
            d = _default_sql(c.get("default"), pg_t)
            if d is not None:
                piece += sql.SQL(" DEFAULT ") + d
            defs.append(piece)
            notes[cn] = {"descripcion": c.get("description", ""), "tipo": c.get("type") or "texto",
                         "alias": c.get("aliases") or []}
        defs += [sql.SQL("creado_en TIMESTAMPTZ DEFAULT NOW()"), sql.SQL("actualizado_en TIMESTAMPTZ DEFAULT NOW()")]
        cur.execute(sql.SQL("CREATE TABLE {} ({})").format(_qt(t), sql.SQL(", ").join(defs)))
        _touch_catalog(cur, t, description or "", aliases or [], notes)
        _log(cur, t, "crear_tabla", {"columnas": notes, "descripcion": description})
        human = ", ".join(f"{k} ({v['tipo']})" for k, v in notes.items())
        return _result(f"Tabla '{t}' creada. Columnas: {human or '(solo id)'}; además id, creado_en y actualizado_en automáticos.")


def alter_table(table, add_columns=None, rename_columns=None, drop_columns=None, change_types=None,
                rename_to=None, description=None, aliases=None, column_notes=None,
                drop_table=False, confirmed=False):
    with _conn() as cur:
        t = resolve_table(cur, table)
        cols = _columns(cur, t)
        cat = _catalog(cur, t)
        done = []

        # Operaciones destructivas: exigen confirmación explícita del señor
        if (drop_columns or drop_table) and not confirmed:
            cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(_qt(t)))
            n = cur.fetchone()["n"]
            what = f"la tabla completa '{t}' ({n} filas, irá a la papelera)" if drop_table else \
                   f"las columnas {', '.join(drop_columns)} de '{t}' ({n} filas afectadas)"
            return _result(f"NO se hizo nada. Borrar {what} requiere confirmación. Pregunta al señor y, "
                           "si dice que sí, repite la llamada con confirmed=true.")

        if drop_table:
            stamp = time.strftime("%Y%m%d%H%M%S")
            trash = (TRASH_PREFIX + t)[:48] + "_" + stamp
            cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(_qt(t), sql.Identifier(trash)))
            cur.execute(sql.SQL("DELETE FROM {} WHERE tabla=%s").format(_qt(CATALOG)), (t,))
            _log(cur, t, "borrar_tabla", {"papelera": trash, "catalogo": dict(cat) if cat else {}})
            return _result(f"Tabla '{t}' enviada a la papelera como '{trash}'. Se puede recuperar con db_undo.")

        for c in add_columns or []:
            if isinstance(c, str):
                c = {"name": c}
            cn = ident(c.get("name"), "nombre de columna")
            if cn in cols:
                done.append(f"'{cn}' ya existía"); continue
            pg_t = _pg_type(c.get("type") or "texto")
            piece = sql.SQL("ALTER TABLE {} ADD COLUMN {} " + pg_t).format(_qt(t), sql.Identifier(cn))
            d = _default_sql(c.get("default"), pg_t)
            if d is not None:
                piece += sql.SQL(" DEFAULT ") + d
            cur.execute(piece)
            cols[cn] = PG_TO_HUMAN_REV.get(pg_t, pg_t.lower())
            _touch_catalog(cur, t, col_notes={cn: {"descripcion": c.get("description", ""),
                                                   "tipo": c.get("type") or "texto", "alias": c.get("aliases") or []}})
            _log(cur, t, "agregar_columna", {"columna": cn, "tipo": pg_t})
            done.append(f"columna '{cn}' ({c.get('type') or 'texto'}) agregada")

        for r in rename_columns or []:
            old = resolve_column(cols, r.get("from", ""))
            new = ident(r.get("to"), "nombre de columna")
            if old is None:
                raise DataError(f"No existe la columna '{r.get('from')}'.")
            if old in SYSTEM_COLS:
                raise DataError(f"'{old}' es una columna del sistema y no se renombra.")
            cur.execute(sql.SQL("ALTER TABLE {} RENAME COLUMN {} TO {}").format(
                _qt(t), sql.Identifier(old), sql.Identifier(new)))
            cur.execute(sql.SQL("""UPDATE {} SET columnas = (columnas - %s) ||
                                   jsonb_build_object(%s, COALESCE(columnas->%s, '{{}}'::jsonb))
                                   WHERE tabla=%s""").format(_qt(CATALOG)), (old, new, old, t))
            cols[new] = cols.pop(old)
            _log(cur, t, "renombrar_columna", {"de": old, "a": new})
            done.append(f"'{old}' → '{new}'")

        for ch in change_types or []:
            col = resolve_column(cols, ch.get("column", ""))
            if col is None or col in SYSTEM_COLS:
                raise DataError(f"No se puede cambiar el tipo de '{ch.get('column')}'.")
            pg_t = _pg_type(ch.get("type"))
            using = sql.SQL("NULLIF({}::text, '')::" + pg_t).format(sql.Identifier(col))
            cur.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} TYPE " + pg_t + " USING {}").format(
                _qt(t), sql.Identifier(col), using))
            _log(cur, t, "cambiar_tipo", {"columna": col, "de": cols[col], "a": pg_t})
            done.append(f"'{col}' ahora es {ch.get('type')}")

        for dc in drop_columns or []:
            col = resolve_column(cols, dc)
            if col is None:
                done.append(f"'{dc}' no existía"); continue
            if col in SYSTEM_COLS:
                raise DataError(f"'{col}' es una columna del sistema y no se borra.")
            cur.execute(sql.SQL("SELECT id, {} AS v FROM {} WHERE {} IS NOT NULL").format(
                sql.Identifier(col), _qt(t), sql.Identifier(col)))
            backup = {str(r["id"]): r["v"] for r in cur.fetchall()}
            cur.execute(sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(_qt(t), sql.Identifier(col)))
            cur.execute(sql.SQL("UPDATE {} SET columnas = columnas - %s WHERE tabla=%s").format(_qt(CATALOG)), (col, t))
            _log(cur, t, "borrar_columna", {"columna": col, "tipo": cols[col], "valores": backup})
            cols.pop(col)
            done.append(f"columna '{col}' borrada ({len(backup)} valores respaldados en la bitácora)")

        if column_notes:
            _touch_catalog(cur, t, col_notes={ident(k): ({"descripcion": v} if isinstance(v, str) else v)
                                              for k, v in column_notes.items()})
            done.append("notas de columnas actualizadas")
        if description is not None or aliases is not None:
            _touch_catalog(cur, t, description=description if description is not None else cat.get("descripcion"),
                           aliases=aliases if aliases is not None else cat.get("alias"))
            done.append("descripción/alias actualizados")

        if rename_to:
            new_t = ident(rename_to, "nombre de tabla")
            if new_t in _tables(cur):
                raise DataError(f"Ya existe una tabla '{new_t}'.")
            cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(_qt(t), sql.Identifier(new_t)))
            cur.execute(sql.SQL("UPDATE {} SET tabla=%s WHERE tabla=%s").format(_qt(CATALOG)), (new_t, t))
            _log(cur, new_t, "renombrar_tabla", {"de": t, "a": new_t})
            done.append(f"tabla renombrada '{t}' → '{new_t}'")
            t = new_t

        if not done:
            return _result("No se pidió ningún cambio.")
        return _result(f"Tabla '{t}': " + "; ".join(done) + ".")


def save(table, rows, create_missing_columns=True, column_types=None, upsert_on=None):
    """Inserta filas. Si una clave no tiene columna, la crea. upsert_on=['fecha'] actualiza si ya existe."""
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        raise DataError("No hay filas para guardar.")
    with _conn() as cur:
        t = resolve_table(cur, table)
        cols = _columns(cur, t)
        cat_cols = _catalog(cur, t).get("columnas") or {}
        saved, created_all, updated_ids = [], [], []
        for data in rows:
            mapped, created = _ensure_columns(cur, t, cols, data, create_missing_columns, column_types, cat_cols)
            created_all += created
            if not mapped:
                raise DataError("La fila no tiene datos.")
            if upsert_on:
                keys = [resolve_column(cols, k) for k in upsert_on]
                if None in keys or any(k not in mapped for k in keys):
                    raise DataError("upsert_on debe nombrar columnas que vengan en la fila.")
                w = sql.SQL(" AND ").join(sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(k)) for k in keys)
                cur.execute(sql.SQL("SELECT * FROM {} WHERE {} LIMIT 2").format(_qt(t), w),
                            [_adapt(mapped[k]) for k in keys])
                existing = cur.fetchall()
                if len(existing) == 1:
                    old = existing[0]
                    sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in mapped)
                    cur.execute(sql.SQL("UPDATE {} SET {}, actualizado_en = NOW() WHERE id=%s RETURNING *").format(
                        _qt(t), sets), [_adapt(v) for v in mapped.values()] + [old["id"]])
                    row = cur.fetchone()
                    _log(cur, t, "actualizar", {"ids": [old["id"]],
                                                "antes": [{k: old.get(k) for k in mapped}]})
                    saved.append(row); updated_ids.append(old["id"])
                    continue
            cur.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                _qt(t), sql.SQL(", ").join(map(sql.Identifier, mapped)),
                sql.SQL(", ").join(sql.Placeholder() * len(mapped))), [_adapt(v) for v in mapped.values()])
            saved.append(cur.fetchone())
        new_ids = [r["id"] for r in saved if r["id"] not in updated_ids]
        if new_ids:
            _log(cur, t, "insertar", {"ids": new_ids})
        parts = []
        if new_ids:
            parts.append(f"{len(new_ids)} fila(s) nuevas")
        if updated_ids:
            parts.append(f"{len(updated_ids)} actualizada(s) porque ya existían")
        msg = f"Guardado en '{t}': " + " y ".join(parts)
        if created_all:
            msg += f". Columnas nuevas creadas: {', '.join(dict.fromkeys(created_all))}"
        return _result(msg + f". Resultado: {_fmt_rows(saved, 10)}",
                       _card(t, saved, title=f"{t.replace('_', ' ')} · guardado",
                             highlight=[c.split(' ')[0] for c in created_all]))


def find(table, filters=None, search=None, id=None, columns=None, order_by=None, limit=None,
         metrics=None, group_by=None):
    with _conn(readonly=True) as cur:
        t = resolve_table(cur, table)
        cols = _columns(cur, t)
        cat_cols = _catalog(cur, t).get("columnas") or {}
        where, params = _where(cur, t, cols, filters, search, id, cat_cols)
        lim = max(1, min(int(limit or DEFAULT_ROWS), MAX_ROWS))

        # ── Agregados: "promedio de peso por mes", "total de calorías" ──
        if metrics:
            sel, grp, labels = [], [], []
            for g in group_by or []:
                col_name, _, period = str(g).partition(":")
                col = resolve_column(cols, col_name, cat_cols)
                if col is None:
                    raise DataError(f"No existe la columna '{col_name}' para agrupar.")
                if period:
                    per = {"dia": "day", "día": "day", "semana": "week", "mes": "month",
                           "anio": "year", "año": "year", "day": "day", "week": "week",
                           "month": "month", "year": "year"}.get(norm_text(period))
                    if not per:
                        raise DataError("Periodo no válido: usa dia, semana, mes o anio.")
                    expr = sql.SQL("date_trunc({}, {})::date").format(sql.Literal(per), sql.Identifier(col))
                    alias = f"{col}_{norm_text(period)}"
                else:
                    expr, alias = sql.Identifier(col), col
                sel.append(sql.SQL("{} AS {}").format(expr, sql.Identifier(alias)))
                grp.append(expr); labels.append(alias)
            for m in metrics:
                fn = norm_text(m.get("func", "count"))
                fn = {"conteo": "count", "contar": "count", "suma": "sum", "total": "sum", "promedio": "avg",
                      "media": "avg", "minimo": "min", "maximo": "max"}.get(fn, fn)
                if fn not in ("count", "sum", "avg", "min", "max"):
                    raise DataError(f"Función '{m.get('func')}' no válida (count, sum, avg, min, max).")
                if m.get("column") and m.get("column") != "*":
                    col = resolve_column(cols, m["column"], cat_cols)
                    if col is None:
                        raise DataError(f"No existe la columna '{m['column']}'.")
                    arg = sql.Identifier(col)
                    alias = f"{fn}_{col}"
                else:
                    arg, alias = sql.SQL("*"), "count"
                e = sql.SQL(fn + "({})").format(arg)
                if fn == "avg":
                    e = sql.SQL("round(avg({})::numeric, 2)").format(arg)
                sel.append(sql.SQL("{} AS {}").format(e, sql.Identifier(alias)))
            q = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(sel), _qt(t)) + where
            if grp:
                q += sql.SQL(" GROUP BY ") + sql.SQL(", ").join(grp) + \
                     sql.SQL(" ORDER BY ") + sql.SQL(", ").join(grp)
            q += sql.SQL(" LIMIT %s")
            cur.execute(q, params + [lim])
            rows = cur.fetchall()
            return _result(f"Resultado de {t}: {_fmt_rows(rows)}",
                           _card(t, rows, title=f"{t.replace('_', ' ')} · resumen"))

        # ── Consulta normal ──
        if columns:
            chosen = []
            for c in columns:
                rc = resolve_column(cols, c, cat_cols)
                if rc is None:
                    raise DataError(f"No existe la columna '{c}'. Columnas: {', '.join(cols)}.")
                chosen.append(rc)
            if "id" not in chosen:
                chosen.insert(0, "id")
            sel = sql.SQL(", ").join(map(sql.Identifier, chosen))
        else:
            sel = sql.SQL("*")
        cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(_qt(t)) + where, params)
        total = cur.fetchone()["n"]
        order = []
        for o in (order_by if isinstance(order_by, list) else ([order_by] if order_by else [])):
            if isinstance(o, dict):
                cn, desc = o.get("column"), bool(o.get("desc"))
            else:
                bits = str(o).split()
                cn, desc = bits[0], len(bits) > 1 and bits[1].lower() in ("desc", "descendente")
            rc = resolve_column(cols, cn, cat_cols)
            if rc is None:
                raise DataError(f"No existe la columna '{cn}' para ordenar.")
            order.append(sql.SQL("{} {} NULLS LAST").format(sql.Identifier(rc), sql.SQL("DESC" if desc else "ASC")))
        if not order:
            order = [sql.SQL("id DESC")]
        cur.execute(sql.SQL("SELECT {} FROM {}").format(sel, _qt(t)) + where +
                    sql.SQL(" ORDER BY ") + sql.SQL(", ").join(order) + sql.SQL(" LIMIT %s"), params + [lim])
        rows = cur.fetchall()
        if not rows:
            sug = _suggest(cur, t, cols, search)
            txt = f"Sin resultados en '{t}'."
            if sug:
                txt += f" Parecidos: {_fmt_rows(sug)}"
            return _result(txt)
        txt = f"{total} fila(s) en '{t}'" + (f" (mostrando {len(rows)})" if total > len(rows) else "") + \
              f": {_fmt_rows(rows)}"
        return _result(txt, _card(t, rows, total))


def _match_ids(cur, t, cols, filters, search, row_id, cat_cols, limit=51):
    where, params = _where(cur, t, cols, filters, search, row_id, cat_cols)
    cur.execute(sql.SQL("SELECT * FROM {}").format(_qt(t)) + where +
                sql.SQL(" ORDER BY id LIMIT %s"), params + [limit])
    return cur.fetchall()


def update(table, set, id=None, filters=None, search=None, allow_multiple=False,
           create_missing_columns=True, column_types=None):
    if not set:
        raise DataError("No hay nada que cambiar (set vacío).")
    if not _has_criteria(id, filters, search):
        raise DataError("Indica qué fila(s) cambiar: id, filters o search.")
    with _conn() as cur:
        t = resolve_table(cur, table)
        cols = _columns(cur, t)
        cat_cols = _catalog(cur, t).get("columnas") or {}
        matches = _match_ids(cur, t, cols, filters, search, id, cat_cols)
        if not matches:
            sug = _suggest(cur, t, cols, search)
            return _result(f"NO se cambió nada: ninguna fila de '{t}' coincide."
                           + (f" Parecidos: {_fmt_rows(sug)} — pregunta al señor si es alguno." if sug else ""))
        if len(matches) > 1 and not allow_multiple:
            return _result(f"NO se cambió nada: {len(matches)} filas coinciden. Pregunta al señor cuál es "
                           f"(o usa allow_multiple=true si quiere cambiarlas todas): {_fmt_rows(matches, 10)}",
                           _card(t, matches[:10], len(matches), title=f"{t.replace('_', ' ')} · ¿cuál?"))
        mapped, created = _ensure_columns(cur, t, cols, set, create_missing_columns, column_types, cat_cols)
        ids = [m["id"] for m in matches]
        before = [{"id": m["id"], **{k: m.get(k) for k in mapped}} for m in matches]
        sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in mapped)
        cur.execute(sql.SQL("UPDATE {} SET {}, actualizado_en = NOW() WHERE id = ANY(%s) RETURNING *").format(
            _qt(t), sets), [_adapt(v) for v in mapped.values()] + [ids])
        rows = cur.fetchall()
        _log(cur, t, "actualizar", {"ids": ids, "antes": before, "columnas_creadas": created})
        msg = f"{len(rows)} fila(s) actualizadas en '{t}'"
        if created:
            msg += f". Columnas nuevas creadas: {', '.join(created)}"
        return _result(msg + f". Resultado: {_fmt_rows(rows, 10)}",
                       _card(t, rows, title=f"{t.replace('_', ' ')} · actualizado",
                             highlight=list(mapped.keys())))


def delete(table, id=None, filters=None, search=None, allow_multiple=False, confirmed=False):
    if not _has_criteria(id, filters, search):
        raise DataError("Indica qué fila(s) borrar: id, filters o search. (Para borrar la tabla usa db_alter_table con drop_table.)")
    with _conn() as cur:
        t = resolve_table(cur, table)
        cols = _columns(cur, t)
        cat_cols = _catalog(cur, t).get("columnas") or {}
        matches = _match_ids(cur, t, cols, filters, search, id, cat_cols, limit=501)
        if not matches:
            return _result(f"NO se borró nada: ninguna fila de '{t}' coincide.")
        if len(matches) > 1 and not allow_multiple:
            return _result(f"NO se borró nada: {len(matches)} filas coinciden. Pregunta cuál: {_fmt_rows(matches, 10)}",
                           _card(t, matches[:10], len(matches), title=f"{t.replace('_', ' ')} · ¿cuál?"))
        if not confirmed:
            return _result(f"NO se borró nada todavía. Se borrarían {len(matches)} fila(s): {_fmt_rows(matches, 10)}. "
                           "Confirma con el señor y repite con confirmed=true.",
                           _card(t, matches[:10], len(matches), title=f"{t.replace('_', ' ')} · por borrar"))
        ids = [m["id"] for m in matches]
        cur.execute(sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(_qt(t)), (ids,))
        _log(cur, t, "borrar", {"filas": _rows_json(matches)})
        return _result(f"{len(ids)} fila(s) borradas de '{t}' (respaldadas; se pueden recuperar con db_undo).")


def undo(table=None):
    """Deshace la última operación (de una tabla o global) usando la bitácora."""
    with _conn() as cur:
        q = sql.SQL("SELECT * FROM {} WHERE deshecho = FALSE AND operacion <> 'deshacer'").format(_qt(LOG))
        params = []
        if table:
            try:
                t = resolve_table(cur, table)
            except DataError:
                t = ident(table)
            q += sql.SQL(" AND tabla = %s"); params.append(t)
        cur.execute(q + sql.SQL(" ORDER BY id DESC LIMIT 1"), params)
        e = cur.fetchone()
        if not e:
            return _result("No hay nada que deshacer.")
        t, op, d = e["tabla"], e["operacion"], e["detalle"] or {}
        cols = _columns(cur, t)
        if op == "insertar":
            cur.execute(sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(_qt(t)), (d["ids"],))
            msg = f"Deshecho: se quitaron las {len(d['ids'])} fila(s) recién guardadas en '{t}'."
        elif op == "actualizar":
            for b in d.get("antes", []):
                rid = b.get("id") or (d["ids"][0] if len(d.get("ids", [])) == 1 else None)
                vals = {k: v for k, v in b.items() if k != "id" and k in cols}
                if rid is None or not vals:
                    continue
                sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in vals)
                cur.execute(sql.SQL("UPDATE {} SET {} WHERE id=%s").format(_qt(t), sets),
                            [_adapt(v) for v in vals.values()] + [rid])
            msg = f"Deshecho: se restauraron los valores anteriores en '{t}'."
        elif op == "borrar":
            restored = 0
            for row in d.get("filas", []):
                vals = {k: v for k, v in row.items() if k in cols}
                cur.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (id) DO NOTHING").format(
                    _qt(t), sql.SQL(", ").join(map(sql.Identifier, vals)),
                    sql.SQL(", ").join(sql.Placeholder() * len(vals))), [_adapt(v) for v in vals.values()])
                restored += cur.rowcount
            msg = f"Deshecho: {restored} fila(s) recuperadas en '{t}'."
        elif op == "borrar_tabla":
            trash = d["papelera"]
            if t in _tables(cur):
                raise DataError(f"Ya existe otra tabla '{t}'; no puedo restaurar la de la papelera.")
            cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(_qt(trash), sql.Identifier(t)))
            cat = d.get("catalogo") or {}
            _touch_catalog(cur, t, cat.get("descripcion", ""), cat.get("alias") or [], cat.get("columnas") or {})
            msg = f"Deshecho: la tabla '{t}' salió de la papelera."
        elif op == "borrar_columna":
            col = d["columna"]
            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} " + _pg_type_from_info(d.get("tipo"))).format(
                _qt(t), sql.Identifier(col)))
            for rid, v in (d.get("valores") or {}).items():
                cur.execute(sql.SQL("UPDATE {} SET {} = %s WHERE id=%s").format(_qt(t), sql.Identifier(col)),
                            (_adapt(v), int(rid)))
            msg = f"Deshecho: la columna '{col}' volvió con sus valores."
        elif op == "agregar_columna":
            col = d["columna"]
            cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {} WHERE {} IS NOT NULL").format(_qt(t), sql.Identifier(col)))
            if cur.fetchone()["n"]:
                raise DataError(f"La columna '{col}' ya tiene datos; no la quito automáticamente. "
                                "Usa db_alter_table con drop_columns (y confirmación) si de verdad se quiere quitar.")
            cur.execute(sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {}").format(_qt(t), sql.Identifier(col)))
            msg = f"Deshecho: se quitó la columna vacía '{col}'."
        elif op == "renombrar_columna":
            cur.execute(sql.SQL("ALTER TABLE {} RENAME COLUMN {} TO {}").format(
                _qt(t), sql.Identifier(d["a"]), sql.Identifier(d["de"])))
            msg = f"Deshecho: la columna volvió a llamarse '{d['de']}'."
        elif op == "renombrar_tabla":
            cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(_qt(d["a"]), sql.Identifier(d["de"])))
            cur.execute(sql.SQL("UPDATE {} SET tabla=%s WHERE tabla=%s").format(_qt(CATALOG)), (d["de"], d["a"]))
            msg = f"Deshecho: la tabla volvió a llamarse '{d['de']}'."
        elif op == "crear_tabla":
            cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(_qt(t)))
            if cur.fetchone()["n"]:
                raise DataError(f"La tabla '{t}' ya tiene datos; para quitarla usa drop_table con confirmación.")
            cur.execute(sql.SQL("DROP TABLE {}").format(_qt(t)))
            cur.execute(sql.SQL("DELETE FROM {} WHERE tabla=%s").format(_qt(CATALOG)), (t,))
            msg = f"Deshecho: se quitó la tabla vacía '{t}'."
        else:
            return _result(f"La operación '{op}' no se puede deshacer automáticamente.")
        cur.execute(sql.SQL("UPDATE {} SET deshecho = TRUE WHERE id=%s").format(_qt(LOG)), (e["id"],))
        _log(cur, t, "deshacer", {"bitacora_id": e["id"], "operacion": op})
        return _result(msg)


def _pg_type_from_info(t):
    t = (t or "text").lower()
    rev = {v: k for k, v in PG_TO_HUMAN_REV.items()}
    return rev.get(t, "TEXT")


_SQL_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|"
                            r"reindex|cluster|comment|lock|call|do|execute|prepare|listen|notify|set|reset|"
                            r"pg_sleep|dblink|lo_import|lo_export|pg_read_file|pg_read_binary_file|pg_ls_dir|set_config|"
                            r"pg_terminate_backend|pg_cancel_backend|pg_reload_conf|nextval|setval)\b", re.I)


def run_sql(query):
    """SELECT de solo lectura para análisis complejos (joins, ventanas, CTEs). Transacción READ ONLY."""
    q = (query or "").strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", q, re.I):
        raise DataError("db_sql solo acepta consultas SELECT/WITH. Para escribir usa las otras tools.")
    if ";" in q:
        raise DataError("Solo una consulta a la vez (sin ';').")
    if _SQL_FORBIDDEN.search(re.sub(r"'[^']*'", "''", q)):
        raise DataError("La consulta contiene palabras no permitidas en modo lectura.")
    with _conn(readonly=True) as cur:
        cur.execute(sql.SQL("SELECT set_config('search_path', %s, true)"), (f"{SCHEMA}, public",))
        cur.execute(sql.SQL("SELECT * FROM ({}) AS _q LIMIT %s").format(sql.SQL(q)), (MAX_ROWS + 1,))
        rows = cur.fetchall()
    more = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return _result(f"{len(rows)}{'+' if more else ''} fila(s): {_fmt_rows(rows, 60)}",
                   _card("consulta", rows, title="consulta") if rows else None)


# ── Contexto para el prompt ──────────────────────────────────────────────────
_prompt_cache = {"at": 0, "txt": ""}


def prompt_block(max_tables=40):
    """Resumen compacto de las tablas del señor para el system prompt (cache 15 s)."""
    if not _ready:
        return ""
    if time.time() - _prompt_cache["at"] < 15:
        return _prompt_cache["txt"]
    try:
        with _conn(readonly=True) as cur:
            tables = _tables(cur)[:max_tables]
            cat = _catalog(cur)
            lines = []
            for t in tables:
                cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(_qt(t)))
                n = cur.fetchone()["n"]
                cols = [f"{c}:{PG_TO_HUMAN.get(tp, tp)}" for c, tp in _columns(cur, t).items() if c not in SYSTEM_COLS]
                meta = cat.get(t) or {}
                al = meta.get("alias") or []
                lines.append(f"  - {t} ({n} filas){' — ' + meta['descripcion'] if meta.get('descripcion') else ''}"
                             f"{' [alias: ' + ', '.join(al) + ']' if al else ''}\n    columnas: {', '.join(cols[:30])}")
        txt = ("\nBASE DE DATOS DEL SEÑOR (tablas que tú administras con las tools db_*):\n" +
               ("\n".join(lines) if lines else "  (todavía no hay tablas)") + "\n")
    except Exception as e:
        print(f"[DATA] prompt_block: {e}")
        txt = ""
    _prompt_cache.update(at=time.time(), txt=txt)
    return txt


def invalidate_cache():
    _prompt_cache["at"] = 0


PROMPT_RULES = (
    "BASE DE DATOS DINÁMICA (tools db_*):\n"
    "- YA tienes acceso real a la base de datos. NUNCA propongas SQL, esquemas ni funciones nuevas "
    "(tipo 'contacts_action') para que el señor las implemente, y NUNCA digas que algo 'no está implementado' "
    "para guardar datos. Si pide una tabla, créala con db_create_table en ese momento; si da datos, guárdalos con "
    "db_save en el mismo turno (si la tabla no existe, créala primero y luego guarda).\n"
    "- Si en el historial dijiste antes que no podías guardar en la base, eso ya no aplica: hazlo ahora.\n"
    "- Tú administras las tablas del señor. Cualquier dato estructurado que quiera guardar, buscar, "
    "cambiar, borrar o analizar (contactos, medidas, inventarios, listas, registros diarios...) va con las tools db_*.\n"
    "- Antes de crear una tabla revisa la lista de arriba: si ya hay una que sirve, úsala; no dupliques. "
    "Nombres en español, snake_case y en plural (contactos, medidas_corporales). Da una descripción y alias útiles.\n"
    "- Tablas de seguimiento (peso, calorías, hábitos, gastos de algo): incluye una columna fecha (tipo fecha, default 'hoy').\n"
    "- Si el señor da un dato para el que no hay columna (ej. una red social nueva), NO pidas permiso: "
    "db_update/db_save crean la columna solos. Usa nombres genéricos y consistentes (instagram, tiktok, linkedin, "
    "cumpleanos) y tipos correctos (numero para medidas, fecha para fechas).\n"
    "- Para ubicar una fila usa search con el nombre o dato que dio el señor. Si hay varias coincidencias, pregunta cuál. "
    "Si no hay ninguna, dilo y ofrece crearla.\n"
    "- Borrar filas, columnas o tablas: primero confirma con el señor; solo entonces repite con confirmed=true.\n"
    "- Si dice 'deshaz', 'eso no' o 'me equivoqué' sobre un cambio de datos, usa db_undo.\n"
    "- Para análisis (promedios, tendencias, totales por mes) usa db_find con metrics/group_by; "
    "para algo más complejo usa db_sql (solo lectura; también puede leer tablas del sistema como public.financial).\n"
    "- Nunca digas que guardaste, creaste o cambiaste algo si la tool no lo confirmó. La interfaz muestra una tabla "
    "con el resultado: no repitas todas las filas, resume lo importante.\n"
)


# ── Tools para Claude ────────────────────────────────────────────────────────
_FILTERS = {"type": "array", "description": "Condiciones AND. op: =, !=, >, >=, <, <=, contiene, empieza, entre, en, es_nulo, no_nulo. "
                                            "Texto se compara sin mayúsculas ni acentos.",
            "items": {"type": "object", "properties": {
                "column": {"type": "string"}, "op": {"type": "string"},
                "value": {"description": "Valor; para 'entre' [desde, hasta]; para 'en' una lista"}},
                "required": ["column"]}}
_SEARCH = {"type": "string", "description": "Texto libre: encuentra filas que contengan TODAS las palabras en cualquier columna "
                                           "(sin acentos ni mayúsculas). Ideal para 'busca a Juan Pérez'."}
_TYPES = "texto, numero, entero, booleano, fecha, fecha_hora, hora, json, email, url, telefono"

TOOLS = [
    {"name": "db_describe",
     "description": "Lista las tablas del señor (sin 'table') o muestra columnas, notas y últimas filas de una tabla.",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"},
         "include_trash": {"type": "boolean", "description": "Mostrar tablas en la papelera"}}}},
    {"name": "db_create_table",
     "description": "Crea una tabla nueva. id, creado_en y actualizado_en se agregan solos. Si ya existe, no hace nada y avisa.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "snake_case en español y plural, ej. 'contactos', 'medidas_corporales'"},
         "description": {"type": "string", "description": "Para qué sirve la tabla (te ayuda a reconocerla después)"},
         "aliases": {"type": "array", "items": {"type": "string"}, "description": "Otras formas en que el señor podría llamarla"},
         "columns": {"type": "array", "items": {"type": "object", "properties": {
             "name": {"type": "string"},
             "type": {"type": "string", "description": f"Uno de: {_TYPES}"},
             "description": {"type": "string"},
             "required": {"type": "boolean"}, "unique": {"type": "boolean"},
             "default": {"description": "Valor por defecto; 'hoy' o 'ahora' para fechas"}},
             "required": ["name"]}}},
         "required": ["name", "columns"]}},
    {"name": "db_alter_table",
     "description": ("Cambia la estructura de una tabla: agregar/renombrar/borrar columnas, cambiar tipos, renombrar la tabla, "
                     "actualizar descripción/alias, o mandarla a la papelera (drop_table). Borrar requiere confirmed=true "
                     "DESPUÉS de que el señor lo confirme."),
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"},
         "add_columns": {"type": "array", "items": {"type": "object", "properties": {
             "name": {"type": "string"}, "type": {"type": "string"}, "description": {"type": "string"},
             "default": {}}, "required": ["name"]}},
         "rename_columns": {"type": "array", "items": {"type": "object", "properties": {
             "from": {"type": "string"}, "to": {"type": "string"}}, "required": ["from", "to"]}},
         "change_types": {"type": "array", "items": {"type": "object", "properties": {
             "column": {"type": "string"}, "type": {"type": "string"}}, "required": ["column", "type"]}},
         "drop_columns": {"type": "array", "items": {"type": "string"}},
         "rename_to": {"type": "string"},
         "description": {"type": "string"},
         "aliases": {"type": "array", "items": {"type": "string"}},
         "column_notes": {"type": "object", "description": "{columna: descripción}"},
         "drop_table": {"type": "boolean"},
         "confirmed": {"type": "boolean"}},
         "required": ["table"]}},
    {"name": "db_save",
     "description": ("Inserta una o varias filas. Si una clave no tiene columna, la columna se crea sola (tipo inferido o column_types). "
                     "upsert_on=['fecha'] actualiza la fila de esa fecha si ya existe en vez de duplicar."),
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"},
         "rows": {"type": "array", "items": {"type": "object"}, "description": "Lista de {columna: valor}"},
         "column_types": {"type": "object", "description": f"Tipo para columnas nuevas, ej. {{'peso':'numero'}}. Tipos: {_TYPES}"},
         "create_missing_columns": {"type": "boolean", "description": "Por defecto true"},
         "upsert_on": {"type": "array", "items": {"type": "string"}}},
         "required": ["table", "rows"]}},
    {"name": "db_find",
     "description": ("Busca y lee filas, o calcula agregados. Con metrics + group_by hace resúmenes "
                     "(ej. metrics=[{func:'avg',column:'peso'}], group_by=['fecha:mes'])."),
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"},
         "search": _SEARCH, "filters": _FILTERS,
         "id": {"type": "integer"},
         "columns": {"type": "array", "items": {"type": "string"}},
         "order_by": {"type": "array", "items": {"type": "string"}, "description": "ej. ['fecha desc']"},
         "limit": {"type": "integer", "description": f"Por defecto {DEFAULT_ROWS}, máximo {MAX_ROWS}"},
         "metrics": {"type": "array", "items": {"type": "object", "properties": {
             "func": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
             "column": {"type": "string"}}, "required": ["func"]}},
         "group_by": {"type": "array", "items": {"type": "string"},
                      "description": "Columnas; para fechas 'columna:dia|semana|mes|anio'"}},
         "required": ["table"]}},
    {"name": "db_update",
     "description": ("Cambia valores de filas existentes ubicadas por id, search o filters. Si una columna de 'set' no existe, "
                     "se crea sola. Si coinciden varias filas y allow_multiple no es true, no cambia nada y devuelve las candidatas."),
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"},
         "set": {"type": "object", "description": "{columna: nuevo_valor}"},
         "id": {"type": "integer"}, "search": _SEARCH, "filters": _FILTERS,
         "allow_multiple": {"type": "boolean"},
         "column_types": {"type": "object"},
         "create_missing_columns": {"type": "boolean"}},
         "required": ["table", "set"]}},
    {"name": "db_delete",
     "description": "Borra filas (ubicadas por id, search o filters). Sin confirmed=true solo muestra lo que se borraría.",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"},
         "id": {"type": "integer"}, "search": _SEARCH, "filters": _FILTERS,
         "allow_multiple": {"type": "boolean"}, "confirmed": {"type": "boolean"}},
         "required": ["table"]}},
    {"name": "db_undo",
     "description": "Deshace el último cambio de datos o estructura (de una tabla si se indica, o el último en general).",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}}},
    {"name": "db_sql",
     "description": (f"Consulta SELECT de solo lectura para análisis complejos (joins, CTEs, funciones de ventana). "
                     f"Tablas del señor en el esquema '{SCHEMA}' (search_path ya incluye {SCHEMA} y public). "
                     "No puede modificar nada."),
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]

TOOL_NAMES = {t["name"] for t in TOOLS}

_HANDLERS = {
    "db_describe": lambda a: describe(a.get("table"), a.get("include_trash", False)),
    "db_create_table": lambda a: create_table(a.get("name"), a.get("columns"), a.get("description", ""), a.get("aliases")),
    "db_alter_table": lambda a: alter_table(
        a.get("table"), a.get("add_columns"), a.get("rename_columns"), a.get("drop_columns"), a.get("change_types"),
        a.get("rename_to"), a.get("description"), a.get("aliases"), a.get("column_notes"),
        bool(a.get("drop_table")), bool(a.get("confirmed"))),
    "db_save": lambda a: save(a.get("table"), a.get("rows"), a.get("create_missing_columns", True),
                              a.get("column_types"), a.get("upsert_on")),
    "db_find": lambda a: find(a.get("table"), a.get("filters"), a.get("search"), a.get("id"), a.get("columns"),
                              a.get("order_by"), a.get("limit"), a.get("metrics"), a.get("group_by")),
    "db_update": lambda a: update(a.get("table"), a.get("set"), a.get("id"), a.get("filters"), a.get("search"),
                                  bool(a.get("allow_multiple")), a.get("create_missing_columns", True),
                                  a.get("column_types")),
    "db_delete": lambda a: delete(a.get("table"), a.get("id"), a.get("filters"), a.get("search"),
                                  bool(a.get("allow_multiple")), bool(a.get("confirmed"))),
    "db_undo": lambda a: undo(a.get("table")),
    "db_sql": lambda a: run_sql(a.get("query")),
}


def handle(tool_name, tool_input):
    """Punto de entrada del dispatcher. Siempre devuelve {"text", "card"}; nunca lanza."""
    try:
        res = _HANDLERS[tool_name](tool_input or {})
        if tool_name not in ("db_find", "db_describe", "db_sql"):
            invalidate_cache()
        return res
    except DataError as e:
        return _result(f"ERROR: {e}")
    except Exception as e:
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        print(f"[DATA] {tool_name} falló: {msg}")
        return _result(f"ERROR de base de datos: {msg}. No se aplicó ningún cambio. Corrige y reintenta.")


# ── Para la API/UI ───────────────────────────────────────────────────────────
def list_tables():
    with _conn(readonly=True) as cur:
        cat = _catalog(cur)
        out = []
        for t in _tables(cur):
            cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(_qt(t)))
            out.append({"table": t, "rows": cur.fetchone()["n"],
                        "description": (cat.get(t) or {}).get("descripcion", ""),
                        "columns": list(_columns(cur, t).keys())})
        return out


def table_rows(table, search=None, limit=100, offset=0):
    with _conn(readonly=True) as cur:
        t = resolve_table(cur, table)
        cols = _columns(cur, t)
        where, params = _where(cur, t, cols, None, search, None)
        cur.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(_qt(t)) + where, params)
        total = cur.fetchone()["n"]
        cur.execute(sql.SQL("SELECT * FROM {}").format(_qt(t)) + where +
                    sql.SQL(" ORDER BY id DESC LIMIT %s OFFSET %s"),
                    params + [max(1, min(int(limit), 1000)), max(0, int(offset))])
        return t, list(cols.keys()), _rows_json(cur.fetchall()), total
