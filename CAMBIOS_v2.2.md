# Vesper v2.2: base de datos dinámica

Vesper ahora administra tablas propias a partir de lo que le dices, sin que tengas que definir nada en el código. Decide solo si debe crear una tabla, agregar una columna, guardar, buscar, cambiar, borrar o calcular.

## Qué puedes decirle
| Dices | Vesper hace |
|---|---|
| "Crea una tabla de contactos con nombre, teléfono, correo y cumpleaños" | `db_create_table` → `contactos` (con id, creado_en y actualizado_en automáticos) |
| *(días después)* "Busca a Juan Pérez y agrégale su Instagram @juanp" | `db_update` con `search: "juan perez"`. La columna `instagram` no existía, así que la crea y le pone el valor |
| "Agrégale también su TikTok" / "su LinkedIn" | Lo mismo: cada red social nueva se vuelve una columna |
| "Crea una tabla para medir calorías, peso, % de grasa y % de músculo" | `medidas_corporales` con `fecha` (por defecto hoy), `calorias`, `peso`, `pct_grasa`, `porcentaje_musculo` |
| "Hoy pesé 80.1 y comí 2,000 calorías" | `db_save` con `upsert_on: ["fecha"]`. Si ya había registro de hoy lo actualiza en vez de duplicarlo |
| "¿Cuánto promedié de peso por mes?" | `db_find` con `metrics` + `group_by: ["fecha:mes"]` |
| "¿Cuánto bajé entre cada medición?" | `db_sql` (solo lectura) con funciones de ventana |
| "Borra a Juan Gómez" | Primero te muestra lo que borraría y te pide confirmación. Después lo borra (con respaldo) |
| "Deshaz eso" / "me equivoqué" | `db_undo`: revierte el último cambio (filas, columnas, renombres, tabla borrada) |
| "¿Qué tablas tengo?" | `db_describe` |

## Cómo "entiende" qué tabla usar días después
- En cada mensaje, el prompt incluye la lista de tus tablas con sus columnas, cuántas filas tienen, su descripción y sus alias.
- Si dices "agenda", "contacto" o "medidas", busca el nombre exacto, luego singular/plural, luego los alias y luego una coincidencia parcial única. Por ejemplo, "medidas" encuentra `medidas_corporales`.
- Las búsquedas ignoran mayúsculas y acentos: "juan perez" encuentra "Juan Pérez".
- Si hay varias coincidencias, no cambia nada y te pregunta cuál es. Si no hay ninguna, te muestra filas parecidas.

## Seguridad
- **Tus tablas están aisladas** en el esquema `vesper_data`. Las del sistema (`context_history`, `financial`, `reminders`, etc.) no se pueden alterar desde aquí. Vesper solo puede leerlas con `db_sql`, por ejemplo para cruzar tus gastos con otra tabla.
- **El modelo no escribe SQL de escritura.** Usa herramientas estructuradas y el servidor arma el SQL con identificadores citados y valores parametrizados.
- `db_sql` solo acepta un único `SELECT`/`WITH`, corre en una transacción `READ ONLY` con límite de tiempo (8 s) y de filas (200).
- **Nada se pierde:** borrar filas guarda una copia en `_bitacora`, borrar una columna respalda sus valores y borrar una tabla la mueve a la papelera (`_papelera_<tabla>_<fecha>`). Todo se puede revertir con `db_undo`.
- Borrar filas, columnas o tablas exige `confirmed=true`, y el prompt obliga a Vesper a preguntarte antes.
- Solo el dueño tiene estas tools. Los invitados no.
- Si un dato no cabe en el tipo (por ejemplo "no sé" en una columna de fecha), la operación completa se revierte y Vesper recibe el error para corregirlo.

## Interfaz
- Nueva tarjeta **tabla** dentro de la respuesta:
  - un registro se muestra como ficha (campo → valor);
  - varios registros se muestran como tabla con scroll horizontal en el celular.
