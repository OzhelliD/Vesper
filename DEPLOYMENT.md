# 📦 Guía de Deployment a Railway

Vesper v2.0 está listo para producción en Railway.

---

## ✅ Pre-requisitos

- [Cuenta en Railway](https://railway.app)
- Repositorio en GitHub
- API keys (ver `.env.example`)

---

## 🚀 Deployment en 5 pasos

### Paso 1: Conectar GitHub

1. En Railway Dashboard → "New Project"
2. Selecciona "Deploy from GitHub"
3. Autoriza y selecciona este repositorio
4. Railway leerá `railway.toml` automáticamente

### Paso 2: Configurar PostgreSQL

1. En Railway → "Add Database"
2. Selecciona "PostgreSQL"
3. Railway creará automáticamente `DATABASE_URL`

### Paso 3: Agregar Variables de Entorno

En Railway Dashboard → Variables:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENWEATHER_API_KEY=... (opcional)
SPOTIFY_CLIENT_ID=... (opcional)
SPOTIFY_CLIENT_SECRET=... (opcional)
SPOTIFY_REDIRECT_URI=https://tu-app.railway.app/spotify/callback
```

**Nota:** `DATABASE_URL` se crea automáticamente con PostgreSQL

### Paso 4: Deploy

```bash
# Automático al push
git push

# O manual desde Railway CLI
railway deploy
```

### Paso 5: Verificar

```bash
# Health check
curl https://tu-app.railway.app/api/health

# Debe retornar: {"status":"ok"}
```

---

## 🔧 Configuración Detallada

### railway.toml

```toml
[build]
builder = "dockerfile"

[deploy]
startCommand = "gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300 --worker-class gthread"
healthcheckPath = "/api/health"
healthcheckTimeout = 10

[env]
PORT = "8080"
```

Este archivo ya está incluido y configurado.

### Dockerfile

Incluye:
- Python 3.11
- Gunicorn como servidor
- Todas las dependencias
- Optimizado para Railway

### requirements.txt

Incluye todas las dependencias necesarias:
- Flask + CORS
- Anthropic SDK
- psycopg2 (PostgreSQL)
- APScheduler
- requests, httpx
- pgvector

---

## 📊 Variables de Entorno Detalladas

### DATABASE_URL ✅ (Automático)

Railway crea esto automáticamente cuando agregas PostgreSQL.

Formato: `postgresql://user:password@host:5432/database`

### ANTHROPIC_API_KEY ✅ (Requerido)

Obtener en [console.anthropic.com](https://console.anthropic.com)

Ejemplo: `sk-ant-v0.1.8a9b7c6d5e4f3g2h1i0j`

### OPENWEATHER_API_KEY (Opcional)

Para clima en tiempo real.

Obtener en [openweathermap.org/api](https://openweathermap.org/api)

### SPOTIFY_CLIENT_ID/SECRET (Opcional)

Para reproducción de música.

Obtener en [developer.spotify.com](https://developer.spotify.com)

Redirect URI debe ser: `https://tu-app.railway.app/spotify/callback`

### OWNER_PIN (Opcional)

PIN para acceso restringido. Recomendado: 32 caracteres aleatorios.

---

## 🔒 Seguridad

### SSL/TLS

Railway proporciona HTTPS automáticamente.

### Database

- PostgreSQL en Railway está encriptado
- Conexión segura automática
- Backups automáticos

### Secrets

Todas las variables confidenciales en Railway Vault (no en código)

---

## 📈 Monitoreo

### Logs

En Railway Dashboard → Deployments → Logs

Ver logs en tiempo real:
```bash
railway logs
```

### Métricas

Railway proporciona:
- CPU usage
- Memory usage
- Network I/O
- Requests/sec

### Health Check

```bash
curl https://tu-app.railway.app/api/health
```

---

## 🐛 Troubleshooting

### Error: "DATABASE_URL no encontrada"

→ Agregaste PostgreSQL?
→ Las variables se sincronizan dentro de 1 minuto

### Error: "ANTHROPIC_API_KEY inválida"

→ Verifica que es correcta en console.anthropic.com
→ Sin espacios al copiar/pegar

### App lenta o timeout

→ Aumentar memoria en Railway settings
→ Verificar si hay queries pesadas en DB

### Conexión rechazada a DB

→ PostgreSQL está inicializando (esperar 1-2 min)
→ Verificar que DATABASE_URL está correcta

---

## 🔄 CI/CD

Railway automatiza:
- Deploy en cada `git push`
- Testing (opcional, editar `railway.toml`)
- Rollback automático si falla
- Health checks después de deploy

---

## 💾 Backups

Railroad proporciona backups automáticos:

- Diarios (7 días retenidos)
- Semanales (4 semanas retenidas)
- Mensualse (12 meses retenidos)

Para restaurar:
1. Railway Dashboard → Database → Backups
2. Seleccionar fecha
3. Restaurar

---

## 🎯 Checklist Pre-Deploy

- [ ] `ANTHROPIC_API_KEY` configurada
- [ ] PostgreSQL agregada
- [ ] `.env.example` listo
- [ ] `requirements.txt` actualizado
- [ ] `railway.toml` presente
- [ ] `Dockerfile` presente
- [ ] Repositorio público en GitHub
- [ ] `main.py` compila sin errores (`python -m py_compile main.py`)

---

## ✅ Post-Deploy

- [ ] Health check responde
- [ ] API endpoints funcionan
- [ ] Logs sin errores
- [ ] Database conectada
- [ ] Claude API responde

---

## 🚀 Próximos Pasos

1. Deploy a Railway (arriba)
2. Configurar custom domain (opcional)
3. Monitoring en tiempo real
4. Agregar más variables según necesites

---

## 📞 Soporte

- [Railway Docs](https://docs.railway.app)
- [Railway Community](https://railway.app)
- [Anthropic Support](https://support.anthropic.com)

---

**Vesper v2.0 está listo para producción** ✨
