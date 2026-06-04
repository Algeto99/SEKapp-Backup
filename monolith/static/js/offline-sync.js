/* SecApp offline queue sync manager */
(function () {
    'use strict';

    const DB_NAME = 'secapp-offline';
    const DB_VERSION = 1;
    const STORE_NAME = 'pending_submissions';
    const PROPERTIES_URL = '/forms/api/properties';
    const PROPERTIES_STORAGE_KEY = 'secapp:properties:v1';
    const AUTO_SYNC_DELAY_MS = 750;
    const SYNC_REQUEST_TIMEOUT_MS = 90000;

    let syncInFlight = false;
    let syncStartedAt = 0;
    let autoSyncTimer = null;
    let lastQueueActionAt = 0;

    // File field key used when attaching images in the queue manager, by form type
    const ATTACH_FILE_KEY = {
        incident_report: 'foto_evidencia',
        supervision_puesto: 'foto_evidencia',
        informe_novedades_disciplinario: 'foto_evidencia',
        registro_de_capacitaciones: 'capacitacion_files',
        registro_y_acta_de_visita: 'anexos_files',
        log_de_patrullas: 'foto_evidencia',
        medicion_experiencia_cliente: 'foto_evidencia',
        planilla_vehicular: 'foto_evidencia',
        planilla_motocicletas: 'foto_evidencia',
        checklist_cumplimiento: 'foto_evidencia',
        confiabilidad_equipos: 'foto_evidencia',
    };

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

    function normalizeStoreKey(id) {
        if (typeof id === 'number') return id;
        const text = String(id == null ? '' : id).trim();
        const numeric = Number(text);
        return text && Number.isInteger(numeric) && String(numeric) === text ? numeric : id;
    }

    function beginSync() {
        if (!syncInFlight) {
            syncInFlight = true;
            syncStartedAt = Date.now();
            return true;
        }

        const elapsed = Date.now() - syncStartedAt;
        if (elapsed > SYNC_REQUEST_TIMEOUT_MS + 5000) {
            syncInFlight = true;
            syncStartedAt = Date.now();
            return true;
        }

        showToast('Ya hay una sincronización en curso. Intenta de nuevo en unos segundos.', true);
        return false;
    }

    function clearSyncState() {
        syncInFlight = false;
        syncStartedAt = 0;
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
            tx.objectStore(STORE_NAME).delete(normalizeStoreKey(id));
            tx.oncomplete = () => resolve();
            tx.onerror = e => reject(e.target.error);
        });
    }

    // ── CSRF token ────────────────────────────────────────────────────────────
    async function fetchCsrfToken() {
        const res = await fetchWithTimeout(
            '/forms/api/csrf_token',
            { credentials: 'include' },
            SYNC_REQUEST_TIMEOUT_MS
        );
        if (res.status === 401 || res.url?.includes('/login')) throw new Error('session_expired');
        if (!res.ok) throw new Error('Could not obtain CSRF token');
        const json = await res.json();
        return json.csrf_token;
    }

    // ── Session expiry handler ────────────────────────────────────────────────
    function handleSessionExpired() {
        const overlay = document.createElement('div');
        overlay.style.cssText = [
            'position:fixed', 'inset:0', 'background:rgba(0,0,0,.75)',
            'z-index:20000', 'display:flex', 'align-items:center', 'justify-content:center',
            'padding:1rem',
        ].join(';');

        overlay.innerHTML = `
            <div style="background:#1f2937;border:1px solid rgba(96,165,250,.3);border-radius:12px;
                        padding:1.75rem;width:min(380px,92vw);text-align:center;
                        font-family:Roboto,sans-serif;color:#e2e8f0;">
                <div style="font-size:2.5rem;margin-bottom:.75rem;">🔒</div>
                <h3 style="font-size:1.05rem;font-weight:800;color:#fff;margin:0 0 .5rem;">Sesión expirada</h3>
                <p style="font-size:.85rem;color:#93c5fd;margin:0 0 1.25rem;line-height:1.5;">
                    Tu sesión ha expirado. Al iniciar sesión nuevamente, tus formularios pendientes
                    seguirán guardados y podrás enviarlos.
                </p>
                <button id="session-expired-login-btn" type="button"
                    style="background:#2563eb;border:1px solid #60a5fa;color:#fff;border-radius:8px;
                           padding:.65rem 1.5rem;font-size:.9rem;font-weight:700;cursor:pointer;width:100%;">
                    Iniciar sesión
                </button>
            </div>`;

        document.body.appendChild(overlay);

        document.getElementById('session-expired-login-btn').addEventListener('click', () => {
            window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
        });
    }

    // ── Property resolution hook ──────────────────────────────────────────────
    // Pages can register window.secappResolveOfflineProperty to intercept
    // submissions that were entered offline without a real property selection.
    // The hook receives the submission and must return a Promise that resolves
    // to { id_propiedad, cliente_instalacion } or rejects/returns null to skip.
    async function resolvePropertyIfNeeded(submission, options = {}) {
        const hasOfflineFlag = submission.entries.some(
            ([k]) => k === 'property_entered_offline'
        );
        if (!hasOfflineFlag) return submission;

        if (options.forceSubmit) {
            const patched = submission.entries.filter(([k]) => k !== 'property_entered_offline');
            await updateItem(submission.id, { ...submission, entries: patched });
            return { ...submission, entries: patched };
        }

        if (typeof window.secappResolveOfflineProperty !== 'function') {
            const exactResolution = await resolvePropertyFromCachedExactMatch(submission);
            if (exactResolution) {
                return patchResolvedProperty(submission, exactResolution);
            }
            throw new Error('property_unresolved');
        }

        const resolution = await window.secappResolveOfflineProperty(submission);
        if (!resolution) throw new Error('property_unresolved');

        return patchResolvedProperty(submission, resolution);
    }

    async function patchResolvedProperty(submission, resolution) {
        // Patch the entries: replace cliente_instalacion, remove flag, optionally set id_propiedad
        const patched = submission.entries
            .filter(([k]) => k !== 'cliente_instalacion' && k !== 'property_entered_offline' && k !== 'id_propiedad');
        patched.push(['cliente_instalacion', resolution.cliente_instalacion]);
        if (resolution.id_propiedad) {
            patched.push(['id_propiedad', String(resolution.id_propiedad)]);
        }

        // Persist the patched entries back to IndexedDB so a page reload doesn't re-prompt
        await updateItem(submission.id, { ...submission, entries: patched });

        return { ...submission, entries: patched };
    }

    function normalizePropertyName(value) {
        return String(value || '').trim().toLowerCase();
    }

    async function resolvePropertyFromCachedExactMatch(submission) {
        const offlineName = (
            submission.entries.find(([k]) => k === 'cliente_instalacion')
            || submission.entries.find(([k]) => k === 'cliente_visitado')
            || []
        )[1];
        const wanted = normalizePropertyName(offlineName);
        if (!wanted) return null;

        const data = await loadCachedProperties();
        const matches = (data.properties || []).filter(p => normalizePropertyName(p.name) === wanted);
        if (matches.length !== 1) return null;

        return {
            id_propiedad: matches[0].id,
            cliente_instalacion: matches[0].name,
        };
    }

    async function loadCachedProperties() {
        if (typeof window.secappLoadPreparedProperties === 'function') {
            try {
                const data = await window.secappLoadPreparedProperties();
                if (data && Array.isArray(data.properties)) return data;
            } catch {
                // Fall through to local cache reads.
            }
        }

        if ('caches' in window) {
            try {
                const cached = await caches.match(PROPERTIES_URL, { ignoreVary: true });
                if (cached && cached.ok) {
                    const data = await cached.json();
                    if (data && Array.isArray(data.properties)) return data;
                }
            } catch {
                // Fall through to localStorage.
            }
        }

        try {
            const data = JSON.parse(localStorage.getItem(PROPERTIES_STORAGE_KEY) || 'null');
            if (data && Array.isArray(data.properties)) return data;
        } catch {
            // No usable properties snapshot.
        }

        return { properties: [] };
    }

    async function updateItem(id, data) {
        const db = await openDB();
        const storeKey = normalizeStoreKey(id);
        return new Promise((resolve, reject) => {
            const tx = db.transaction(STORE_NAME, 'readwrite');
            tx.objectStore(STORE_NAME).put({ ...data, id: storeKey });
            tx.oncomplete = () => resolve();
            tx.onerror = e => reject(e.target.error);
        });
    }

    // ── Submit one queued item ────────────────────────────────────────────────
    async function syncOne(submission, csrfToken, options = {}) {
        // May throw 'property_unresolved' — caller handles it
        const resolved = await resolvePropertyIfNeeded(submission, options);

        const fd = new FormData();
        fd.append('csrf_token', csrfToken);
        for (const [key, value] of resolved.entries) {
            fd.append(key, value);
        }
        // Reconstruct files stored as ArrayBuffer during offline capture
        for (const fileEntry of resolved.fileEntries || []) {
            const blob = new Blob([fileEntry.buffer], { type: fileEntry.type });
            fd.append(fileEntry.key, blob, fileEntry.name);
        }

        const res = await fetchWithTimeout(resolved.url, {
            method: 'POST',
            body: fd,
            credentials: 'include',
            redirect: 'follow',
            headers: { 'X-SecApp-Replay': '1' },
        }, SYNC_REQUEST_TIMEOUT_MS);

        // Success: the server redirected us to the /success page, or returned any
        // other non-error 2xx/3xx response after storing the form.
        const landed = res.url || '';
        if (landed.includes('/login') || landed === location.origin + '/') {
            throw new Error('session_expired');
        }
        if (res.ok && res.status !== 202 && !landed.includes('/error')) {
            await removeItem(resolved.id);
            return true;
        }
        return false;
    }

    async function fetchWithTimeout(url, options, timeoutMs) {
        const controller = 'AbortController' in window ? new AbortController() : null;
        const timer = controller
            ? setTimeout(() => controller.abort(), timeoutMs)
            : null;

        try {
            return await fetch(url, {
                ...options,
                signal: controller ? controller.signal : options.signal,
            });
        } finally {
            if (timer) clearTimeout(timer);
        }
    }

    // ── UI helpers ────────────────────────────────────────────────────────────
    function getBanner() { return document.getElementById('offline-sync-banner'); }
    function getSyncBtn() { return document.getElementById('sync-now-btn'); }
    function getManageBtn() { return document.getElementById('sync-manage-btn'); }
    function getCountEl() { return document.getElementById('sync-pending-count'); }

    function ensureBanner() {
        let banner = getBanner();
        if (!banner && document.body) {
            banner = document.createElement('div');
            banner.id = 'offline-sync-banner';
            banner.className = 'offline-banner';
            banner.setAttribute('role', 'status');
            banner.setAttribute('aria-live', 'polite');
            banner.style.cssText = [
                'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:200',
                'background:linear-gradient(90deg,#1e40af,#2563eb)', 'color:#fff',
                'padding:.6rem 1.25rem', 'display:none', 'align-items:center',
                'justify-content:center', 'gap:1rem', 'font-size:.875rem',
                'box-shadow:0 2px 8px rgba(0,0,0,.3)', 'flex-wrap:wrap',
            ].join(';');
            banner.innerHTML = [
                '<span class="sync-icon" style="font-size:1.1rem;flex-shrink:0;">📋</span>',
                '<span class="sync-text" id="sync-pending-count" style="flex:1;min-width:0;">Formularios pendientes de sincronización</span>',
                '<button id="sync-now-btn" class="sync-now-btn" type="button">Sincronizar Ahora</button>',
                '<button id="sync-manage-btn" class="sync-now-btn" type="button">Gestionar</button>',
            ].join('');
            document.body.appendChild(banner);
        } else if (banner && !getManageBtn()) {
            const btn = document.createElement('button');
            btn.id = 'sync-manage-btn';
            btn.className = 'sync-now-btn';
            btn.type = 'button';
            btn.textContent = 'Gestionar';
            banner.appendChild(btn);
        }

        bindBannerButtons();
        return banner;
    }

    function bindBannerButtons() {
        const syncBtn = getSyncBtn();
        if (syncBtn && !syncBtn.dataset.secappBound) {
            syncBtn.dataset.secappBound = '1';
            syncBtn.addEventListener('click', () => syncAll());
        }

        const manageBtn = getManageBtn();
        if (manageBtn && !manageBtn.dataset.secappBound) {
            manageBtn.dataset.secappBound = '1';
            manageBtn.addEventListener('click', openQueueManager);
        }
    }

    function updateBanner(count, syncing) {
        if (count === 0) {
            const banner = getBanner();
            if (!banner) return;
            banner.style.display = 'none';
            return;
        }

        const banner = ensureBanner();
        if (!banner) return;

        banner.style.display = 'flex';
        const countEl = getCountEl();
        const btn = getSyncBtn();
        const manageBtn = getManageBtn();

        if (countEl) {
            countEl.textContent = count === 1
                ? '1 formulario pendiente de sincronización'
                : `${count} formularios pendientes de sincronización`;
        }
        if (btn) {
            btn.disabled = syncing;
            btn.textContent = syncing ? 'Sincronizando…' : 'Sincronizar Ahora';
        }
        if (manageBtn) {
            manageBtn.disabled = syncing;
        }
    }

    function showToast(message, isError) {
        const el = document.createElement('div');
        el.style.cssText = [
            'position:fixed', 'bottom:1.5rem', 'left:50%', 'transform:translateX(-50%)',
            `background:${isError ? '#c53030' : '#276749'}`, 'color:#fff',
            'padding:.7rem 1.4rem', 'border-radius:8px',
            'box-shadow:0 4px 14px rgba(0,0,0,.35)', 'z-index:10050',
            'font-size:.9rem', 'transition:opacity .3s ease', 'white-space:nowrap',
        ].join(';');
        el.textContent = message;
        document.body.appendChild(el);
        setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 350); }, 3500);
    }

    // ── Sync all pending ──────────────────────────────────────────────────────
    async function syncAll(options = {}) {
        if (!beginSync()) return;
        if (!navigator.onLine) {
            clearSyncState();
            showToast('Sin conexión. Conéctate a internet e intenta de nuevo.', true);
            return;
        }

        try {
            const pending = await getPending();
            if (!pending.length) {
                updateBanner(0, false);
                showToast('No hay formularios pendientes.');
                return;
            }

            updateBanner(pending.length, true);
            showToast('Sincronizando formularios pendientes...');

            let ok = 0, fail = 0, unresolved = 0;
            try {
                const csrfToken = await fetchCsrfToken();
                for (const item of pending) {
                    try {
                        const success = await syncOne(item, csrfToken, options);
                        success ? ok++ : fail++;
                    } catch (err) {
                        if (err.message === 'session_expired') {
                            updateBanner(await countPending(), false);
                            handleSessionExpired();
                            return;
                        }
                        if (err.message === 'property_unresolved') {
                            unresolved++;
                            continue;
                        }
                        fail++;
                    }
                }
            } catch (err) {
                if (err.message === 'session_expired') {
                    updateBanner(await countPending(), false);
                    handleSessionExpired();
                    return;
                }
                showToast('Error al obtener token de seguridad. Intenta más tarde.', true);
                updateBanner(await countPending(), false);
                return;
            }

            const remaining = await countPending();
            updateBanner(remaining, false);
            if (remaining === 0) reviewPromptShown = false;

            if (unresolved > 0) {
                showToast(
                    `${unresolved} formulario${unresolved !== 1 ? 's' : ''} requiere${unresolved === 1 ? '' : 'n'} seleccionar instalación antes de enviar.`,
                    true
                );
            }
            if (ok > 0 && fail === 0 && unresolved === 0) {
                showToast(`✓ ${ok} formulario${ok !== 1 ? 's' : ''} enviado${ok !== 1 ? 's' : ''} correctamente`);
            } else if (fail > 0) {
                showToast(
                    `${ok} enviado${ok !== 1 ? 's' : ''}, ${fail} fallido${fail !== 1 ? 's' : ''}. Intenta de nuevo.`,
                    true
                );
            }
        } finally {
            clearSyncState();
            refreshQueueManager();
        }
    }

    async function syncQueuedItem(id, options = {}) {
        if (!beginSync()) return;
        if (!navigator.onLine) {
            clearSyncState();
            showToast('Sin conexión. Conéctate a internet e intenta de nuevo.', true);
            return;
        }

        try {
            const item = (await getPending()).find(entry => String(entry.id) === String(id));
            if (!item) {
                showToast('Ese formulario ya no está en cola.', false);
                return;
            }

            updateBanner(await countPending(), true);
            showToast('Sincronizando formulario pendiente...');
            const csrfToken = await fetchCsrfToken();
            const success = await syncOne(item, csrfToken, options);
            if (success) {
                showToast('Formulario enviado correctamente.');
            } else {
                showToast('No se pudo enviar ese formulario. Intenta forzarlo o elimínalo.', true);
            }
        } catch (err) {
            if (err.message === 'session_expired') {
                updateBanner(await countPending().catch(() => 0), false);
                handleSessionExpired();
                return;
            } else if (err.message === 'property_unresolved') {
                showToast('Selecciona la instalación o usa envío forzado.', true);
            } else {
                showToast('Error al enviar ese formulario.', true);
            }
        } finally {
            clearSyncState();
            updateBanner(await countPending().catch(() => 0), false);
            refreshQueueManager();
        }
    }

    async function deleteQueuedItem(id) {
        clearSyncState();
        const ok = window.confirm('¿Eliminar este formulario pendiente? Esta acción no se puede deshacer.');
        if (!ok) return;

        try {
            await removeItem(id);
            const remaining = await countPending().catch(() => 0);
            updateBanner(remaining, false);
            showToast('Formulario pendiente eliminado.');
            refreshQueueManager();
        } catch {
            showToast('No se pudo eliminar el formulario pendiente.', true);
        }
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatQueuedAt(timestamp) {
        if (!timestamp) return 'Fecha no disponible';
        try {
            return new Date(timestamp).toLocaleString('es-CO', {
                dateStyle: 'medium',
                timeStyle: 'short',
            });
        } catch {
            return new Date(timestamp).toLocaleString();
        }
    }

    function getEntryValue(item, key) {
        const entry = (item.entries || []).find(([entryKey]) => entryKey === key);
        return entry ? entry[1] : '';
    }

    function describeQueuedItem(item) {
        const fileEntries = item.fileEntries || [];
        const hasLegacyMissingFiles = item.hasFiles && fileEntries.length === 0;
        const needsProperty = (item.entries || []).some(([k]) => k === 'property_entered_offline');
        const propertyName = getEntryValue(item, 'cliente_instalacion') || getEntryValue(item, 'cliente_visitado');
        const notes = [];

        if (fileEntries.length) notes.push(`${fileEntries.length} archivo${fileEntries.length !== 1 ? 's' : ''} guardado${fileEntries.length !== 1 ? 's' : ''}`);
        if (hasLegacyMissingFiles) notes.push('Adjunto anterior no recuperable');
        if (needsProperty) notes.push('Instalación pendiente de resolver');
        if (propertyName) notes.push(`Instalación: ${propertyName}`);

        return {
            title: FORM_NAMES[item.formType] || item.formType || 'Formulario',
            queuedAt: formatQueuedAt(item.timestamp),
            notes,
            hasLegacyMissingFiles,
            needsProperty,
        };
    }

    function ensureQueueManager() {
        let modal = document.getElementById('offline-queue-modal');
        if (modal || !document.body) return modal;

        const style = document.createElement('style');
        style.textContent = [
            '.offline-queue-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.68);z-index:10000;align-items:center;justify-content:center;padding:1rem;}',
            '.offline-queue-modal.open{display:flex;}',
            '.offline-queue-panel{width:min(760px,96vw);max-height:86vh;overflow:auto;background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,.12);border-radius:12px;box-shadow:0 20px 70px rgba(0,0,0,.55);}',
            '.offline-queue-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;padding:1.25rem 1.35rem;border-bottom:1px solid rgba(255,255,255,.08);}',
            '.offline-queue-title{font-family:Roboto,sans-serif;font-size:1rem;font-weight:800;margin:0;color:#fff;}',
            '.offline-queue-subtitle{font-size:.8rem;color:#9ca3af;margin:.25rem 0 0;}',
            '.offline-queue-close{background:none;border:none;color:#cbd5e1;font-size:1.35rem;line-height:1;cursor:pointer;padding:.15rem;touch-action:manipulation;}',
            '.offline-queue-actions{display:flex;gap:.6rem;flex-wrap:wrap;padding:1rem 1.35rem;border-bottom:1px solid rgba(255,255,255,.08);}',
            '.offline-queue-list{display:flex;flex-direction:column;gap:.75rem;padding:1rem 1.35rem 1.35rem;}',
            '.offline-queue-item{border:1px solid rgba(255,255,255,.1);border-radius:10px;background:rgba(255,255,255,.04);padding:1rem;}',
            '.offline-queue-row{display:flex;justify-content:space-between;gap:1rem;align-items:flex-start;flex-wrap:wrap;}',
            '.offline-queue-name{font-weight:800;color:#fff;}',
            '.offline-queue-date{font-size:.78rem;color:#9ca3af;margin-top:.15rem;}',
            '.offline-queue-notes{display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.65rem;}',
            '.offline-queue-note{font-size:.72rem;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);border-radius:999px;padding:.18rem .5rem;color:#cbd5e1;}',
            '.offline-queue-note.warn{border-color:#f59e0b;color:#fcd34d;background:rgba(245,158,11,.12);}',
            '.offline-queue-buttons{display:flex;gap:.45rem;flex-wrap:wrap;margin-top:.85rem;}',
            '.offline-queue-btn{border:1px solid rgba(255,255,255,.16);border-radius:7px;background:rgba(255,255,255,.08);color:#fff;padding:.45rem .7rem;font-size:.78rem;font-weight:700;cursor:pointer;touch-action:manipulation;pointer-events:auto;}',
            '.offline-queue-btn.primary{background:#2563eb;border-color:#60a5fa;}',
            '.offline-queue-btn.warn{background:#92400e;border-color:#f59e0b;color:#fef3c7;}',
            '.offline-queue-btn.danger{background:#7f1d1d;border-color:#ef4444;color:#fee2e2;}',
            '.offline-queue-btn.attach{background:#065f46;border-color:#34d399;color:#d1fae5;}',
            '.offline-queue-files{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.55rem;}',
            '.offline-queue-file-chip{display:flex;align-items:center;gap:.3rem;font-size:.72rem;border:1px solid rgba(52,211,153,.3);border-radius:999px;padding:.18rem .55rem;color:#6ee7b7;background:rgba(52,211,153,.08);}',
            '.offline-queue-file-chip button{background:none;border:none;color:#f87171;cursor:pointer;font-size:.8rem;line-height:1;padding:0 .1rem;}',
            '.offline-queue-empty{padding:1.5rem;color:#9ca3af;text-align:center;}',
            '@media(max-width:640px){.offline-queue-head,.offline-queue-actions,.offline-queue-list{padding-left:1rem;padding-right:1rem;}.offline-queue-btn{flex:1 1 auto;}}',
        ].join('');
        document.head.appendChild(style);

        modal = document.createElement('div');
        modal.id = 'offline-queue-modal';
        modal.className = 'offline-queue-modal';
        modal.innerHTML = [
            '<div class="offline-queue-panel" role="dialog" aria-modal="true" aria-labelledby="offline-queue-title">',
            '<div class="offline-queue-head">',
            '<div>',
            '<h2 id="offline-queue-title" class="offline-queue-title">Cola de formularios sin conexión</h2>',
            '<p class="offline-queue-subtitle">Reintenta, fuerza el envío o elimina formularios pendientes.</p>',
            '</div>',
            '<button class="offline-queue-close" type="button" aria-label="Cerrar" data-queue-action="close">×</button>',
            '</div>',
            '<div class="offline-queue-actions">',
            '<button id="offline-queue-retry-all" class="offline-queue-btn primary" type="button" data-queue-action="retry-all">Reintentar todos</button>',
            '<button id="offline-queue-force-all" class="offline-queue-btn warn" type="button" data-queue-action="force-all">Forzar todos</button>',
            '</div>',
            '<div id="offline-queue-list" class="offline-queue-list"></div>',
            '</div>',
        ].join('');
        document.body.appendChild(modal);

        modal.addEventListener('click', event => {
            if (event.target === modal) closeQueueManager();
        });
        modal.addEventListener('click', handleQueueAction);
        modal.addEventListener('touchend', handleQueueAction, { passive: false });

        return modal;
    }

    async function renderQueueManager() {
        const modal = ensureQueueManager();
        const list = document.getElementById('offline-queue-list');
        if (!modal || !list) return;

        const pending = await getPending().catch(() => []);
        if (!pending.length) {
            list.innerHTML = '<div class="offline-queue-empty">No hay formularios pendientes.</div>';
            updateBanner(0, false);
            return;
        }

        list.innerHTML = pending.map(item => {
            const details = describeQueuedItem(item);
            const notes = details.notes.length
                ? `<div class="offline-queue-notes">${details.notes.map(note => {
                    const warn = /Adjunto anterior|pendiente/.test(note) ? ' warn' : '';
                    return `<span class="offline-queue-note${warn}">${escapeHtml(note)}</span>`;
                }).join('')}</div>`
                : '';
            const forceLabel = details.hasLegacyMissingFiles ? 'Enviar sin adjunto' : 'Forzar envío';
            const fileEntries = item.fileEntries || [];
            const fileChips = fileEntries.length
                ? `<div class="offline-queue-files">${fileEntries.map((fe, idx) =>
                    `<span class="offline-queue-file-chip">📎 ${escapeHtml(fe.name)}<button type="button" title="Quitar adjunto" data-queue-action="remove-file" data-queue-id="${escapeHtml(item.id)}" data-file-index="${idx}">×</button></span>`
                  ).join('')}</div>`
                : '';

            return [
                `<div class="offline-queue-item" data-queue-id="${escapeHtml(item.id)}">`,
                '<div class="offline-queue-row">',
                '<div>',
                `<div class="offline-queue-name">${escapeHtml(details.title)}</div>`,
                `<div class="offline-queue-date">${escapeHtml(details.queuedAt)}</div>`,
                '</div>',
                '</div>',
                notes,
                fileChips,
                '<div class="offline-queue-buttons">',
                `<button class="offline-queue-btn primary" type="button" data-queue-action="retry" data-queue-id="${escapeHtml(item.id)}">Reintentar</button>`,
                `<button class="offline-queue-btn attach" type="button" data-queue-action="attach" data-queue-id="${escapeHtml(item.id)}">📎 Adjuntar imagen</button>`,
                `<button class="offline-queue-btn warn" type="button" data-queue-action="force" data-queue-id="${escapeHtml(item.id)}">${forceLabel}</button>`,
                `<button class="offline-queue-btn danger" type="button" data-queue-action="delete" data-queue-id="${escapeHtml(item.id)}">Eliminar</button>`,
                '</div>',
                '</div>',
            ].join('');
        }).join('');
    }

    function handleQueueAction(event) {
        const target = event.target instanceof Element ? event.target : event.target?.parentElement;
        const button = target?.closest('[data-queue-action]');
        if (!button) return;

        const now = Date.now();
        if (now - lastQueueActionAt < 350) return;
        lastQueueActionAt = now;

        event.preventDefault();
        event.stopPropagation();

        const action = button.dataset.queueAction;
        const id = button.dataset.queueId;

        if (action === 'close') {
            closeQueueManager();
        } else if (action === 'retry-all') {
            syncAll();
        } else if (action === 'force-all') {
            const ok = window.confirm('¿Forzar el envío de todos los formularios pendientes? Los adjuntos antiguos que no fueron guardados no se podrán recuperar.');
            if (ok) syncAll({ forceSubmit: true });
        } else if (action === 'retry') {
            syncQueuedItem(id);
        } else if (action === 'force') {
            const ok = window.confirm('¿Forzar el envío de este formulario? Si el adjunto fue creado antes de la corrección, no se podrá recuperar.');
            if (ok) syncQueuedItem(id, { forceSubmit: true });
        } else if (action === 'delete') {
            deleteQueuedItem(id);
        } else if (action === 'attach') {
            attachFileToQueuedItem(id);
        } else if (action === 'remove-file') {
            removeFileFromQueuedItem(id, parseInt(button.dataset.fileIndex, 10));
        }
    }

    function attachFileToQueuedItem(id) {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = 'image/*,application/pdf';
        input.multiple = true;
        input.style.display = 'none';
        document.body.appendChild(input);

        input.addEventListener('change', async () => {
            document.body.removeChild(input);
            if (!input.files.length) return;

            try {
                const pending = await getPending();
                const item = pending.find(e => String(e.id) === String(id));
                if (!item) { showToast('Formulario no encontrado en cola.', true); return; }

                const existingFiles = item.fileEntries || [];
                const fileKey = ATTACH_FILE_KEY[item.formType] || 'foto_evidencia';
                const newEntries = await Promise.all(Array.from(input.files).map(async file => {
                    const buffer = await file.arrayBuffer();
                    return { key: fileKey, name: file.name, type: file.type, buffer };
                }));

                await updateItem(id, { ...item, fileEntries: [...existingFiles, ...newEntries] });
                showToast(`${newEntries.length} archivo${newEntries.length > 1 ? 's' : ''} adjunto${newEntries.length > 1 ? 's' : ''} correctamente.`);
                refreshQueueManager();
            } catch (err) {
                console.error('Error attaching file:', err);
                showToast('No se pudo adjuntar el archivo.', true);
            }
        });

        input.click();
    }

    async function removeFileFromQueuedItem(id, index) {
        try {
            const pending = await getPending();
            const item = pending.find(e => String(e.id) === String(id));
            if (!item) return;
            const updated = (item.fileEntries || []).filter((_, i) => i !== index);
            await updateItem(id, { ...item, fileEntries: updated });
            refreshQueueManager();
        } catch (err) {
            console.error('Error removing file:', err);
            showToast('No se pudo quitar el archivo.', true);
        }
    }

    function openQueueManager() {
        const modal = ensureQueueManager();
        if (!modal) return;
        modal.classList.add('open');
        renderQueueManager();
    }

    function closeQueueManager() {
        const modal = document.getElementById('offline-queue-modal');
        if (modal) modal.classList.remove('open');
    }

    function refreshQueueManager() {
        const modal = document.getElementById('offline-queue-modal');
        if (modal && modal.classList.contains('open')) renderQueueManager();
    }

    // When back online, prompt the user to review pending forms instead of auto-syncing.
    let reviewPromptShown = false;

    async function maybeSyncPending() {
        if (!navigator.onLine || syncInFlight) return;
        const count = await countPending().catch(() => 0);
        if (count === 0) return;

        updateBanner(count, false);

        // Only show the review prompt once per online session to avoid repeated interruptions.
        if (reviewPromptShown) return;
        reviewPromptShown = true;

        showReviewPrompt(count);
    }

    function showReviewPrompt(count) {
        const existing = document.getElementById('offline-review-prompt');
        if (existing) existing.remove();

        const prompt = document.createElement('div');
        prompt.id = 'offline-review-prompt';
        prompt.style.cssText = [
            'position:fixed', 'bottom:1.5rem', 'left:50%', 'transform:translateX(-50%)',
            'background:#1e3a5f', 'color:#e2e8f0',
            'border:1px solid rgba(96,165,250,.4)',
            'border-radius:12px', 'padding:1rem 1.25rem',
            'box-shadow:0 8px 32px rgba(0,0,0,.5)',
            'z-index:10100', 'width:min(420px,92vw)',
            'display:flex', 'flex-direction:column', 'gap:.75rem',
            'font-family:Roboto,sans-serif',
        ].join(';');

        const label = count === 1
            ? '1 formulario pendiente de envío'
            : `${count} formularios pendientes de envío`;

        prompt.innerHTML = `
            <div style="display:flex;align-items:flex-start;gap:.75rem;">
                <span style="font-size:1.5rem;flex-shrink:0;">📋</span>
                <div>
                    <p style="font-weight:700;font-size:.95rem;margin:0 0 .25rem;color:#fff;">${label}</p>
                    <p style="font-size:.82rem;color:#93c5fd;margin:0;">Estás de nuevo en línea. Revisa tus formularios antes de enviarlos — puedes adjuntar imágenes o verificar los detalles.</p>
                </div>
            </div>
            <div style="display:flex;gap:.6rem;flex-wrap:wrap;">
                <button id="offline-review-btn" type="button"
                    style="flex:1;background:#2563eb;border:1px solid #60a5fa;color:#fff;border-radius:8px;padding:.5rem .8rem;font-size:.82rem;font-weight:700;cursor:pointer;">
                    Revisar y adjuntar
                </button>
                <button id="offline-sync-quick-btn" type="button"
                    style="flex:1;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);color:#e2e8f0;border-radius:8px;padding:.5rem .8rem;font-size:.82rem;font-weight:700;cursor:pointer;">
                    Enviar ahora
                </button>
                <button id="offline-review-dismiss-btn" type="button"
                    style="background:none;border:none;color:#9ca3af;font-size:.8rem;cursor:pointer;padding:.25rem .5rem;align-self:center;">
                    Más tarde
                </button>
            </div>`;

        document.body.appendChild(prompt);

        document.getElementById('offline-review-btn').addEventListener('click', () => {
            prompt.remove();
            openQueueManager();
        });

        document.getElementById('offline-sync-quick-btn').addEventListener('click', () => {
            prompt.remove();
            syncAll();
        });

        document.getElementById('offline-review-dismiss-btn').addEventListener('click', () => {
            prompt.remove();
        });
    }

    function scheduleAutoSync() {
        if (autoSyncTimer) clearTimeout(autoSyncTimer);
        autoSyncTimer = setTimeout(() => {
            autoSyncTimer = null;
            maybeSyncPending();
        }, AUTO_SYNC_DELAY_MS);
    }

    // ── Register service worker ───────────────────────────────────────────────
    function registerSW() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.register('/forms/sw.js', { scope: '/' })
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

        if (navigator.onLine) scheduleAutoSync();

        // Sync automatically when coming back online, and when mobile browsers
        // resume a page after the connection changed in the background.
        window.addEventListener('online', scheduleAutoSync);
        window.addEventListener('focus', scheduleAutoSync);
        window.addEventListener('pageshow', scheduleAutoSync);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') scheduleAutoSync();
        });

        // Manual sync and queue management buttons
        bindBannerButtons();

        // Offline indicator in banner — reset review prompt so it re-appears next time online
        window.addEventListener('offline', async () => {
            reviewPromptShown = false;
            const count = await countPending().catch(() => 0);
            if (count > 0) updateBanner(count, false);
        });
    }

    window.secappSyncOfflineNow = syncAll;

    document.addEventListener('DOMContentLoaded', init);
})();