- Las columnas recién creadas o modificadas aparecen resaltadas en cian.
- Botones: **Tabla**, que carga la tabla completa, y **CSV**, que la descarga y abre bien en Excel.
- Endpoints nuevos (con el header `X-Vesper-Pin`):
  - `GET /api/data/tables`
  - `GET /api/data/<tabla>?q=&limit=&offset=`
  - `GET /api/data/<tabla>?format=csv`

## Archivos
- **Nuevo:** `data_manager.py`, con todo el motor: catálogo, bitácora, tools, reglas del prompt y funciones para la API.
- **`main.py`:**
  - registra las 9 tools `db_*`;
  - las despacha en orden, para que crear y guardar en el mismo turno no choquen;
  - inyecta el catálogo en el prompt;
  - sube el tope de vueltas de herramientas de 8 a 12;
  - agrega la tarjeta y los endpoints.
- **`static/index.html`:** tarjeta `data_table`.

## Variables nuevas (opcionales)
| Variable | Default | Para qué |
|---|---|---|
| `VESPER_DATA_URL` | *(vacío)* | Usar OTRA base PostgreSQL para tus tablas. Vacío = la misma `DATABASE_URL` |
| `VESPER_DATA_SCHEMA` | `vesper_data` | Esquema donde viven tus tablas |
| `VESPER_DATA_TIMEOUT` | `8s` | Tiempo máximo por consulta |
| `WEBHOOK_SECRET` | *(vacío)* | Secreto del webhook de Outlook/Teams |

No hay dependencias nuevas. El esquema, `_catalogo` y `_bitacora` se crean solos al arrancar.

## Correcciones de paso
- `WEBHOOK_SECRET` se usaba pero nunca se definía, así que `/api/webhook/microsoft` fallaba con `NameError`. Ahora se lee de la variable de entorno.
- El resumen diario de las 23:00 leía las filas como tuplas (`r[0]`), pero son diccionarios. Fallaba siempre.
- Los "patrones detectados" en la memoria se imprimían con los nombres de las columnas en vez de sus valores.

## Tipos de columna que entiende
`texto`, `numero`, `entero`, `booleano`, `fecha`, `fecha_hora`, `hora`, `json`, además de `email`, `url` y `telefono`, que se guardan como texto. Si Vesper crea una columna al vuelo sin indicar el tipo, lo deduce del valor: los números se vuelven `numero`, `"2026-09-28"` se vuelve `fecha` y el resto `texto`.

## Recordatorios duplicados (reporte del 28/09)
- **Problema:** un recordatorio de prueba llegó 3 veces. Railway corre 2 procesos (`--workers 2`) y cada uno tiene su propio revisor de recordatorios, así que los dos lo disparaban. Además, cada aviso se guardaba en la tabla `notifications` y también en una lista en memoria, y `/api/notifications` entregaba los dos.
- **Solución:** cada recordatorio y cada alarma se "reclaman" con `UPDATE ... WHERE status='pending' RETURNING`, así que solo lo dispara el primer proceso que llega. La tabla `notifications` es la única fuente, y la entrega también es atómica (`UPDATE ... SET delivered=TRUE ... RETURNING`), así que aunque tengas la app abierta en el celular y en la compu, cada aviso sale una sola vez.
- Probado con 3 revisores simultáneos y 2 consultas al mismo tiempo: se entregó 1 aviso.

## Recordatorios que Vesper decide cómo preparar
- `set_reminder` tiene un campo nuevo, `prepare`. **Vesper decide** cuándo usarlo:
  - si el recordatorio implica algo que puede preparar (plan de comidas con cantidades, resumen de tus tablas, clima para una salida, noticias), deja escrita la instrucción;
  - para avisos simples ("llamar a mamá") lo deja vacío y el aviso sale como antes.
