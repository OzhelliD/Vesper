"""
web_media.py — Búsqueda web con fuentes enlazadas e imágenes (v2.2)

web_search devuelve:
  • text: resultados numerados [1], [2]… con su URL, para que Vesper cite.
  • card: tarjeta "web" para la UI con las fuentes (enlaces) y, si Vesper lo decidió
          (images=true), imágenes que llevan a la página de donde salieron.

Imágenes, en orden de preferencia (todas con enlace):
  1. SerpAPI Google Images (si hay SERPAPI_KEY): miniatura estable + enlace a la página.
  2. Tavily (TAVILY_API_KEY) cuyo sitio coincide con una fuente → enlace al artículo.
  3. Wikipedia (gratis), solo artículos cuyo título coincide con la búsqueda.
  4. Resto de Tavily → enlace a la portada del sitio.
Solo se aceptan URLs https (evita contenido mixto en la app).
"""

import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

TAVILY_KEY  = os.environ.get("TAVILY_API_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
UA = {"User-Agent": "Vesper/2.2 (asistente personal; contacto: owner)"}
MAX_IMAGES  = 6
MAX_SOURCES = 6

_pool = ThreadPoolExecutor(max_workers=4)


def _domain(url):
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def _base(domain):
    """img.marca.com → marca.com ; cdn.x.com.mx → x.com.mx"""
    parts = (domain or "").split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "org", "net", "gob", "edu", "co") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _https(url):
    return isinstance(url, str) and url.startswith("https://")


# ── Texto + fuentes ──────────────────────────────────────────────────────────
def _tavily(query, want_images):
    if not TAVILY_KEY:
        return [], []
    try:
        r = requests.post("https://api.tavily.com/search", timeout=12, json={
            "api_key": TAVILY_KEY, "query": query, "max_results": 5, "search_depth": "basic",
            "include_images": bool(want_images), "include_image_descriptions": bool(want_images)})
        if r.status_code != 200:
            print(f"[TAVILY] {r.status_code} {r.text[:120]}")
            return [], []
        d = r.json()
        sources = [{"title": x.get("title") or _domain(x.get("url", "")), "url": x.get("url", ""),
                    "domain": _domain(x.get("url", "")), "snippet": (x.get("content") or "")[:400]}
                   for x in d.get("results", []) if x.get("url")]
        imgs = []
        for im in d.get("images") or []:
            url = im.get("url") if isinstance(im, dict) else im
            desc = im.get("description", "") if isinstance(im, dict) else ""
            if _https(url):
                imgs.append({"img": url, "thumb": url, "title": desc[:140], "source": _domain(url), "link": None})
        return sources, imgs
    except Exception as e:
        print(f"[TAVILY] {e}")
        return [], []


def _ddg(query):
    try:
        d = requests.get("https://api.duckduckgo.com/", timeout=8,
                         params={"q": query, "format": "json", "no_html": 1}).json()
        txt, url = d.get("AbstractText", ""), d.get("AbstractURL", "")
        if not txt and d.get("RelatedTopics"):
            first = d["RelatedTopics"][0]
            if isinstance(first, dict):
                txt, url = first.get("Text", ""), first.get("FirstURL", "")
        if txt:
            return [{"title": d.get("Heading") or query, "url": url, "domain": _domain(url), "snippet": txt[:400]}]
    except Exception as e:
        print(f"[DDG] {e}")
    return []


# ── Imágenes ─────────────────────────────────────────────────────────────────
def _serpapi_images(query):
    if not SERPAPI_KEY:
        return []
    try:
        r = requests.get("https://serpapi.com/search", timeout=10, params={
            "engine": "google_images", "q": query, "api_key": SERPAPI_KEY, "hl": "es", "safe": "active"})
        if r.status_code != 200:
            return []
        out = []
        for it in r.json().get("images_results", [])[:12]:
            thumb, link = it.get("thumbnail"), it.get("link")
            if _https(thumb) and link:
                big = it.get("original") if _https(it.get("original")) else thumb
                out.append({"img": big, "thumb": thumb, "title": (it.get("title") or "")[:140],
                            "source": it.get("source") or _domain(link), "link": link})
        return out
    except Exception as e:
        print(f"[SERPAPI] {e}")
        return []


def _wikipedia_images(query, lang="es"):
    try:
        r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", headers=UA, timeout=8, params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrlimit": 6,
            "prop": "pageimages|info", "piprop": "thumbnail", "pithumbsize": 640, "inprop": "url"})
        pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        out = []
        for p in pages:
            th = (p.get("thumbnail") or {}).get("source")
            if _https(th) and not th.lower().endswith((".svg.png",)) and p.get("fullurl"):
                out.append({"img": th, "thumb": th, "title": p.get("title", ""),
                            "source": f"Wikipedia ({lang})", "link": p["fullurl"]})
        return out
    except Exception as e:
        print(f"[WIKI] {e}")
        return []


