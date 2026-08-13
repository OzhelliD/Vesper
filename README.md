# Vesper — despliegue en Render

## Arquitectura de producción

- Backend: Flask + Gunicorn
- Runtime: Python 3.11
- Base de datos: PostgreSQL mediante `DATABASE_URL`
- Frontend: `static/index.html`
- Health check: `/api/health`
- Un solo Gunicorn worker para evitar duplicar schedulers y procesos background

## Deploy

1. Sube el repositorio a GitHub.
2. En Render: **New → Blueprint** y selecciona el repositorio.
3. Render leerá `render.yaml`.
4. Configura `DATABASE_URL` con tu PostgreSQL.
5. Configura `ANTHROPIC_API_KEY`.
6. Añade las integraciones que uses: Spotify, OpenWeather, Tavily, SerpAPI y Pixabay.
7. Para Spotify, usa como `SPOTIFY_REDIRECT_URI` la URL pública de Render seguida de `/spotify/callback`.

## Keep-alive

El proyecto incluye un keep-alive opcional para Render. Está activado en `render.yaml` y consulta `/api/health` cada 14 minutos.

Es una medida experimental y no debe considerarse una garantía contra las políticas de suspensión del proveedor. Se puede desactivar con:

```text
ENABLE_KEEP_ALIVE=false
```

## Variables

Obligatorias:

```text
DATABASE_URL
ANTHROPIC_API_KEY
```

Opcionales:

```text
OPENWEATHER_API_KEY
OWNER_PIN
SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET
SPOTIFY_REDIRECT_URI
TAVILY_API_KEY
SERPAPI_KEY
PIXABAY_KEY
```

Render proporciona `PORT` y, cuando está disponible, `RENDER_EXTERNAL_URL`.

## Comprobación local

```bash
python -m py_compile main.py
gunicorn main:app --bind 0.0.0.0:8080 --workers 1 --threads 4 --timeout 300 --worker-class gthread
```

Abre `/api/health` para comprobar que el proceso está vivo.
