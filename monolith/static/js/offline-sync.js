/* SecApp offline queue sync manager */
(function () {
    'use strict';

    const DB_NAME = 'secapp-offline';
    const DB_VERSION = 1;
    const STORE_NAME = 'pending_submissions';

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

    // ── IndexedDB ─────────────────────────────────────────────────────────────
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

    async function getPending() {
        const db = await openDB();
        return new Promise((resolve, reject) => {
            const req = db.transaction(STORE_NAME, 'readonly').objectStore(STORE_NAME).getAll();
            req.onsuccess = () => resolve(req.result);
            req.onerror = e => reject(e.target.error);
        });
    }

    async function countPending() {
        const db = await openDB();
        return new Promise((resolve, reject) => {
            const req = db.transaction(STORE_NAME, 'readonly').objectStore(STORE_NAME).count();
            req.onsuccess = () => resolve(req.result);
            req.onerror = e => reject(e.target.error);
        });
    }

    async function removeItem(id) {
        const db = await openDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_NAME, 'readwrite');
            tx.objectStore(STORE_NAME).delete(id);
            tx.oncomplete = () => resolve();
            tx.onerror = e => reject(e.target.error);
        });
    }

    // ── CSRF token ────────────────────────────────────────────────────────────
    async function fetchCsrfToken() {
        const res = await fetch('/forms/api/csrf_token', { credentials: 'include' });
        if (!res.ok) throw new Error('Could not obtain CSRF token');
        const json = await res.json();
        return json.csrf_token;
    }

    // ── Submit one queued item ────────────────────────────────────────────────
    async function syncOne(submission, csrfToken) {
        const fd = new FormData();
        fd.append('csrf_token', csrfToken);
        for (const [key, value] of submission.entries) {
            fd.append(key, value);
        }

        const res = await fetch(submission.url, {
            method: 'POST',
            body: fd,
            credentials: 'include',
            redirect: 'follow',
        });

        // Success: the server redirected us to the /success page (or we got 200)
        const landed = res.url || '';
        if (res.ok && (landed.includes('/success') || landed.includes('/forms/select'))) {
            await removeItem(submission.id);
            return true;
        }
        // Auth expired — server redirected to login
        if (landed.includes('/login') || landed === location.origin + '/') {
            throw new Error('session_expired');
        }
        return false;
    }

    // ── UI helpers ────────────────────────────────────────────────────────────
    function getBanner() { return document.getElementById('offline-sync-banner'); }
    function getSyncBtn() { return document.getElementById('sync-now-btn'); }
    function getCountEl() { return document.getElementById('sync-pending-count'); }

    function updateBanner(count, syncing) {
        const banner = getBanner();
        if (!banner) return;

        if (count === 0) {
            banner.style.display = 'none';
            return;
        }

        banner.style.display = 'flex';
        const countEl = getCountEl();
        const btn = getSyncBtn();

        if (countEl) {
            countEl.textContent = count === 1
                ? '1 formulario pendiente de sincronización'
                : `${count} formularios pendientes de sincronización`;
        }
        if (btn) {
            btn.disabled = syncing;
            btn.textContent = syncing ? 'Sincronizando…' : 'Sincronizar Ahora';
        }
    }

    function showToast(message, isError) {
        const el = document.createElement('div');
        el.style.cssText = [
            'position:fixed', 'bottom:1.5rem', 'left:50%', 'transform:translateX(-50%)',
            `background:${isError ? '#c53030' : '#276749'}`, 'color:#fff',
            'padding:.7rem 1.4rem', 'border-radius:8px',
            'box-shadow:0 4px 14px rgba(0,0,0,.35)', 'z-index:9999',
            'font-size:.9rem', 'transition:opacity .3s ease', 'white-space:nowrap',
        ].join(';');
        el.textContent = message;
        document.body.appendChild(el);
        setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 350); }, 3500);
    }

    // ── Sync all pending ──────────────────────────────────────────────────────
    async function syncAll() {
        if (!navigator.onLine) {
            showToast('Sin conexión. Conéctate a internet e intenta de nuevo.', true);
            return;
        }

        const pending = await getPending();
        if (!pending.length) return;

        updateBanner(pending.length, true);

        let ok = 0, fail = 0;
        try {
            const csrfToken = await fetchCsrfToken();
            for (const item of pending) {
                try {
                    const success = await syncOne(item, csrfToken);
                    success ? ok++ : fail++;
                } catch (err) {
                    if (err.message === 'session_expired') {
                        showToast('Sesión expirada. Por favor, inicia sesión nuevamente.', true);
                        updateBanner(await countPending(), false);
                        return;
                    }
                    fail++;
                }
            }
        } catch {
            showToast('Error al obtener token de seguridad. Intenta más tarde.', true);
            updateBanner(await countPending(), false);
            return;
        }

        const remaining = await countPending();
        updateBanner(remaining, false);

        if (ok > 0 && fail === 0) {
            showToast(`✓ ${ok} formulario${ok !== 1 ? 's' : ''} enviado${ok !== 1 ? 's' : ''} correctamente`);
        } else if (fail > 0) {
            showToast(
                `${ok} enviado${ok !== 1 ? 's' : ''}, ${fail} fallido${fail !== 1 ? 's' : ''}. Intenta de nuevo.`,
                true
            );
        }
    }

    // ── Register service worker ───────────────────────────────────────────────
    function registerSW() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.register('/forms/sw.js', { scope: '/forms/' })
            .then(reg => {
                // Listen for SW messages (new item queued from another tab/context)
                navigator.serviceWorker.addEventListener('message', async event => {
                    if (event.data?.type === 'OFFLINE_QUEUED') {
                        updateBanner(await countPending(), false);
                    }
                });
            })
            .catch(err => console.warn('SW registration failed:', err));
    }

    // ── Init ──────────────────────────────────────────────────────────────────
    async function init() {
        registerSW();

        try {
            const count = await countPending();
            updateBanner(count, false);
        } catch {
            // IndexedDB unavailable (private browsing etc.) — silent fail
        }

        // Sync automatically when coming back online
        window.addEventListener('online', async () => {
            const count = await countPending().catch(() => 0);
            if (count > 0) syncAll();
        });

        // Manual sync button
        const btn = getSyncBtn();
        if (btn) btn.addEventListener('click', syncAll);

        // Offline indicator in banner
        window.addEventListener('offline', async () => {
            const count = await countPending().catch(() => 0);
            if (count > 0) updateBanner(count, false);
        });
    }

    document.addEventListener('DOMContentLoaded', init);
})();