_STOP = {"de", "la", "el", "los", "las", "y", "en", "del", "con", "para", "por", "un", "una", "que", "como",
         "the", "of", "and", "in", "to", "a", "fisico", "físico", "foto", "fotos", "imagen", "imagenes", "imágenes"}


def _words(text):
    import unicodedata, re
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 2 and w not in _STOP}


def _pick_images(query, tavily_imgs, sources):
    """Orden (sin SerpAPI):
       1. Imágenes de Tavily cuyo sitio coincide con una fuente → enlazan al artículo.
       2. Wikipedia, solo artículos cuyo título comparte palabras clave con la búsqueda.
       3. Resto de Tavily → enlazan a la portada del sitio que aloja la imagen."""
    imgs = _serpapi_images(query)
    if len(imgs) < 4:
        by_domain = {}
        for s in sources:
            by_domain.setdefault(_base(s["domain"]), s)
        matched, loose = [], []
        for im in tavily_imgs:
            src = by_domain.get(_base(im["source"]))
            if src:
                im.update(link=src["url"], source=src["domain"])
                matched.append(im)
            else:
                im["link"] = f"https://{_base(im['source'])}"
                loose.append(im)
        imgs += matched
        if len(imgs) < 4:
            q = _words(query)
            wiki = _wikipedia_images(query, "es") or _wikipedia_images(query, "en")
            need = min(2, len(q)) or 1
            imgs += [w for w in wiki if len(q & _words(w["title"])) >= need][:3]
        imgs += loose
    seen, out = set(), []
    for im in imgs:
        if im["img"] in seen:
            continue
        seen.add(im["img"])
        out.append(im)
    return out[:MAX_IMAGES]


# ── Entrada principal ────────────────────────────────────────────────────────
def search(query, images=False, image_query=None):
    """Devuelve {"text": str para el modelo, "card": dict | None para la UI}."""
    query = (query or "").strip()
    if not query:
        return {"text": "Búsqueda vacía.", "card": None}
    print(f"[WEB_SEARCH] '{query}' imágenes={bool(images)}")
    sources, tav_imgs = _tavily(query, images)
    if not sources:
        sources = _ddg(query)
    imgs = []
    if images:
        imgs = _pick_images(image_query or query, tav_imgs, sources)

    if sources:
        text = "\n\n".join(f"[{i}] {s['title']} ({s['domain']}) {s['url']}\n{s['snippet']}"
                           for i, s in enumerate(sources, 1))
    else:
        text = "Sin resultados de texto."
    if images:
        text += (f"\n\n(La interfaz muestra {len(imgs)} imágenes con enlace a su fuente.)" if imgs
                 else "\n\n(No se encontraron imágenes útiles.)")
    text += ("\n\nLa interfaz ya muestra las fuentes como enlaces: no pegues URLs; "
             "si citas, usa el número [n] o el nombre del sitio.")

    card = None
    if sources or imgs:
        card = {"type": "web", "query": query,
                "sources": [{k: s[k] for k in ("title", "url", "domain")} for s in sources if s.get("url")][:MAX_SOURCES],
                "images": imgs}
    return {"text": text, "card": card}


def merge_cards(a, b):
    """Varias búsquedas en un turno → una sola tarjeta sin duplicados."""
    if not a:
        return b
    seen_u = {s["url"] for s in a["sources"]}
    seen_i = {i["img"] for i in a["images"]}
    a["sources"] += [s for s in b["sources"] if s["url"] not in seen_u]
    a["images"] += [i for i in b["images"] if i["img"] not in seen_i]
    a["sources"] = a["sources"][:MAX_SOURCES]
    a["images"] = a["images"][:MAX_IMAGES]
    return a


TOOL = {
    "name": "web_search",
    "description": ("Busca información actualizada en internet. Devuelve resultados numerados con su fuente; la interfaz "
                    "muestra las fuentes como enlaces. Pon images=true SOLO cuando ver imágenes aporte de verdad "
                    "(personas, lugares, productos, platillos, animales, ejercicios, obras, noticias visuales). "
                    "No para precios, marcadores, clima, horarios ni datos."),
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string"},
        "images": {"type": "boolean", "description": "Mostrar imágenes con enlace a su fuente"},
        "image_query": {"type": "string",
                        "description": "Opcional: búsqueda de imágenes más precisa, ej. 'Giuliano Simeone Atlético de Madrid'"}},
        "required": ["query"]},
}
