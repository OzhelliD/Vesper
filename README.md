# 🤖 Vesper — Agente Personal Inteligente
## Versión con Arquitectura Interna DIAN

**Vesper** es tu asistente personal IA especializado en finanzas, rutinas, comunicación y análisis en tiempo real.

**v2.0** — Refactorizado con arquitectura interna DIAN (6 capas desacopladas, reglas sin IF anidados)

---

## 🚀 Características

- **Claude API** como cerebro inteligente
- **PostgreSQL** en Railway para persistencia
- **Clima en tiempo real** (OpenWeather)
- **Spotify integrado** para música
- **Finanzas tracking** (gastos, presupuesto, deuda)
- **Plugins auto-generados** con Python
- **Búsqueda web** (Tavily, SerpAPI)
- **Sin morning routine** (simplificado)

---

## 📦 Despliegue en Railway

### 1. Deploy automático desde GitHub

```bash
git push
# Railway desplegará automáticamente
```

### 2. Configurar variables en Railway

```
DATABASE_URL          → PostgreSQL (Railway proporciona)
ANTHROPIC_API_KEY     → sk-ant-... (obtener en console.anthropic.com)
```

### 3. Verificar

```bash
curl https://tu-app.railway.app/api/health
```

---

## 🛠️ Local Development

```bash
# Setup
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Editar .env

# Run
python main.py
# http://localhost:8080
```

---

## 📋 Variables Requeridas

**Obligatorias:**
- `DATABASE_URL` → PostgreSQL
- `ANTHROPIC_API_KEY` → Claude API

**Opcionales:**
- `OPENWEATHER_API_KEY` → Clima
- `SPOTIFY_CLIENT_ID/SECRET` → Música
- `OWNER_PIN` → Seguridad

Ver `.env.example` para más detalles.

---

## ✅ Cambios en v2.0

✅ Morning routine eliminada  
✅ Arquitectura DIAN interna (6 capas)  
✅ Reglas sin IF anidados  
✅ Todos los demás features intactos  

---

## 📞 Documentación

- [Railway Docs](https://docs.railway.app)
- [Anthropic API](https://docs.anthropic.com)
- [Flask](https://flask.palletsprojects.com)

---

**Vesper © 2024** | Agente Personal de Ozhelli
