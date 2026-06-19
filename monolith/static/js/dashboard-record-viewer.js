(function() {
    function getCsrfToken() {
        const row = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('csrf_access_token='));
        return row ? decodeURIComponent(row.substring('csrf_access_token='.length)) : '';
    }

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
            .drv-inv-table-wrap {
                overflow-x: auto;
                width: 100%;
            }
            .drv-inv-table {
                width: 100%;
                border-collapse: collapse;
                font-size: 0.83rem;
                font-family: Roboto, sans-serif;
            }
            .drv-inv-table thead tr {
                background: rgba(99,102,241,0.25);
            }
            .drv-inv-table th {
                padding: 0.45rem 0.75rem;
                text-align: left;
                color: #c7d2fe;
                font-weight: 700;
                font-size: 0.72rem;
                text-transform: uppercase;
                letter-spacing: 0.04em;
                white-space: nowrap;
                border-bottom: 1px solid rgba(255,255,255,0.1);
            }
            .drv-inv-table td {
                padding: 0.4rem 0.75rem;
                color: #e5e7eb;
                border-bottom: 1px solid rgba(255,255,255,0.05);
                vertical-align: top;
            }
            .drv-inv-table tbody tr:hover td {
                background: rgba(255,255,255,0.04);
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
            .drv-btn-asignar {
                background: rgba(245,158,11,0.14);
                border-color: rgba(245,158,11,0.3);
                color: #fde68a;
            }
            .drv-btn-visita {
                background: rgba(139,92,246,0.14);
                border-color: rgba(139,92,246,0.3);
                color: #ddd6fe;
            }
            .drv-btn-asignar-confirm {
                background: rgba(245,158,11,0.18);
                border-color: rgba(245,158,11,0.35);
                color: #fde68a;
            }
            .drv-asignar-overlay {
                display: none;
                position: absolute;
                inset: 0;
                background: rgba(15,23,42,0.78);
                align-items: center;
                justify-content: center;
                padding: 1rem;
                border-radius: 16px;
            }
            .drv-asignar-overlay.active { display: flex; }
            .drv-asignar-label {
                display: block;
                color: #94a3b8;
                font-size: 0.72rem;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin: 0.65rem 0 0.25rem;
            }
            .drv-asignar-nota {
                resize: vertical;
                min-height: 72px;
                font-family: Roboto, sans-serif;
            }
            body.light-mode .drv-asignar-overlay {
                background: rgba(248,250,252,0.84);
            }
            body.light-mode .drv-asignar-label { color: #64748b; }
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
            body.light-mode .drv-action-btn {
                background: rgba(59,130,246,0.1);
                color: #1d4ed8;
                border-color: rgba(37,99,235,0.22);
            }
            body.light-mode .drv-action-btn:hover {
                background: rgba(59,130,246,0.16);
                color: #1e40af;
            }
            body.light-mode .drv-modal-backdrop {
                background: rgba(15,23,42,0.38);
            }
            body.light-mode .drv-modal-container {
                background: #ffffff;
                border-color: rgba(15,23,42,0.1);
                box-shadow: 0 20px 60px rgba(15,23,42,0.18);
            }
            body.light-mode .drv-modal-header {
                border-bottom-color: rgba(15,23,42,0.08);
            }
            body.light-mode .drv-modal-title,
            body.light-mode .drv-detail-section h4,
            body.light-mode .drv-email-box h4 {
                color: #0f172a;
            }
            body.light-mode .drv-modal-close {
                color: #64748b;
            }
            body.light-mode .drv-modal-close:hover {
                color: #0f172a;
            }
            body.light-mode .drv-modal-body,
            body.light-mode .drv-detail-field p,
            body.light-mode .drv-detail-field > div,
            body.light-mode .drv-kv-item div {
                color: #334155;
            }
            body.light-mode .drv-record-loading,
            body.light-mode .drv-detail-field label,
            body.light-mode .drv-kv-item label,
            body.light-mode .drv-email-box p {
                color: #64748b;
            }
            body.light-mode .drv-detail-section {
                background: #f8fafc;
                border-color: rgba(15,23,42,0.08);
            }
            body.light-mode .drv-nested-card {
                background: #ffffff;
                border-color: rgba(15,23,42,0.08);
            }
            body.light-mode .drv-nested-card h5 {
                color: #334155;
            }
            body.light-mode .drv-inline-image {
                border-color: rgba(15,23,42,0.14);
            }
            body.light-mode .drv-inv-table thead tr {
                background: rgba(79,70,229,0.1);
            }
            body.light-mode .drv-inv-table th {
                color: #3730a3;
                border-bottom-color: rgba(15,23,42,0.08);
            }
            body.light-mode .drv-inv-table td {
                color: #334155;
                border-bottom-color: rgba(15,23,42,0.06);
            }
            body.light-mode .drv-inv-table tbody tr:hover td {
                background: rgba(15,23,42,0.04);
            }
            body.light-mode .drv-action-bar {
                border-top-color: rgba(15,23,42,0.08);
            }
            .drv-5q-grid {
                display: grid;
                grid-template-columns: repeat(5, 1fr);
                gap: 0.6rem;
                margin-bottom: 1rem;
            }
            @media (max-width: 600px) {
                .drv-5q-grid { grid-template-columns: repeat(2, 1fr); }
                .drv-5q-grid .drv-5q-card:last-child { grid-column: 1 / -1; }
            }
            .drv-5q-card {
                background: rgba(99,102,241,0.1);
                border: 1px solid rgba(99,102,241,0.22);
                border-radius: 12px;
                padding: 0.85rem 0.75rem;
                display: flex;
                flex-direction: column;
                gap: 0.3rem;
            }
            .drv-5q-icon {
                font-size: 1.3rem;
                line-height: 1;
            }
            .drv-5q-label {
                font-size: 0.68rem;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.07em;
                color: #a5b4fc;
            }
            .drv-5q-value {
                font-size: 0.86rem;
                color: #e5e7eb;
                word-break: break-word;
                line-height: 1.4;
            }
            details.drv-detail-section > summary {
                cursor: pointer;
                user-select: none;
                list-style: none;
                display: flex;
                align-items: center;
                gap: 0.5rem;
                margin: -1rem -1rem 0 -1rem;
                padding: 0.75rem 1rem;
                border-radius: 12px;
            }
            details.drv-detail-section[open] > summary {
                border-bottom: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px 12px 0 0;
                margin-bottom: 0;
            }
            details.drv-detail-section > summary::before {
                content: '▶';
                font-size: 0.6rem;
                color: #94a3b8;
                transition: transform 0.18s ease;
                flex-shrink: 0;
            }
            details.drv-detail-section[open] > summary::before {
                transform: rotate(90deg);
            }
            details.drv-detail-section > summary:hover::before {
                color: #cbd5e1;
            }
            body.light-mode .drv-5q-card {
                background: rgba(79,70,229,0.07);
                border-color: rgba(79,70,229,0.18);
            }
            body.light-mode .drv-5q-label { color: #4338ca; }
            body.light-mode .drv-5q-value { color: #1e293b; }
            body.light-mode details.drv-detail-section[open] > summary {
                border-bottom-color: rgba(15,23,42,0.08);
            }
            body.light-mode .drv-modal-btn.secondary {
                background: rgba(100,116,139,0.1);
                border-color: rgba(100,116,139,0.22);
                color: #334155;
            }
            body.light-mode .drv-email-overlay {
                background: rgba(248,250,252,0.84);
            }
            body.light-mode .drv-email-box {
                background: #ffffff;
                border-color: rgba(15,23,42,0.1);
                box-shadow: 0 18px 50px rgba(15,23,42,0.16);
            }
            body.light-mode .drv-email-input {
                background: #ffffff;
                border-color: #cbd5e1;
                color: #0f172a;
            }
            body.light-mode .drv-email-input::placeholder {
                color: #94a3b8;
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
                    <button class="drv-modal-btn drv-btn-asignar" type="button" style="display:none;">Asignar hallazgo</button>
                    <button class="drv-modal-btn drv-btn-visita"  type="button" style="display:none;">Agendar visita</button>
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
                <div class="drv-asignar-overlay">
                    <div class="drv-email-box">
                        <h4>Asignar hallazgo</h4>
                        <p>Seleccione el responsable, fecha límite y una nota opcional.</p>
                        <label class="drv-asignar-label">Responsable</label>
                        <select class="drv-email-input drv-asignar-select">
                            <option value="">Cargando usuarios…</option>
                        </select>
                        <label class="drv-asignar-label">Fecha límite</label>
                        <input class="drv-email-input drv-asignar-fecha" type="date">
                        <label class="drv-asignar-label">Nota (opcional)</label>
                        <textarea class="drv-email-input drv-asignar-nota" rows="3" placeholder="Instrucciones o contexto…"></textarea>
                        <div class="drv-email-msg drv-asignar-msg"></div>
                        <div class="drv-email-actions">
                            <button class="drv-modal-btn secondary drv-asignar-cancel" type="button">Cancelar</button>
                            <button class="drv-modal-btn drv-btn-asignar-confirm" type="button">Confirmar</button>
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
        } catch (_) {}
        // Fallback: handle Python repr format (single quotes, None/True/False literals)
        try {
            const jsonStr = value
                .replace(/'/g, '"')
                .replace(/\bNone\b/g, 'null')
                .replace(/\bTrue\b/g, 'true')
                .replace(/\bFalse\b/g, 'false');
            return JSON.parse(jsonStr);
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

    const _INV_LABELS = {
        tipo_equipo:           'Tipo de Equipo',
        total_equipos:         'Total',
        equipos_operativos:    'Operativos',
        equipos_con_falla:     'Con Falla',
        pendiente_reparacion:  'Pend. Reparación',
        pendiente_compra:      'Pend. Compra',
        comentario:            'Comentario',
    };

    // Keys are the LABELS produced by fetch_reports_by_ids (data_mapping in viewer_bp.py)
    const FIVE_Q_MAP = {
        reporte_incidente: {
            QUE:    ['Título de Incidencia', 'Categoría'],
            CUANDO: ['Fecha del Incidente'],
            DONDE:  ['Propiedad', 'Lugar del Incidente'],
            COMO:   ['Nivel Severidad', 'Descripción del Incidente', 'URLs de Imágenes o PDFs'],
            QUIEN:  ['Nombre del Supervisor', 'Responsable Asignado'],
        },
        supervision_puesto: {
            QUE:    ['Puesto/Área'],
            CUANDO: ['Fecha/Hora'],
            DONDE:  ['Cliente/Instalación'],
            COMO:   ['Observaciones', 'Foto Evidencia'],
            QUIEN:  ['Supervisor', 'Nombre Guardia'],
        },
        checklist_cumplimiento: {
            QUE:    ['Curso Certificación', 'Nivel Cumplimiento'],
            CUANDO: ['Fecha/Hora'],
            DONDE:  ['Cliente'],
            COMO:   ['Evidencia URL'],
            QUIEN:  ['Auditor', 'Agente'],
        },
        medicion_experiencia_cliente: {
            QUE:    ['Categoría Evaluada', 'NPS'],
            CUANDO: ['Fecha/Hora'],
            DONDE:  ['Cliente/Instalación'],
            COMO:   ['Observaciones', 'Atención al Cliente'],
            QUIEN:  ['Encuestado'],
        },
        informe_novedades_disciplinario: {
            QUE:    ['Tipo Novedad'],
            CUANDO: ['Fecha/Hora'],
            DONDE:  ['Sitio'],
            COMO:   ['Descripción'],
            QUIEN:  ['Empleado', 'Responsable'],
        },
        log_de_patrullas: {
            QUE:    ['Nivel Riesgo', 'Estado'],
            CUANDO: ['Fecha', 'Hora Inicio', 'Hora Fin'],
            DONDE:  ['Sitio'],
            COMO:   ['Detalles Incidente', 'Riesgo Detectado', 'Contexto'],
            QUIEN:  ['Guardia'],
        },
        registro_de_capacitaciones: {
            QUE:    ['Capacitación', 'Objetivo'],
            CUANDO: ['Fecha/Hora'],
            DONDE:  [],
            COMO:   ['Observaciones', 'Nivel Comprensión', 'URLs de Imágenes o PDFs'],
            QUIEN:  ['Responsable'],
        },
        registro_y_acta_de_visita: {
            QUE:    ['Motivo', 'Objetivo'],
            CUANDO: ['Fecha/Hora'],
            DONDE:  ['Cliente'],
            COMO:   ['Temas Tratados', 'Acuerdos'],
            QUIEN:  ['Visitante', 'Atendió'],
        },
        planilla_vehicular: {
            QUE:    ['Placa', 'Kilometraje'],
            CUANDO: ['Fecha/Hora'],
            DONDE:  [],
            COMO:   ['Novedades Críticas', 'Diagrama Daños', 'Acción Inmediata'],
            QUIEN:  ['Responsable'],
        },
        planilla_motocicletas: {
            QUE:    ['Placa', 'Kilometraje'],
            CUANDO: ['Fecha/Hora'],
            DONDE:  [],
            COMO:   ['Novedades Críticas', 'Acción Inmediata'],
            QUIEN:  ['Responsable'],
        },
        confiabilidad_equipos: {
            QUE:    ['Inventario'],
            CUANDO: ['Fecha', 'Hora'],
            DONDE:  ['Sitio', 'Cliente'],
            COMO:   [],
            QUIEN:  ['Técnico Mantenimiento', 'Supervisor Seguridad'],
        },
    };

    const FIVE_Q_LABELS = {
        QUE:    { icon: '📋', title: 'QUÉ' },
        CUANDO: { icon: '🕐', title: 'CUÁNDO' },
        DONDE:  { icon: '📍', title: 'DÓNDE' },
        COMO:   { icon: '⚙️', title: 'CÓMO' },
        QUIEN:  { icon: '👤', title: 'QUIÉN' },
    };

    function isInventarioArray(key, arr) {
        if (!arr || !arr.length || !arr[0] || typeof arr[0] !== 'object') return false;
        return String(key || '').toLowerCase().includes('inventario');
    }

    function renderInventarioTable(arr) {
        const allKeys = [...new Set(arr.flatMap(row => Object.keys(row)))];
        const cols = Object.keys(_INV_LABELS).filter(k => allKeys.includes(k));
        const extras = allKeys.filter(k => !_INV_LABELS[k]);
        const headers = [...cols, ...extras];

        const headerRow = headers.map(h =>
            `<th>${escapeHtml(_INV_LABELS[h] || h.replace(/_/g, ' '))}</th>`
        ).join('');

        const bodyRows = arr.map(row => {
            const cells = headers.map(h => {
                const v = row[h];
                const text = (v === null || v === undefined || v === '') ? '—' : String(v);
                return `<td>${escapeHtml(text)}</td>`;
            }).join('');
            return `<tr>${cells}</tr>`;
        }).join('');

        return `
            <div class="drv-inv-table-wrap">
                <table class="drv-inv-table">
                    <thead><tr>${headerRow}</tr></thead>
                    <tbody>${bodyRows}</tbody>
                </table>
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
            if (isInventarioArray(key, value)) return renderInventarioTable(value);
            return renderArray(key, value, depth);
        }
        if (typeof value === 'object') {
            return renderObject(value, depth);
        }
        return renderPrimitive(key, value);
    }

    function render5Questions(d, raw, qmap) {
        const cards = Object.entries(FIVE_Q_LABELS).map(([key, meta]) => {
            const labels = qmap[key] || [];
            const parts = [];
            for (const lbl of labels) {
                const val = raw[lbl];
                if (val !== null && val !== undefined && val !== '') {
                    parts.push(renderValue(lbl, val, 0));
                }
            }
            // CUÁNDO falls back to dateSubmitted; QUIÉN falls back to submittedBy
            if (!parts.length) {
                if (key === 'CUANDO' && d.dateSubmitted) parts.push(escapeHtml(d.dateSubmitted));
                if (key === 'QUIEN'  && d.submittedBy)  parts.push(escapeHtml(d.submittedBy));
            }
            const content = parts.length ? parts.join('<br>') : '<span style="opacity:.45">—</span>';
            return `
                <div class="drv-5q-card">
                    <div class="drv-5q-icon">${meta.icon}</div>
                    <div class="drv-5q-label">${meta.title}</div>
                    <div class="drv-5q-value">${content}</div>
                </div>`;
        }).join('');
        return `<div class="drv-5q-grid">${cards}</div>`;
    }

    function renderRecordDetail(d, currentRecordId, formType) {
        const raw = d.data || d;
        const qmap = FIVE_Q_MAP[formType] || null;

        // ── ZONA 1: las 5 preguntas (destacada, sin scroll) ──
        const zona1 = qmap ? render5Questions(d, raw, qmap) : '';

        // ── ZONA 2: detalle técnico completo, COLAPSADO ──
        const usadas = qmap ? new Set(Object.values(qmap).flat()) : new Set();
        const META_KEYS = new Set(['id', 'formType', 'submittedBy', 'dateSubmitted', 'title']);

        const metaRows = `
            <div class="drv-detail-field"><label>ID</label><p>${escapeHtml(d.id || currentRecordId || '—')}</p></div>
            <div class="drv-detail-field"><label>Enviado por</label><p>${escapeHtml(d.submittedBy || '—')}</p></div>
            <div class="drv-detail-field"><label>Fecha de envío</label><p>${escapeHtml(d.dateSubmitted || '—')}</p></div>
            <div class="drv-detail-field"><label>Formulario</label><p>${escapeHtml(d.title || 'Registro')}</p></div>`;

        const rows = Object.entries(raw)
            .filter(([k]) => !META_KEYS.has(k) && !usadas.has(k))
            .map(([k, v]) => `
                <div class="drv-detail-field">
                    <label>${escapeHtml(k)}</label>
                    <div>${renderValue(k, v, 0)}</div>
                </div>`).join('');

        const zona2 = `
            <details class="drv-detail-section" ${qmap ? '' : 'open'}>
                <summary><h4 style="display:inline">Detalle técnico completo</h4></summary>
                <div class="drv-detail-grid" style="margin-top:0.85rem;">
                    ${metaRows}
                    ${rows}
                </div>
            </details>`;

        return zona1 + zona2;
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
        let _currentRecord  = null;

        function closeRecord() {
            modal.classList.remove('active');
            emailOverlay.classList.remove('active');
            currentRecordId = null;
            _currentRecord  = null;
        }

        async function openRecord(id) {
            currentRecordId = id;
            _currentRecord  = null;
            titleEl.textContent = cfg.recordTitle || 'Detalle del Registro';
            modal.classList.add('active');
            contentEl.style.display = 'none';
            loadingEl.style.display = 'block';
            actionBar.style.display = 'none';
            emailOverlay.classList.remove('active');
            try {
                const res = await fetch(`/viewer/api/report/${id}?form_type=${encodeURIComponent(cfg.formType)}`, {
                    credentials: 'include'
                });
                const data = await res.json();
                if (!res.ok || !data) {
                    showToast('drv-error-toast',
                        'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#7f1d1d;color:#fca5a5;border:1px solid #ef4444;border-radius:10px;padding:.75rem 1.25rem;font-size:.8125rem;font-family:Roboto,sans-serif;z-index:9999;max-width:90vw;box-shadow:0 8px 24px rgba(0,0,0,.4);',
                        'No se encontró el registro.'
                    );
                    return;
                }
                _currentRecord = data;
                contentEl.innerHTML = renderRecordDetail(data, currentRecordId, cfg.formType);
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
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-TOKEN': getCsrfToken()
                    },
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
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-TOKEN': getCsrfToken()
                    },
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

        const btnAsignar      = modal.querySelector('.drv-btn-asignar');
        const btnVisita       = modal.querySelector('.drv-btn-visita');
        const asignarOverlay  = modal.querySelector('.drv-asignar-overlay');
        const asignarSelect   = modal.querySelector('.drv-asignar-select');
        const asignarFecha    = modal.querySelector('.drv-asignar-fecha');
        const asignarNota     = modal.querySelector('.drv-asignar-nota');
        const asignarMsg      = modal.querySelector('.drv-asignar-msg');

        if (cfg.formType === 'reporte_incidente') {
            btnAsignar.style.display = '';
            btnVisita.style.display  = '';
        }

        let _usuariosCached = null;

        async function showAsignarOverlay() {
            asignarMsg.textContent = '';
            asignarFecha.value = '';
            asignarNota.value = '';
            asignarOverlay.classList.add('active');

            if (!_usuariosCached) {
                asignarSelect.innerHTML = '<option value="">Cargando…</option>';
                try {
                    const res = await fetch('/cgeo/api/usuarios-asignables', { credentials: 'include' });
                    const data = await res.json();
                    _usuariosCached = data.usuarios || [];
                } catch (_) {
                    _usuariosCached = [];
                }
            }
            asignarSelect.innerHTML = '<option value="">— Seleccionar responsable —</option>' +
                _usuariosCached.map(u =>
                    `<option value="${escapeHtml(String(u.id))}">${escapeHtml(u.name)} (${escapeHtml(u.email)})</option>`
                ).join('');
        }

        function hideAsignarOverlay() {
            asignarOverlay.classList.remove('active');
        }

        async function submitAsignacion() {
            const asignadoA = asignarSelect.value;
            if (!asignadoA) {
                asignarMsg.textContent = 'Seleccione un responsable.';
                asignarMsg.style.color = '#fca5a5';
                return;
            }
            const confirmBtn = modal.querySelector('.drv-btn-asignar-confirm');
            confirmBtn.disabled = true;
            asignarMsg.textContent = 'Guardando…';
            asignarMsg.style.color = '#94a3b8';
            try {
                const res = await fetch('/cgeo/api/asignar-hallazgo', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-TOKEN': getCsrfToken()
                    },
                    body: JSON.stringify({
                        form_type:    cfg.formType,
                        record_id:    currentRecordId,
                        asignado_a:   parseInt(asignadoA, 10),
                        fecha_limite: asignarFecha.value || null,
                        nota:         asignarNota.value.trim() || null,
                    })
                });
                const data = await res.json();
                if (!res.ok || !data.success) throw new Error(data.error || 'Error al asignar.');
                hideAsignarOverlay();
                showToast('drv-success-toast',
                    'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#14532d;color:#86efac;border:1px solid #22c55e;border-radius:10px;padding:.75rem 1.25rem;font-size:.8125rem;font-family:Roboto,sans-serif;z-index:9999;max-width:90vw;box-shadow:0 8px 24px rgba(0,0,0,.4);',
                    `Hallazgo asignado a ${data.responsable} correctamente.`
                );
            } catch (err) {
                asignarMsg.textContent = err.message;
                asignarMsg.style.color = '#fca5a5';
            } finally {
                confirmBtn.disabled = false;
            }
        }

        btnAsignar.onclick = showAsignarOverlay;
        btnVisita.onclick = () => {
            const raw = _currentRecord && (_currentRecord.data || _currentRecord);
            const idPropiedad = raw && (raw['ID Propiedad'] || raw['id_propiedad'] || '');
            const titulo      = raw && (raw['Título de Incidencia'] || '');
            const categoria   = raw && (raw['Categoría'] || '');
            const descripcion = raw && (raw['Descripción del Incidente'] || '');
            const partes = [titulo, categoria, descripcion].filter(Boolean);
            const temas  = 'Hallazgos del período:\n- ' + (partes.length ? partes.join('\n- ') : `Registro #${currentRecordId}`);
            const params = new URLSearchParams();
            if (idPropiedad) params.set('id_propiedad', idPropiedad);
            params.set('motivo', 'Seguimiento de servicio');
            params.set('temas', temas);
            window.location = `/forms/registro_y_acta_de_visita?${params}`;
        };
        modal.querySelector('.drv-asignar-cancel').onclick = hideAsignarOverlay;
        modal.querySelector('.drv-btn-asignar-confirm').onclick = submitAsignacion;
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