- Regla nueva en el prompt: si promete contenido ("te aviso con el plan"), está obligado a llenar `prepare`. Así se evita lo que pasó el 28/09, cuando el recordatorio llegó solo con el título.
- Cuando suena, Vesper genera el contenido en ese momento con tu contexto (hora, clima, memorias, preferencias y tus tablas) y puede consultar tus tablas, tus finanzas e internet. **Solo lectura**: no puede modificar nada mientras prepara un recordatorio.
- Se muestra como mensaje completo con formato. En modo voz lo lee entero; en modo texto dice el título y "le dejé el detalle en pantalla".
- Queda guardado en el historial, así que después puedes preguntar "¿cuánto queso dijiste?".
- Si la generación falla, el aviso llega igual con la instrucción original.
- La app revisa avisos cada 20 s (antes cada 45 s) y también al volver a abrir la pestaña.
- Columnas nuevas, creadas solas al arrancar: `reminders.prepare` y `notifications.body`.

## Imágenes y fuentes con enlace
- `web_search` ahora devuelve resultados numerados [1], [2]… con su fuente, y la interfaz los muestra en una tarjeta con enlaces.
- **Vesper decide** cuándo pedir imágenes (`images=true`): personas, lugares, productos, platillos, ejercicios. No las pide para precios, marcadores o clima.
- De dónde salen las imágenes, siempre con enlace a la página de origen:
  1. **SerpAPI** (Google Images), si configuras `SERPAPI_KEY`. Es la mejor opción.
  2. **Wikipedia**: gratis y sin llave; da la imagen del artículo con enlace al artículo.
  3. **Tavily**: la imagen se enlaza al artículo del mismo sitio.
- Cuidados para no romper la interfaz:
  - tira horizontal de miniaturas de tamaño fijo, que se desliza en el celular;
  - si una imagen no carga, se quita sola;
  - solo se aceptan imágenes `https`;
  - las fuentes se muestran en una línea cada una, cortando lo que no cabe, y solo las 3 primeras, con "Ver más".
- Al tocar una imagen se abre en grande con el botón **Abrir fuente ↗**.
- Si en un turno hay varias búsquedas, se juntan en una sola tarjeta.
- Archivo nuevo: `web_media.py`.

## Modo radio: música continua sin crear playlist
**Por qué no un thread:** la música no suena en el servidor de Vesper, suena en tu dispositivo de Spotify (celular, compu o bocina). Vesper le entrega a Spotify una cola de canciones, y Spotify las encadena solo aunque Vesper esté pensando, respondiendo o hablando. Así, una respuesta de Vesper nunca puede cortar la música.

- Tool nueva **`play_music`** para pedidos como "pon música country", "algo para concentrarme" o "más como esta".
  - Vesper elige unas 14 canciones con tus gustos musicales guardados en memoria.
  - Las busca en Spotify en paralelo y las reproduce en orden, sin aleatorio y sin repetir la lista.
  - No crea playlist.
- **El DJ en segundo plano** (`music_radio.tick()`, cada 20 s dentro del scheduler):
  - cuando quedan 2 canciones, elige 8 más del mismo estilo sin repetir ninguna que ya sonó y las agrega a la cola de Spotify;
  - con varios workers, solo uno rellena, porque el relleno se reclama con `UPDATE ... RETURNING`;
  - si pones otra cosa por tu cuenta, la radio se apaga sola;
  - `play_song`, `play_playlist` y `create_playlist` también la apagan.
- **`music_control`**: pausar, reanudar, siguiente, anterior, detener, volumen, `que_suena` (qué suena y qué sigue).
- **Baja la música mientras Vesper habla:** cuando Vesper empieza a hablar, el volumen baja a 25% y regresa al terminar.
  - Solo funciona en dispositivos que permiten cambiar el volumen (compu, bocinas). En iPhone Spotify no lo permite.
  - Si el navegador no avisa que Vesper terminó, el volumen se restaura solo a los 90 s.
- Sin tarjeta en el chat: en escritorio la canción se ve en el "Now playing" del panel derecho y en el celular no se muestra. Vesper confirma en su respuesta qué puso.
- Si no hay ningún Spotify abierto, Vesper te pide abrirlo; basta con abrir la app.
- **Archivo nuevo:** `music_radio.py`. Tabla nueva `music_session`, que se crea sola al arrancar.
- **Endpoints nuevos** (con PIN):
  - `POST /api/music/control` con `{action, volume}`;
  - `POST /api/music/duck` con `{on}`.
- `/spotify/now-playing` ahora incluye `radio: true/false`.
