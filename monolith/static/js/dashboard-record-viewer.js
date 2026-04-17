(function() {
    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function ensureStyles() {
        if (document.getElementById('drv-styles')) return;
        const style = document.createElement('style');
        style.id = 'drv-styles';
        style.textContent = `
            .drv-action-btn {
                background: rgba(96,165,250,0.14);
                color: #93c5fd;
                border: 1px solid rgba(96,165,250,0.28);
                border-radius: 8px;
                padding: 0.35rem 0.65rem;
                font-size: 0.78rem;
                font-family: Roboto, sans-serif;
                cursor: pointer;
                transition: all 0.18s ease;
            }
            .drv-action-btn:hover {
                background: rgba(96,165,250,0.22);
                color: #dbeafe;
            }
            .drv-modal-backdrop {
                display: none;
                position: fixed;
                inset: 0;
                background: rgba(2,6,23,0.72);
                z-index: 400;
                align-items: center;
                justify-content: center;
                padding: 1rem;
            }
            .drv-modal-backdrop.active { display: flex; }
            .drv-modal-container {
                width: min(760px, 100%);
                max-height: 88vh;
                background: #1f2937;
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 16px;
                overflow: hidden;
                display: flex;
                flex-direction: column;
                box-shadow: 0 20px 60px rgba(0,0,0,0.45);
            }
            .drv-modal-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 1rem 1.25rem;
                border-bottom: 1px solid rgba(255,255,255,0.08);
            }
            .drv-modal-title {
                margin: 0;
                color: #fff;
                font-family: Merriweather, serif;
                font-size: 1.05rem;
                font-weight: 700;
            }
            .drv-modal-close {
                background: none;
                border: none;
                color: #94a3b8;
                font-size: 1.5rem;
                cursor: pointer;
            }
            .drv-modal-body {
                padding: 1.25rem;
                overflow-y: auto;
                flex: 1;
            }
            .drv-record-loading {
                text-align: center;
                color: #94a3b8;
                padding: 2rem;
            }
            .drv-detail-section {
                background: rgba(255,255,255,0.03);
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                padding: 1rem;
                margin-bottom: 1rem;
            }
            .drv-detail-section h4 {
                margin: 0 0 0.85rem 0;
                color: #fff;
                font-family: Merriweather, serif;
                font-size: 0.98rem;
            }
            .drv-detail-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 0.85rem;
            }
            .drv-detail-field label {
                display: block;
                color: #94a3b8;
                font-size: 0.72rem;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 0.35rem;
                font-weight: 700;
            }
            .drv-detail-field p {
                margin: 0;
                color: #e5e7eb;
                font-size: 0.9rem;
                word-break: break-word;
            }
            .drv-detail-field > div {
                color: #e5e7eb;
                font-size: 0.9rem;
                word-break: break-word;
            }
            .drv-nested-block {
                display: grid;
                gap: 0.7rem;
            }
            .drv-nested-card {
                background: rgba(255,255,255,0.03);
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 10px;
                padding: 0.8rem;
            }
            .drv-nested-card h5 {
                margin: 0 0 0.65rem 0;
                color: #cbd5e1;
                font-size: 0.84rem;
                font-family: Roboto, sans-serif;
                font-weight: 700;
            }
            .drv-kv-list {
                display: grid;
                gap: 0.65rem;
            }
            .drv-kv-item label {
                display: block;
                color: #94a3b8;
                font-size: 0.68rem;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 0.28rem;
                font-weight: 700;
            }
            .drv-kv-item div {
                color: #e5e7eb;
                font-size: 0.88rem;
                word-break: break-word;
            }
            .drv-inline-image {
                max-width: 220px;
                max-height: 110px;
                border-radius: 6px;
                border: 1px solid rgba(255,255,255,0.15);
                background: #fff;
                padding: 6px;
                display: block;
            }
            .drv-inline-text {
                white-space: pre-wrap;
            }
            .drv-action-bar {
                display: none;
                gap: 0.75rem;
                justify-content: flex-end;
                padding: 1rem 1.25rem;
                border-top: 1px solid rgba(255,255,255,0.08);
            }
            .drv-modal-btn {
                border: 1px solid transparent;
                border-radius: 10px;
                padding: 0.62rem 1rem;
                color: #fff;
                cursor: pointer;
                font-size: 0.84rem;
                font-family: Roboto, sans-serif;
            }
            .drv-modal-btn.secondary {
                background: rgba(148,163,184,0.14);
                border-color: rgba(148,163,184,0.25);
                color: #e2e8f0;
            }
            .drv-modal-btn.pdf {
                background: rgba(239,68,68,0.14);
                border-color: rgba(239,68,68,0.3);
                color: #fecaca;
            }
            .drv-modal-btn.excel {
                background: rgba(34,197,94,0.14);
                border-color: rgba(34,197,94,0.3);
                color: #bbf7d0;
            }
            .drv-modal-btn.email {
                background: rgba(59,130,246,0.14);
                border-color: rgba(59,130,246,0.3);
                color: #bfdbfe;
            }
            .drv-email-overlay {
                display: none;
                position: absolute;
                inset: 0;
                background: rgba(15,23,42,0.78);
                align-items: center;
                justify-content: center;
                padding: 1rem;
            }
            .drv-email-overlay.active { display: flex; }
            .drv-email-box {
                width: min(420px, 100%);
                background: #111827;
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 14px;
                padding: 1.25rem;
            }
            .drv-email-box h4 {
                margin: 0 0 0.75rem 0;
                color: #fff;
                font-family: Merriweather, serif;
            }
            .drv-email-box p {
                margin: 0 0 0.85rem 0;
                color: #94a3b8;
                font-size: 0.82rem;
            }
            .drv-email-input {
                width: 100%;
                background: rgba(255,255,255,0.05);
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 10px;
                color: #fff;
                padding: 0.72rem 0.85rem;
                margin-bottom: 0.85rem;
                font-size: 0.9rem;
            }
            .drv-email-actions {
                display: flex;
                justify-content: flex-end;
                gap: 0.7rem;
            }
            .drv-email-msg {
                min-height: 1.1rem;
                font-size: 0.8rem;
                margin-bottom: 0.65rem;
            }
        `;
        document.head.appendChild(style);
    }

    function ensureModal() {
        let modal = document.getElementById('drv-record-modal');
        if (modal) return modal;

        modal = document.createElement('div');
        modal.id = 'drv-record-modal';
        modal.className = 'drv-modal-backdrop';
        modal.innerHTML = `
            <div class="drv-modal-container" style="position:relative;">
                <div class="drv-modal-header">
                    <h3 class="drv-modal-title">Detalle del Registro</h3>
                    <button class="drv-modal-close" type="button">×</button>
                </div>
                <div class="drv-modal-body">
                    <div class="drv-record-loading">Cargando...</div>
                    <div class="drv-record-content" style="display:none;"></div>
                </div>
                <div class="drv-action-bar">
                    <button class="drv-modal-btn secondary drv-btn-close" type="button">Cerrar</button>
                    <button class="drv-modal-btn pdf drv-btn-pdf" type="button">Descargar PDF</button>
                    <button class="drv-modal-btn excel drv-btn-excel" type="button">Descargar Excel</button>
                    <button class="drv-modal-btn email drv-btn-email" type="button">Enviar por Correo</button>
                </div>
                <div class="drv-email-overlay">
                    <div class="drv-email-box">
                        <h4>Enviar registro por correo</h4>
                        <p>Ingrese la dirección de correo del destinatario.</p>
                        <input class="drv-email-input" type="email" placeholder="correo@ejemplo.com">
                        <div class="drv-email-msg"></div>
                        <div class="drv-email-actions">
                            <button class="drv-modal-btn secondary drv-email-cancel" type="button">Cancelar</button>
                            <button class="drv-modal-btn email drv-email-send" type="button">Enviar</button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        return modal;
    }

    function createToast(id, styles) {
        let toast = document.getElementById(id);
        if (!toast) {
            toast = document.createElement('div');
            toast.id = id;
            toast.style.cssText = styles;
            document.body.appendChild(toast);
        }
        return toast;
    }

    function showToast(id, styles, text) {
        const toast = createToast(id, styles);
        toast.textContent = text;
        toast.style.display = 'block';
        clearTimeout(toast._timer);
        toast._timer = setTimeout(() => { toast.style.display = 'none'; }, 5000);
    }

    function looksLikeJson(value) {
        if (typeof value !== 'string') return false;
        const trimmed = value.trim();
        return (trimmed.startsWith('{') && trimmed.endsWith('}'))
            || (trimmed.startsWith('[') && trimmed.endsWith(']'));
    }

    function tryParseJson(value) {
        if (!looksLikeJson(value)) return value;
        try {
            return JSON.parse(value);
        } catch (_) {
            return value;
        }
    }

    function isImageLikeKey(key) {
        const lower = String(key || '').toLowerCase();
        return ['firma', 'signature', 'imagen', 'image', 'foto', 'photo', 'evidencia', 'diagram'].some(token => lower.includes(token));
    }

    function isImageSource(value) {
        const str = String(value || '').trim();
        return str.startsWith('data:image')
            || /\.(png|jpe?g|gif|webp|svg)(\?|$)/i.test(str)
            || (str.startsWith('http://') || str.startsWith('https://'));
    }

    function renderImage(value, alt) {
        return `<img src="${escapeHtml(value)}" alt="${escapeHtml(alt || 'Imagen')}" class="drv-inline-image">`;
    }

    function renderPrimitive(key, value) {
        const parsedValue = tryParseJson(value);
        if (parsedValue !== value) {
            return renderValue(key, parsedValue, 0);
        }

        const strVal = value && String(value).trim() ? String(value).trim() : '';
        if (!strVal) {
            return '<span class="drv-inline-text">—</span>';
        }
        if (isImageLikeKey(key) && isImageSource(strVal)) {
            return renderImage(strVal, key);
        }
        return `<span class="drv-inline-text">${escapeHtml(strVal)}</span>`;
    }

    function renderObject(obj, depth) {
        const entries = Object.entries(obj || {});
        if (!entries.length) {
            return '<span class="drv-inline-text">—</span>';
        }
        return `
            <div class="drv-kv-list">
                ${entries.map(([subKey, subVal]) => `
                    <div class="drv-kv-item">
                        <label>${escapeHtml(subKey)}</label>
                        <div>${renderValue(subKey, subVal, depth + 1)}</div>
                    </div>
                `).join('')}
            </div>
        `;
    }

    function renderArray(key, arr, depth) {
        if (!arr.length) {
            return '<span class="drv-inline-text">—</span>';
        }
        return `
            <div class="drv-nested-block">
                ${arr.map((item, index) => `
                    <div class="drv-nested-card">
                        <h5>${escapeHtml(`${key || 'Elemento'} ${index + 1}`)}</h5>
                        ${renderValue(`${key || 'item'}_${index + 1}`, item, depth + 1)}
                    </div>
                `).join('')}
            </div>
        `;
    }

    function renderValue(key, value, depth) {
        if (depth > 4) {
            return renderPrimitive(key, value);
        }
        if (value === null || value === undefined) {
            return '<span class="drv-inline-text">—</span>';
        }
        if (Array.isArray(value)) {
            return renderArray(key, value, depth);
        }
        if (typeof value === 'object') {
            return renderObject(value, depth);
        }
        return renderPrimitive(key, value);
    }

    function renderRecordDetail(d, currentRecordId) {
        const raw = d.data || d;
        const rows = Object.entries(raw)
            .filter(([k]) => !['id', 'formType', 'submittedBy', 'dateSubmitted', 'title'].includes(k))
            .map(([k, v]) => {
                const display = renderValue(k, v, 0);
                return `
                    <div class="drv-detail-field">
                        <label>${escapeHtml(k)}</label>
                        <div>${display}</div>
                    </div>
                `;
            }).join('');

        return `
            <div class="drv-detail-section">
                <h4>Información del Registro</h4>
                <div class="drv-detail-grid">
                    <div class="drv-detail-field"><label>ID</label><p>${escapeHtml(d.id || currentRecordId || '—')}</p></div>
                    <div class="drv-detail-field"><label>Enviado por</label><p>${escapeHtml(d.submittedBy || '—')}</p></div>
                    <div class="drv-detail-field"><label>Fecha de envío</label><p>${escapeHtml(d.dateSubmitted || '—')}</p></div>
                    <div class="drv-detail-field"><label>Formulario</label><p>${escapeHtml(d.title || 'Registro')}</p></div>
                </div>
            </div>
            ${rows ? `<div class="drv-detail-section"><h4>Datos del Formulario</h4><div class="drv-detail-grid">${rows}</div></div>` : ''}
        `;
    }

    window.createDashboardRecordViewer = function(options) {
        ensureStyles();
        const modal = ensureModal();
        const cfg = Object.assign({
            formType: 'reporte_incidente',
            recordTitle: 'Detalle del Registro',
        }, options || {});

        const titleEl = modal.querySelector('.drv-modal-title');
        const loadingEl = modal.querySelector('.drv-record-loading');
        const contentEl = modal.querySelector('.drv-record-content');
        const actionBar = modal.querySelector('.drv-action-bar');
        const emailOverlay = modal.querySelector('.drv-email-overlay');
        const emailInput = modal.querySelector('.drv-email-input');
        const emailMsg = modal.querySelector('.drv-email-msg');
        let currentRecordId = null;

        function closeRecord() {
            modal.classList.remove('active');
            emailOverlay.classList.remove('active');
            currentRecordId = null;
        }

        async function openRecord(id) {
            currentRecordId = id;
            titleEl.textContent = cfg.recordTitle || 'Detalle del Registro';
            modal.classList.add('active');
            contentEl.style.display = 'none';
            loadingEl.style.display = 'block';
            actionBar.style.display = 'none';
            emailOverlay.classList.remove('active');
            try {
                const res = await fetch(`/viewer/api/report/${id}?form_type=${encodeURIComponent(cfg.formType)}`);
                const data = await res.json();
                if (!res.ok || !data) {
                    showToast('drv-error-toast',
                        'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#7f1d1d;color:#fca5a5;border:1px solid #ef4444;border-radius:10px;padding:.75rem 1.25rem;font-size:.8125rem;font-family:Roboto,sans-serif;z-index:9999;max-width:90vw;box-shadow:0 8px 24px rgba(0,0,0,.4);',
                        'No se encontró el registro.'
                    );
                    return;
                }
                contentEl.innerHTML = renderRecordDetail(data, currentRecordId);
                contentEl.style.display = 'block';
                actionBar.style.display = 'flex';
            } catch (err) {
                showToast('drv-error-toast',
                    'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#7f1d1d;color:#fca5a5;border:1px solid #ef4444;border-radius:10px;padding:.75rem 1.25rem;font-size:.8125rem;font-family:Roboto,sans-serif;z-index:9999;max-width:90vw;box-shadow:0 8px 24px rgba(0,0,0,.4);',
                    `Error cargando registro: ${err.message}`
                );
            } finally {
                loadingEl.style.display = 'none';
            }
        }

        async function exportRecord(format) {
            if (!currentRecordId) return;
            const endpoint = format === 'pdf' ? '/viewer/api/generate-pdf' : '/viewer/api/export-excel';
            const btn = modal.querySelector(format === 'pdf' ? '.drv-btn-pdf' : '.drv-btn-excel');
            btn.disabled = true;
            try {
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ reports: [{ id: currentRecordId, formType: cfg.formType }] })
                });
                if (!res.ok) {
                    let msg = 'Error generando archivo.';
                    try {
                        const payload = await res.json();
                        msg = payload.message || payload.error || msg;
                    } catch (_) {}
                    throw new Error(msg);
                }
                const blob = await res.blob();
                const ext = format === 'pdf' ? 'pdf' : 'xlsx';
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${cfg.formType}_${currentRecordId}.${ext}`;
                a.click();
                URL.revokeObjectURL(url);
            } catch (err) {
                showToast('drv-error-toast',
                    'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#7f1d1d;color:#fca5a5;border:1px solid #ef4444;border-radius:10px;padding:.75rem 1.25rem;font-size:.8125rem;font-family:Roboto,sans-serif;z-index:9999;max-width:90vw;box-shadow:0 8px 24px rgba(0,0,0,.4);',
                    `Error al exportar: ${err.message}`
                );
            } finally {
                btn.disabled = false;
            }
        }

        function showEmailPrompt() {
            emailInput.value = '';
            emailMsg.textContent = '';
            emailMsg.style.color = '#94a3b8';
            emailOverlay.classList.add('active');
            emailInput.focus();
        }

        function hideEmailPrompt() {
            emailOverlay.classList.remove('active');
        }

        async function sendEmail() {
            const email = emailInput.value.trim();
            if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
                emailMsg.textContent = 'Ingrese un correo electrónico válido.';
                emailMsg.style.color = '#fca5a5';
                return;
            }
            const btn = modal.querySelector('.drv-email-send');
            btn.disabled = true;
            emailMsg.textContent = 'Enviando...';
            emailMsg.style.color = '#94a3b8';
            try {
                const res = await fetch('/viewer/api/email-reports', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        reports: [{ id: currentRecordId, formType: cfg.formType }],
                        recipient_email: email
                    })
                });
                let data = {};
                try { data = await res.json(); } catch (_) {}
                if (!res.ok || !data.success) {
                    throw new Error(data.message || 'Error enviando correo.');
                }
                hideEmailPrompt();
                showToast('drv-success-toast',
                    'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#14532d;color:#86efac;border:1px solid #22c55e;border-radius:10px;padding:.75rem 1.25rem;font-size:.8125rem;font-family:Roboto,sans-serif;z-index:9999;max-width:90vw;box-shadow:0 8px 24px rgba(0,0,0,.4);',
                    `Correo enviado a ${email} exitosamente.`
                );
            } catch (err) {
                emailMsg.textContent = err.message;
                emailMsg.style.color = '#fca5a5';
            } finally {
                btn.disabled = false;
            }
        }

        function wireActionButtons(root) {
            if (!root) return;
            root.querySelectorAll('.drv-action-btn').forEach(btn => {
                if (btn.dataset.bound === 'true') return;
                btn.dataset.bound = 'true';
                btn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    const id = btn.getAttribute('data-record-id');
                    if (id) openRecord(id);
                });
            });
        }

        modal.querySelector('.drv-modal-close').onclick = closeRecord;
        modal.querySelector('.drv-btn-close').onclick = closeRecord;
        modal.querySelector('.drv-btn-pdf').onclick = () => exportRecord('pdf');
        modal.querySelector('.drv-btn-excel').onclick = () => exportRecord('excel');
        modal.querySelector('.drv-btn-email').onclick = showEmailPrompt;
        modal.querySelector('.drv-email-cancel').onclick = hideEmailPrompt;
        modal.querySelector('.drv-email-send').onclick = sendEmail;
        modal.addEventListener('click', (event) => {
            if (event.target === modal) closeRecord();
        });

        return {
            openRecord,
            wireActionButtons,
            actionButtonHtml(id) {
                return `<button type="button" class="drv-action-btn" data-record-id="${escapeHtml(id)}">Ver ↗</button>`;
            }
        };
    };
})();
