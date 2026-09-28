# Vesper v2.1: cambios

## 1. Interfaz con modo Voz / Texto
- Botón **Voz | Texto** en el encabezado. Vesper recuerda el último modo que usaste.
- **Voz:** círculo más chico (180 px en escritorio, 140 px en celular), la última respuesta en una tarjeta que se lee bien y el micrófono grande. Las respuestas se leen en voz alta.
- **Texto:** el círculo desaparece y la conversación se ve como un chat: tus mensajes a la derecha y las respuestas con formato (negritas, listas y tablas). Al abrir la app carga los últimos 40 mensajes.
- La caja para escribir y adjuntar aparece en los dos modos. Tocar el micrófono corta a Vesper si está hablando.
- Se quitaron el panel de "Comandos recientes" (tenía porcentajes aleatorios), el cajón de historial y el código de la rutina de despertar, que consultaba un endpoint que ya no existe cada 10 s.

## 2. Celular
- Altura real de la pantalla (`100dvh`), caja de escribir fija abajo aunque salga el teclado y márgenes para el notch.
- Letra de 16 px en la caja de escribir, para que iPhone no haga zoom.
- En celular, Enter hace salto de línea y se envía con el botón. En escritorio, Enter envía.
- El panel derecho (Spotify, finanzas, outfits) solo se muestra en pantallas de más de 1100 px, y en celular no se descarga.

## 3. Adjuntos (`attachments.py`)
- Hasta 5 archivos por mensaje. Se pueden adjuntar con el clip, arrastrarlos o pegarlos.
- Fotos → la foto se reduce a 1600 px en el navegador antes de enviarse. Claude ve la imagen.
- PDF → se envía como documento; Claude lee el texto y también las tablas y gráficas.
- Word (.docx) y Excel (.xlsx) → se extrae el texto en el servidor.
- CSV, TXT, MD, JSON → texto.
- .doc/.xls antiguos y fotos HEIC → Vesper avisa cómo convertirlos.

## 4. Foto de contexto (`context_snapshot.py`)
En cada mensaje se arma y se inyecta al modelo:
- **Temporal:** fecha y hora de Monterrey. Corrige un error: antes se usaba la hora del servidor, que en Railway está en UTC. También indica si es oficina (lun/mar), home office o fin de semana.
- **Espacial:** GPS del navegador → dirección con OpenStreetMap (gratis, sin llave). Si vas en movimiento, calcula velocidad y rumbo.
- **Personal inmediato:** cuánto tiempo pasó desde tu último mensaje, próximos recordatorios y alarmas activas.
- **Ambiental:** qué suena en Spotify y el clima donde estás.
- Todas las consultas externas usan caché y corren en paralelo.
- En modo voz el modelo responde corto y sin formato. En texto puede extenderse y usar tablas.

## 5. Preferencias por conteo + confirmación (`preferences.py`)
- Después de cada respuesta, en segundo plano, se detectan "señales" de preferencia en el último intercambio.
- Cada señal cuenta 1 si pasaron al menos 20 min desde la anterior; así varios panes en la misma visita al súper cuentan como una sola vez.
- A las **3** veces, Vesper te pregunta (máximo una vez cada 12 h) si quieres guardarla como regla fija.
- Sí → queda como **REGLA FIJA** y la toma en cuenta en cada respuesta. No → no la vuelve a contar ni a preguntar.
- Puedes ajustar los valores con las variables `PREF_THRESHOLD`, `PREF_MIN_GAP_MIN` y `PREF_ASK_COOLDOWN_H`.
- Para revisar lo que está aprendiendo: `GET /api/preferences` con el header `X-Vesper-Pin`.
- Además, la extracción de memoria ahora corre en un hilo aparte y ya no retrasa la respuesta.

## Variables nuevas (todas opcionales)
| Variable | Default | Para qué |
|---|---|---|
| `VESPER_TZ` | America/Monterrey | Zona horaria |
| `VESPER_OFFICE_DAYS` | 0,1 | Días de oficina (0 = lunes) |
| `VESPER_OFFICE_HOURS` | 8:00 a 15:00 | Horario presencial |
| `PREF_THRESHOLD` | 3 | Veces antes de preguntar |
| `PREF_MIN_GAP_MIN` | 20 | Minutos entre conteos |
| `PREF_ASK_COOLDOWN_H` | 12 | Horas entre preguntas |

## Dependencias nuevas
`python-docx`, `openpyxl`, `tzdata` (ya están en requirements.txt). La tabla `preference_candidates` se crea sola al arrancar.

## 6. Finanzas y outfits como tarjetas (ya no hay paneles fijos)
- Se quitaron los paneles de Finanzas y Outfits de la barra derecha. En pantallas grandes, esa barra ahora solo muestra Spotify y el Estado del día.
- **Finanzas:** cuando Vesper usa `financial_action` (preguntas cómo vas, registras un gasto o un abono), aparece una tarjeta dentro de su respuesta con:
  - lo disponible y la deuda del auto, con una barra de lo gastado en el mes;
  - pestañas **Por categoría** y **Movimientos**, con "Ver todos";
  - un botón ↻ para actualizar, que usa `GET /api/financial/summary` y pide PIN;
  - botones rápidos: **+ Gasto**, **+ Abono al auto** y **Analizar**.
- Se corrigió un error del panel viejo: restaba los gastos dos veces. `monthly_budget` ya es lo disponible, porque el backend le resta cada gasto.
- **Outfits:** hay una tool nueva, `show_outfits`. Cuando pides ideas de ropa, aparece una galería. Al tocar una foto se abre en grande con el botón **Más como este**, y el botón **↻ Otro estilo** busca otra tanda.
- En modo voz, la tarjeta aparece debajo de la respuesta.
