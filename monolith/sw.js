/* SecApp Service Worker - Offline-first form queue */
const CACHE_VERSION = 'secapp-v1';
const DB_NAME = 'secapp-offline';
const DB_VERSION = 1;
const STORE_NAME = 'pending_submissions';

const STATIC_FALLBACKS = ['/forms/offline.html'];

// ── Install ──────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_VERSION)
            .then(cache => cache.addAll(STATIC_FALLBACKS).catch(() => {}))
            .then(() => self.skipWaiting())
    );
});

// ── Activate ─────────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(
                keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k))
            ))
            .then(() => self.clients.claim())
    );
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
    const { request } = event;
    const url = new URL(request.url);

    // Only handle same-origin requests
    if (url.origin !== location.origin) return;

    // Intercept form POST submissions
    if (request.method === 'POST' && /\/forms\/submit_/.test(url.pathname)) {
        event.respondWith(handleFormPost(request));
        return;
    }

    // Network-first with dynamic cache for GET navigation requests
    if (request.method === 'GET' && request.headers.get('accept')?.includes('text/html')) {
        event.respondWith(
            fetch(request)
                .then(response => {
                    if (response.ok) {
                        const clone = response.clone();
                        caches.open(CACHE_VERSION).then(cache => cache.put(request, clone));
                    }
                    return response;
                })
                .catch(() =>
                    caches.match(request)
                        .then(cached => cached || caches.match('/forms/offline.html'))
                )
        );
    }
});

// ── Form POST handler ─────────────────────────────────────────────────────────
async function handleFormPost(request) {
    const requestClone = request.clone();
    try {
        const response = await fetch(request);
        return response;
    } catch {
        // Offline — capture and queue the submission
        const formData = await requestClone.formData();
        const entries = [];
        let hasFiles = false;

        formData.forEach((value, key) => {
            if (key === 'csrf_token') return; // strip; a fresh token is fetched on sync
            if (value instanceof File) {
                hasFiles = true;
            } else {
                entries.push([key, value]);
            }
        });

        const url = new URL(request.url);
        const formType = url.pathname.replace('/forms/submit_', '');

        await queueSubmission({ url: url.pathname, formType, entries, hasFiles, timestamp: Date.now() });

        // Notify any open tab so the banner updates immediately
        const clients = await self.clients.matchAll({ type: 'window' });
        clients.forEach(c => c.postMessage({ type: 'OFFLINE_QUEUED', formType }));

        return new Response(buildOfflineSavedHTML(formType, hasFiles), {
            status: 202,
            headers: { 'Content-Type': 'text/html; charset=utf-8' }
        });
    }
}

// ── Offline-saved confirmation page ──────────────────────────────────────────
const FORM_NAMES = {
    incident_report: 'Reporte de Incidente',
    medicion_experiencia_cliente: 'Encuesta a Cliente',
    supervision_puesto: 'Control de Supervisión',
    informe_novedades_disciplinario: 'Reporte Disciplinario',
    log_de_patrullas: 'Log de Patrullas',
    registro_de_capacitaciones: 'Control de Capacitaciones',
    registro_y_acta_de_visita: 'Acta de Visita a Cliente',
    planilla_vehicular: 'Planilla Vehicular',
    planilla_motocicletas: 'Planilla de Motocicletas',
    checklist_cumplimiento: 'Checklist de Cumplimiento',
    confiabilidad_equipos: 'Confiabilidad de Equipos',
};

function buildOfflineSavedHTML(formType, hasFiles) {
    const name = FORM_NAMES[formType] || formType;
    const fileWarning = hasFiles
        ? `<p style="color:#f6ad55;font-size:.85rem;margin-top:.75rem">
             ⚠️ Los archivos adjuntos no pudieron guardarse offline.
             Deberá adjuntarlos nuevamente al sincronizar.
           </p>`
        : '';
    return `<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Guardado sin conexión – SecApp</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:sans-serif;background:#1a202c;color:#e2e8f0;
         display:flex;align-items:center;justify-content:center;
         min-height:100vh;padding:1.5rem}
    .card{background:#2d3748;border-radius:12px;padding:2rem 2.5rem;
          max-width:440px;width:100%;text-align:center;
          box-shadow:0 8px 30px rgba(0,0,0,.5)}
    .icon{font-size:3rem;margin-bottom:1rem}
    h1{color:#68d391;font-size:1.5rem;margin-bottom:.5rem}
    p{color:#a0aec0;line-height:1.5}
    .badge{display:inline-flex;align-items:center;gap:.4rem;
           background:#2563eb;color:#fff;padding:.3rem .9rem;
           border-radius:9999px;font-size:.8rem;margin:1rem 0}
    .btn{display:inline-block;margin-top:1.5rem;background:#3182ce;
         color:#fff;padding:.75rem 1.5rem;border-radius:8px;
         text-decoration:none;font-size:.9rem;transition:background .2s}
    .btn:hover{background:#2b6cb0}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">💾</div>
    <h1>Guardado sin conexión</h1>
    <p>Tu formulario <strong>${name}</strong> fue guardado localmente.</p>
    <div class="badge">📵 Sin conexión</div>
    <p>Será enviado automáticamente al recuperar conexión a internet.</p>
    ${fileWarning}
    <a class="btn" href="/forms/select">← Volver al Inicio</a>
  </div>
</body>
</html>`;
}

// ── IndexedDB helpers ─────────────────────────────────────────────────────────
function openDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = e => {
            const db = e.target.result;
            if (!db.objectStoreNames.contains(STORE_NAME)) {
                const store = db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
                store.createIndex('timestamp', 'timestamp');
            }
        };
        req.onsuccess = e => resolve(e.target.result);
        req.onerror = e => reject(e.target.error);
    });
}

async function queueSubmission(item) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, 'readwrite');
        tx.objectStore(STORE_NAME).add(item);
        tx.oncomplete = () => resolve();
        tx.onerror = e => reject(e.target.error);
    });
}
