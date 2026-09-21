# ── SISTEMA DE ALARMAS INTELIGENTES v2 ──────────────────────────────────────────
# Alarmas robustas con música, sin duplicados, con verificación correcta de tiempo

import datetime
import json
import random
from psycopg2.extras import RealDictCursor
from datetime import datetime as dt

class AlarmManager:
    def __init__(self, db_pool):
        self.pool = db_pool
        self.last_triggered = {}  # Evitar disparos duplicados {alarm_id: last_trigger_time}
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
                        last_triggered TIMESTAMP
                    )
                """)
                conn.commit()
            self.pool.putconn(conn)
        except Exception as e:
            print(f"[ALARM] Error init table: {e}")
            if conn:
                self.pool.putconn(conn)
    
    def get_song_from_history(self, user_id):
        """Analiza historial de chat y elige una canción inteligente"""
        try:
            conn = self.pool.getconn()
            with conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute("""
                    SELECT content FROM chat_history 
                    WHERE user_id=%s AND role='user'
                    ORDER BY created_at DESC LIMIT 100
                """, (user_id,))
                messages = c.fetchall()
            self.pool.putconn(conn)
            
            music_keywords = ['canción', 'música', 'tema', 'cantar', 'artista', 
                            'album', 'rock', 'jazz', 'pop', 'reggaeton', 'trap',
                            'spotify', 'play', 'reproducir']
            
            songs_mentioned = []
            for msg in messages:
                content = msg['content'].lower() if isinstance(msg, dict) else msg.lower()
                if any(kw in content for kw in music_keywords):
                    songs_mentioned.append(msg['content'] if isinstance(msg, dict) else msg)
            
            if songs_mentioned:
                choice = random.choice(songs_mentioned[:20])
                song_name = choice[:80].strip('.,!?')
                return song_name if song_name else "Musica para despertar"
            
            return "Musica para despertar"
        except Exception as e:
            print(f"[ALARM] Error getting song: {e}")
            return "Musica para despertar"
    
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
            # Validar formato HH:MM
            parts = alarm_time.split(':')
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                return {"success": False, "error": "Formato inválido. Use HH:MM"}
            
            hh, mm = int(parts[0]), int(parts[1])
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                return {"success": False, "error": "Hora inválida"}
            
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
                "message": f"Alarma programada para las {alarm['alarm_time']}"
            }
        except Exception as e:
            if conn:
                self.pool.putconn(conn)
            return {"success": False, "error": str(e)}
    
    def list_alarms(self, user_id):
        """Lista todas las alarmas activas del usuario"""
        try:
            conn = self.pool.getconn()
            with conn.cursor(cursor_factory=RealDictCursor) as c:
                c.execute("""
                    SELECT id, alarm_time, is_recurring, selected_song, is_active
                    FROM alarms 
                    WHERE user_id=%s AND is_active=TRUE
                    ORDER BY alarm_time
                """, (user_id,))
                alarms = c.fetchall()
            self.pool.putconn(conn)
            
            return {
                "success": True,
                "alarms": [dict(a) for a in alarms]
            }
        except Exception as e:
            if conn:
                self.pool.putconn(conn)
            return {"success": False, "error": str(e)}
    
    def delete_alarm(self, alarm_id, user_id):
        """Elimina una alarma"""
        try:
            conn = self.pool.getconn()
            with conn.cursor() as c:
                c.execute("""
                    UPDATE alarms 
                    SET is_active=FALSE
                    WHERE id=%s AND user_id=%s
                """, (alarm_id, user_id))
                conn.commit()
            self.pool.putconn(conn)
            return {"success": True, "message": "Alarma eliminada"}
        except Exception as e:
            if conn:
                self.pool.putconn(conn)
            return {"success": False, "error": str(e)}
    
    def check_alarms(self):
        """Verifica si alguna alarma debe sonar
        
        Returns lista de alarmas que deben sonar EXACTAMENTE en este minuto
        Evita duplicados verificando last_triggered
        """
        try:
            now = dt.now()
            current_time = now.strftime("%H:%M")
            
            conn = self.pool.getconn()
            with conn.cursor(cursor_factory=RealDictCursor) as c:
                # Alarmas activas que coinciden con la hora actual
                c.execute("""
                    SELECT id, user_id, alarm_time, selected_song, is_recurring
                    FROM alarms
                    WHERE is_active=TRUE
                    AND alarm_time=%s
                """, (current_time,))
                
                alarmas_por_sonar = c.fetchall()
            
            self.pool.putconn(conn)
            
            # Filtrar para evitar disparos duplicados en el mismo minuto
            resultado = []
            for alarm in alarmas_por_sonar:
                alarm_id = alarm['id']
                last_time = self.last_triggered.get(alarm_id)
                
                # Si nunca se disparó O pasó más de 1 minuto desde el último disparo
                if last_time is None or (now - last_time).total_seconds() > 60:
                    resultado.append(dict(alarm))
                    self.last_triggered[alarm_id] = now
            
            return resultado
        except Exception as e:
            print(f"[ALARM] Error checking alarms: {e}")
            if conn:
                self.pool.putconn(conn)
            return []
    
    def mark_triggered(self, alarm_id):
        """Marca una alarma como disparada"""
        try:
            conn = self.pool.getconn()
            with conn.cursor() as c:
                c.execute("""
                    UPDATE alarms 
                    SET last_triggered=NOW()
                    WHERE id=%s
                """, (alarm_id,))
                conn.commit()
            self.pool.putconn(conn)
        except Exception as e:
            print(f"[ALARM] Error marking triggered: {e}")
            if conn:
                self.pool.putconn(conn)
