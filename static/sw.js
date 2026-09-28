// Vesper Service Worker v1
// Corre en background aunque el navegador esté cerrado

const CACHE_NAME = 'vesper-v1';
const POLL_INTERVAL = 45000; // 45 segundos

// ── Instalación ────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  console.log('[SW] Instalado');
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  console.log('[SW] Activado');
  event.waitUntil(clients.claim());
  // Iniciar polling en background
  startBackgroundPolling();
});

// ── Push Notifications del servidor ────────────────────────────────────────
self.addEventListener('push', event => {
  if (!event.data) return;
  const data = event.data.json();
  event.waitUntil(
    self.registration.showNotification(data.title || 'VESPER', {
      body:    data.body || '',
      icon:    '/static/vesper-icon.png',
      badge:   '/static/vesper-badge.png',
      tag:     data.tag || 'vesper-notification',
      data:    data,
      actions: [
        { action: 'open',    title: 'Abrir Vesper' },
        { action: 'dismiss', title: 'Descartar' }
      ],
      vibrate:   [200, 100, 200],
      requireInteraction: data.requireInteraction || false
    })
  );
});

// ── Click en notificación ─────────────────────────────────────────────────
self.addEventListener('notificationclick', event => {
  event.notification.close();
  if (event.action === 'dismiss') return;

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(clientList => {
        // Si Vesper ya está abierto, enfocar
        for (const client of clientList) {
          if (client.url.includes('vesper') || client.url.includes('railway')) {
            return client.focus();
          }
        }
        // Si no está abierto, abrir
        return clients.openWindow('/');
      })
  );
});

// ── Background Sync — polling cuando hay conexión ─────────────────────────
self.addEventListener('sync', event => {
  if (event.tag === 'poll-notifications') {
    event.waitUntil(pollNotifications());
  }
});

// ── Polling en background ─────────────────────────────────────────────────
async function pollNotifications() {
  try {
    const r = await fetch('/api/notifications');
    if (!r.ok) return;
    const notifs = await r.json();
    if (!notifs || notifs.length === 0) return;

    for (const notif of notifs) {
      const label = notif.label || '';
      const type  = notif.type  || 'notification';

      // Determinar título y urgencia por tipo
      let title   = 'VESPER';
      let urgent  = false;

      if (type.includes('outlook') || type.includes('webhook_outlook')) {
        title  = '📧 Correo CEMEX';
        urgent = label.toLowerCase().includes('urgent') ||
                 label.toLowerCase().includes('importante');
      } else if (type.includes('teams') || type.includes('webhook_teams')) {
        title = '💬 Teams';
      } else if (type === 'alarm') {
        title  = '⏰ Alarma';
        urgent = true;
      } else if (type === 'reminder') {
        title = '🔔 Recordatorio';
      }

      await self.registration.showNotification(title, {
        body:    label.replace(/^[📧💬⏰🔔📞]\s*/u, ''),
        icon:    '/static/vesper-icon.png',
        tag:     `vesper-${type}-${Date.now()}`,
        data:    notif,
        vibrate: urgent ? [300, 100, 300, 100, 300] : [200, 100, 200],
        requireInteraction: urgent
      });
    }
  } catch (e) {
    console.log('[SW] Error polling:', e);
  }
}

// ── Iniciar polling periódico ─────────────────────────────────────────────
function startBackgroundPolling() {
  // Registrar sync periódico si está disponible
  if (self.registration.periodicSync) {
    self.registration.periodicSync.register('poll-notifications', {
      minInterval: 60 * 1000 // mínimo 1 minuto (Chrome lo limita)
    }).catch(() => {});
  }
  // Fallback: setInterval dentro del SW
  setInterval(pollNotifications, POLL_INTERVAL);
}

// Escuchar mensajes del frontend
self.addEventListener('message', event => {
  if (event.data === 'POLL_NOW') {
    pollNotifications();
  }
  if (event.data === 'START_POLLING') {
    startBackgroundPolling();
  }
});
