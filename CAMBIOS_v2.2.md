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
