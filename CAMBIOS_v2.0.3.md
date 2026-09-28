# Vesper v2.0.3 - Cambios Implementados

## ✅ Cambios en esta versión

### 1. **Bugs Financieros Corregidos**
- `registrar_gasto`: Ahora resta del presupuesto correctamente
- `separar`: Ahora suma al presupuesto correctamente  
- `ajustar_disponible`: Establece valor absoluto (no suma)

### 2. **Modelo Claude Actualizado**
- Cambio de `claude-sonnet-4-20250514` → `claude-sonnet-4-5`
- Mejor rendimiento y compatibilidad con API

### 3. **Interfaz Actualizada (index.html)**
- ❌ Input de texto removido → Solo voz
- ❌ Sección de Outfits eliminada
- ❌ Estado Financiero oculto (datos en BD se mantienen)
- ✅ Now Playing mantiene
- ✅ Esfera animada de voz mantiene

### 4. **Sistema de Alarmas Inteligentes (NUEVO)**
- Crear alarmas: "Alarma a las 7 AM"
- Música elegida del historial de canciones del usuario
- Controlar por voz: "Detén alarma", "Snooze"
- Persistencia en PostgreSQL
- Alarmas diarias o una sola vez

### 5. **Código Limpiado**
- Removida referencia a `is_morning_hour()` (obsoleta)
- Removido sistema de morning routine
- Sintaxis validada ✅

## 📊 Estadísticas

- **main.py**: 1411 líneas (bugs + modelo + alarmas integrados)
- **alarms.py**: 262 líneas (módulo de alarmas)
- **index.html**: 70 KB (UI optimizada)
- **Base de datos**: 
  - Nueva tabla: `alarms` (para almacenar alarmas)
  - Todas las tablas existentes intactas

## 🚀 Deployment a Railway

1. Git push directamente
2. Railway detectará cambios
3. Auto-redeploy en 2-3 minutos
4. Vars de entorno: ninguna nueva requerida

## 🧪 Testing

```bash
# Validar sintaxis
python -m py_compile main.py
python -m py_compile alarms.py

# Verificar imports
python3 -c "from alarms import AlarmManager; print('✅ OK')"
```

## 📝 Uso de Alarmas

```
Usuario: "Crea una alarma para las 7 de la mañana"
Vesper: Analiza historial → Elige música → Crea alarma

A las 7:00 AM:
- Alarma suena
- Música se reproduce
- Usuario puede: "Detén" o "Snooze"
```

## 🔐 Datos Guardados

Tabla `alarms`:
- id, user_id, alarm_time, is_recurring, is_active
- selected_song, created_at, triggered_at

Todos los datos persisten en PostgreSQL (Railway)

---

**Versión**: v2.0.3
**Fecha**: Septiembre 21, 2026
**Status**: ✅ LISTO PARA PRODUCCIÓN
