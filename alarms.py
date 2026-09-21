# ── SISTEMA DE ALARMAS INTELIGENTES ──────────────────────────────────────────
# Alarmas con música elegida del historial de solicitudes del usuario

import datetime
import json
import random
from psycopg2.extras import RealDictCursor

class AlarmManager:
    def __init__(self, db_pool):
        self.pool = db_pool
        self.active_alarms = {}
        self.init_table()
    
    def init_table(self):
        """Crear tabla de alarmas si no existe"""
        try:
            conn = self.pool.getconn()
            with conn.cursor() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS alarms (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        alarm_time VARCHAR(5) NOT NULL,
                        is_recurring BOOLEAN DEFAULT FALSE,
                        is_active BOOLEAN DEFAULT TRUE,
                        selected_song TEXT,
                        created_at TIMESTAMP DEFAULT NOW(),
                        triggered_at TIMESTAMP
                    )
                """)
                conn.commit()
            self.pool.putconn(conn)
        except Exception as e:
            print(f"[ALARM] Error init table: {e}")
            self.pool.putconn(conn)
    
    def get_song_from_history(self, user_id):
        """Analiza historial de chat y elige una canción inteligente"""
        try:
            conn = self.pool.getconn()
            with conn.cursor(cursor_factory=RealDictCursor) as c:
                # Busca últimos 100 mensajes del usuario
                c.execute("""
                    SELECT content FROM chat_history 
                    WHERE user_id=%s AND role='user'
                    ORDER BY created_at DESC LIMIT 100
                """, (user_id,))
                messages = c.fetchall()
            self.pool.putconn(conn)
            
            # Palabras clave para detectar menciones de canciones
            music_keywords = ['canción', 'música', 'tema', 'cantar', 'artista', 
                            'album', 'rock', 'jazz', 'pop', 'reggaeton', 'trap',
                            'spotify', 'play', 'reproducir']
            
            songs_mentioned = []
            for msg in messages:
                content = msg['content'].lower() if isinstance(msg, dict) else msg.lower()
                if any(kw in content for kw in music_keywords):
                    songs_mentioned.append(msg['content'] if isinstance(msg, dict) else msg)
            
            # Elige una canción aleatoria del historial
            if songs_mentioned:
                choice = random.choice(songs_mentioned[:20])
                # Extrae un nombre de canción (primeros 50 chars)
                song_name = choice[:80].strip('.,!?')
                return song_name if song_name else "Morning Energy Mix"
            
            # Default: canción energética para despertar
            return "Wake Up - Energy Mix"
        except Exception as e:
            print(f"[ALARM] Error getting song: {e}")
            return "Morning Energy Mix"
    
    def create_alarm(self, user_id, alarm_time, is_recurring=False):
        """Crea una nueva alarma
        
        Args:
            user_id: ID del usuario
            alarm_time: Hora en formato "HH:MM"
            is_recurring: Si es diaria (True) o una sola vez (False)
        
        Returns:
            Dict con resultado de la operación
        """
        try:
            song = self.get_song_from_history(user_id)
            conn = self.pool.getconn()
            with conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute("""
                    INSERT INTO alarms (user_id, alarm_time, is_recurring, selected_song, is_active)
                    VALUES (%s, %s, %s, %s, TRUE)
                    RETURNING id, alarm_time, selected_song, is_recurring
                """, (user_id, alarm_time, is_recurring, song))
                alarm = c.fetchone()
                conn.commit()
            self.pool.putconn(conn)
            
            return {
                "success": True,
                "alarm_id": alarm['id'],
                "time": alarm['alarm_time'],
                "song": alarm['selected_song'],
                "recurring": alarm['is_recurring'],
                "message": f"Alarma programada para las {alarm['alarm_time']} con música: {alarm['selected_song']}"
            }
        except Exception as e:
            self.pool.putconn(conn)
            return {"success": False, "error": str(e)}
    
    def list_alarms(self, user_id):
        """Lista todas las alarmas del usuario"""
        try:
            conn = self.pool.getconn()
            with conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute("""
                    SELECT id, alarm_time, is_recurring, selected_song, is_active
                    FROM alarms 
                    WHERE user_id=%s 
                    ORDER BY alarm_time
                """, (user_id,))
                alarms = c.fetchall()
            self.pool.putconn(conn)
            
            return {
                "success": True,
                "alarms": [dict(a) for a in alarms]
            }
        except Exception as e:
            self.pool.putconn(conn)
            return {"success": False, "error": str(e)}
    
    def delete_alarm(self, alarm_id, user_id):
        """Elimina una alarma"""
        try:
            conn = self.pool.getconn()
            with conn.cursor() as c:
                c.execute("""
                    DELETE FROM alarms 
                    WHERE id=%s AND user_id=%s
                """, (alarm_id, user_id))
                conn.commit()
            self.pool.putconn(conn)
            return {"success": True, "message": "Alarma eliminada"}
        except Exception as e:
            self.pool.putconn(conn)
            return {"success": False, "error": str(e)}
    
    def trigger_alarm(self, alarm_id):
        """Marca una alarma como disparada (suena)"""
        try:
            conn = self.pool.getconn()
            with conn.cursor() as c:
                c.execute("""
                    UPDATE alarms 
                    SET triggered_at=NOW()
                    WHERE id=%s
                """, (alarm_id,))
                conn.commit()
            self.pool.putconn(conn)
            self.active_alarms[alarm_id] = datetime.datetime.now()
            return {"success": True}
        except Exception as e:
            self.pool.putconn(conn)
            return {"success": False, "error": str(e)}
    
    def stop_alarm(self, alarm_id):
        """Detiene una alarma activa"""
        try:
            if alarm_id in self.active_alarms:
                del self.active_alarms[alarm_id]
            conn = self.pool.getconn()
            with conn.cursor() as c:
                c.execute("""
                    UPDATE alarms 
                    SET is_active=FALSE
                    WHERE id=%s
                """, (alarm_id,))
                conn.commit()
            self.pool.putconn(conn)
            return {"success": True, "message": "Alarma detenida"}
        except Exception as e:
            self.pool.putconn(conn)
            return {"success": False, "error": str(e)}
    
    def snooze_alarm(self, alarm_id, user_id, minutes=5):
        """Pospone una alarma"""
        try:
            # Calcula la nueva hora
            now = datetime.datetime.now()
            new_time = (now + datetime.timedelta(minutes=minutes)).strftime("%H:%M")
            
            # Detiene la actual
            self.stop_alarm(alarm_id)
            
            # Crea una nueva para la hora pospuesta
            result = self.create_alarm(user_id, new_time, is_recurring=False)
            if result["success"]:
                return {
                    "success": True,
                    "message": f"Alarma pospuesta {minutes} minutos. Nueva hora: {new_time}"
                }
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def check_alarms(self):
        """Verifica si alguna alarma debe sonar (ejecutar cada segundo)
        
        Retorna lista de alarmas que deben sonar
        """
        try:
            now = datetime.datetime.now()
            current_time = now.strftime("%H:%M")
            
            conn = self.pool.getconn()
            with conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute("""
                    SELECT id, user_id, alarm_time, selected_song, is_recurring
                    FROM alarms
                    WHERE is_active=TRUE
                    AND alarm_time=%s
                    AND triggered_at IS NULL
                """, (current_time,))
                alarmas_por_sonar = c.fetchall()
            self.pool.putconn(conn)
            
            return [dict(a) for a in alarmas_por_sonar] if alarmas_por_sonar else []
        except Exception as e:
            self.pool.putconn(conn)
            print(f"[ALARM] Error checking alarms: {e}")
            return []
