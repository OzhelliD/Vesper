# Agente Personal Ozhelli — Render.com

## Setup en 5 pasos

### 1. Sube el código a GitHub
Crea un repositorio privado en github.com y sube estos archivos:
```
main.py
requirements.txt
render.yaml
static/
  index.html
```

### 2. Crea cuenta en Render
render.com → Sign up (gratis, con GitHub)

### 3. New Web Service
- Dashboard → New → Web Service
- Conecta tu repositorio de GitHub
- Render detecta automáticamente el render.yaml

### 4. Agrega las variables de entorno
En Render → Environment:
```
ANTHROPIC_API_KEY   = sk-ant-...tu-key-nueva...
OPENWEATHER_API_KEY = (opcional, para clima real)
```

### 5. Agrega el disco persistente
En Render → Disks → Add Disk:
```
Name:       ozhelli-db
Mount Path: /data
Size:       1 GB (gratis en plan Individual)
```

→ Deploy → en 2-3 minutos tienes tu URL pública

---

## Estructura
```
/
├── main.py           ← Backend Flask completo
├── requirements.txt  ← Dependencias + gunicorn
├── render.yaml       ← Config automática de Render
└── static/
    └── index.html    ← Frontend con voz
```

---

## Por qué Render > Replit para este proyecto

| | Render | Replit |
|---|---|---|
| Flask soporte | Nativo con gunicorn | Limitado |
| DB persistente | /data disk incluido | No en free |
| Sleep en free | 15 min inactividad | Sí |
| Logs | Claros y en tiempo real | Ruidosos |
| Deploy desde GitHub | Automático | Manual |

---

## Notas importantes

- **Free tier de Render:** el servicio se duerme después de 15 min sin tráfico.
  Primera request después del sleep tarda ~30 segundos (cold start).
  Para uso personal esto es perfectamente aceptable.

- **La DB persiste en /data:** recordatorios, alarmas, historial y finanzas
  sobreviven reinicios y deploys.

- **Voz:** funciona via Web Speech API del navegador — no requiere nada extra.
