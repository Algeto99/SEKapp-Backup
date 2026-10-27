/**
 * Agendar visita — modal compartido.
 *
 * Programa una visita a una instalación como una asignación de "Asignar
 * hallazgo" (form_type 'visita_programada', record_id = la instalación,
 * fecha_limite = fecha de la visita): responsable (usuario o correo externo),
 * correo, bandeja de Hallazgos Asignados y alerta pendiente/vencida. El acta se
 * registra después, desde la vista de la visita, ya prellenada con el motivo
 * (va al final del título, "… — Motivo") y la situación identificada.
 *
 * Nació en cgeo_operacion.html (Tendencia Operacional, KANAN 2026-10-19) y se
 * extrajo aquí para que el botón "Agendar visita" del visor de registros de los
 * dashboards (dashboard-record-viewer.js) abra el mismo agendamiento en vez de
 * mandar al Acta de Visita a crear un registro nuevo (KANAN 2026-10-26). Una
 * sola caja, una sola validación y un solo contrato con /cgeo/api/asignar-hallazgo.
 *
 * Uso:
 *   AgendarVisita.abrir({
 *       instalaciones:   [{id, name, cliente}]   // opcional: sin lista, se cargan de /cgeo/api/filtros
 *       propiedadId:     123,                    // opcional: preselección por id
 *       propiedadNombre: 'Bodega Norte',         // opcional: preselección por nombre
 *       clienteNombre:   'Cliente S.A.',         //   (desempata nombres repetidos)
 *       contexto:        'texto de la situación identificada',
 *       motivo:          'Seguimiento de servicio',   // opcional
 *       onProgramada:    (respuesta, payload) => {},  // opcional, tras programar
 *   });
 *
 * Requiere sesión (los endpoints exigen JWT); la caja es blanca en ambos temas,
 * como la del QR en Operación.
 */
(function () {
    'use strict';

    const EXTERNO = '__external__';
    // Las mismas opciones del Acta de Visita (acta_visita_cliente.html).
    const MOTIVOS = [
        'Seguimiento de servicio',
        'Auditoría de seguridad',
        'Atención de quejas o reclamos',
        'Presentación de propuesta',
        'Reunión de coordinación',
    ];

    let _usuarios    = null;   // usuarios asignables, cargados una vez por página
    let _propiedades = null;   // instalaciones de /cgeo/api/filtros, una vez por página
    let _abierto     = null;   // lo que recibió abrir(): instalaciones, contexto, callback

    function csrf() {
        const row = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('csrf_access_token='));
        return row ? decodeURIComponent(row.substring('csrf_access_token='.length)) : '';
    }

    function esc(v) {
        return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function norm(v) {
        return String(v ?? '').trim().toLowerCase();
    }

    function ensureStyles() {
        if (document.getElementById('ag-styles')) return;
        const style = document.createElement('style');
        style.id = 'ag-styles';
        // Por encima del visor de registros (.drv-modal-backdrop, 550) y de los
        // menús del sidebar. Clases propias (.ag-input, .ag-btn) y tres niveles
        // de selector para que ningún tema oscuro de la página las pise.
        style.textContent = `
.ag-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;display:none;align-items:center;justify-content:center;padding:1rem}
.ag-backdrop.active{display:flex}
.ag-box{background:#fff;border-radius:1rem;padding:1.5rem;max-width:560px;width:100%;max-height:92vh;overflow:auto;box-shadow:0 25px 60px rgba(0,0,0,.3);color:#0f172a;font-family:'Inter',Roboto,sans-serif;box-sizing:border-box}
.ag-title{font-family:'Merriweather',serif;font-size:1.125rem;font-weight:700;color:#0f172a}
.ag-sub{font-size:.78rem;color:#64748b;margin:.15rem 0 1rem}
.ag-ctx{background:#f5f3ff;border:1px solid #ddd6fe;border-radius:.6rem;padding:.75rem .9rem;font-size:.78rem;color:#4c1d95;line-height:1.5;margin-bottom:1rem;white-space:pre-line}
.ag-ctx strong{display:block;font-size:.7rem;letter-spacing:.05em;text-transform:uppercase;color:#6d28d9;margin-bottom:.25rem}
.ag-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
.ag-field{display:flex;flex-direction:column;gap:.3rem;min-width:0}
.ag-field.full{grid-column:1 / -1}
.ag-label{font-size:.72rem;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:.04em}
.ag-backdrop .ag-box .ag-input{width:100%;background:#fff;border:1px solid #e2e8f0;color:#334155;border-radius:.5rem;font-size:.8125rem;padding:0 .75rem;height:36px;box-sizing:border-box;font-family:inherit;margin:0}
.ag-backdrop .ag-box textarea.ag-input{height:auto;min-height:72px;padding:.5rem .75rem;resize:vertical}
.ag-msg{font-size:.78rem;min-height:1.1rem;margin-top:.75rem}
.ag-actions{display:flex;gap:.5rem;justify-content:flex-end;margin-top:1rem;flex-wrap:wrap}
.ag-backdrop .ag-box .ag-btn{height:36px;padding:0 1rem;color:#fff;border:none;border-radius:.5rem;font-size:.8125rem;font-weight:600;cursor:pointer;white-space:nowrap;font-family:inherit;background:#7c3aed}
.ag-backdrop .ag-box .ag-btn.cancel{background:#64748b}
.ag-backdrop .ag-box .ag-btn:disabled{opacity:.6;cursor:default}
.ag-toast{position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);border-radius:10px;padding:.75rem 1.25rem;font-size:.8125rem;font-family:'Inter',Roboto,sans-serif;z-index:9999;max-width:90vw;box-shadow:0 8px 24px rgba(0,0,0,.4);display:none}
.ag-toast.ok{background:#14532d;color:#86efac;border:1px solid #22c55e}
.ag-toast.warn{background:#78350f;color:#fde68a;border:1px solid #f59e0b}
@media (max-width:640px){.ag-grid{grid-template-columns:1fr}.ag-actions .ag-btn{flex:1}}
`;
        document.head.appendChild(style);
    }

    function ensureModal() {
        let modal = document.getElementById('agendarModal');
        if (modal) return modal;
        ensureStyles();
        modal = document.createElement('div');
        modal.id = 'agendarModal';
        modal.className = 'ag-backdrop';
        modal.innerHTML = `
    <div class="ag-box" role="dialog" aria-labelledby="agTitulo">
        <div class="ag-title" id="agTitulo">📅 Agendar visita</div>
        <div class="ag-sub">La visita queda programada, se notifica por correo al responsable y aparece como pendiente en Hallazgos Asignados y en las alertas.</div>
        <div class="ag-ctx"><strong>Situación identificada</strong><span id="agContexto"></span></div>
        <div class="ag-grid">
            <div class="ag-field full">
                <label class="ag-label" for="agPropiedad">Propiedad / Instalación</label>
                <select id="agPropiedad" class="ag-input"></select>
            </div>
            <div class="ag-field full">
                <label class="ag-label" for="agResponsable">Responsable</label>
                <select id="agResponsable" class="ag-input">
                    <option value="">Cargando usuarios…</option>
                </select>
                <input type="email" id="agExtEmail" class="ag-input" placeholder="correo@externo.com" autocomplete="off" style="display:none;margin-top:.3rem">
            </div>
            <div class="ag-field">
                <label class="ag-label" for="agFecha">Fecha de la visita</label>
                <input type="date" id="agFecha" class="ag-input allow-future" data-allow-future="true">
            </div>
            <div class="ag-field">
                <label class="ag-label" for="agMotivo">Motivo</label>
                <select id="agMotivo" class="ag-input">
                    ${MOTIVOS.map(m => `<option>${esc(m)}</option>`).join('')}
                </select>
            </div>
            <div class="ag-field full">
                <label class="ag-label" for="agNota">Nota para el responsable (opcional)</label>
                <textarea id="agNota" class="ag-input" rows="3" placeholder="Instrucciones o contexto adicional…"></textarea>
            </div>
        </div>
        <div class="ag-msg" id="agMsg"></div>
        <div class="ag-actions">
            <button type="button" class="ag-btn cancel" id="agCancelar">Cancelar</button>
            <button type="button" class="ag-btn" id="agConfirmar">Programar visita</button>
        </div>
    </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (event) => { if (event.target === modal) cerrar(); });
        modal.querySelector('#agCancelar').addEventListener('click', cerrar);
        modal.querySelector('#agConfirmar').addEventListener('click', confirmar);
        modal.querySelector('#agResponsable').addEventListener('change', toggleExterno);
        return modal;
    }

    function toast(texto, ok) {
        let t = document.getElementById('agToast');
        if (!t) { t = document.createElement('div'); t.id = 'agToast'; document.body.appendChild(t); }
        t.className = 'ag-toast ' + (ok ? 'ok' : 'warn');
        t.textContent = texto;
        t.style.display = 'block';
        clearTimeout(t._timer);
        t._timer = setTimeout(() => { t.style.display = 'none'; }, 7000);
    }

    function toggleExterno() {
        const ext = document.getElementById('agExtEmail');
        const esExt = document.getElementById('agResponsable').value === EXTERNO;
        ext.style.display = esExt ? '' : 'none';
        if (esExt) ext.focus();
    }

    async function cargarUsuarios() {
        if (_usuarios) return _usuarios;
        try {
            const r = await fetch('/cgeo/api/usuarios-asignables', { credentials: 'include' });
            _usuarios = (await r.json()).usuarios || [];
        } catch (_) { _usuarios = []; }
        return _usuarios;
    }

    // Instalaciones activas de clientes activos, acotadas al ámbito del
    // Coordinador: la misma lista que alimenta los filtros de Operación.
    async function cargarPropiedades() {
        if (_propiedades) return _propiedades;
        try {
            const r = await fetch('/cgeo/api/filtros', { credentials: 'include' });
            _propiedades = (await r.json()).propiedades || [];
        } catch (_) { _propiedades = []; }
        return _propiedades;
    }

    // Preselección: por id; si no, por nombre (y cliente, cuando el nombre se
    // repite entre clientes); si no, la única candidata; si no, en blanco.
    function preseleccion(candidatas, o) {
        if (o.propiedadId && candidatas.some(p => String(p.id) === String(o.propiedadId))) return String(o.propiedadId);
        const nombre = norm(o.propiedadNombre);
        if (nombre) {
            const porNombre = candidatas.filter(p => norm(p.name) === nombre);
            if (porNombre.length === 1) return String(porNombre[0].id);
            if (porNombre.length > 1) {
                const cliente = norm(o.clienteNombre);
                const conCliente = porNombre.find(p => cliente && norm(p.cliente) === cliente);
                return String((conCliente || porNombre[0]).id);
            }
        }
        if (candidatas.length === 1) return String(candidatas[0].id);
        return '';
    }

    async function abrir(opts) {
        const o = opts || {};
        const modal = ensureModal();
        _abierto = { instalaciones: [], contexto: String(o.contexto || ''), onProgramada: o.onProgramada };

        const propSel = modal.querySelector('#agPropiedad');
        propSel.innerHTML = '<option value="">Cargando instalaciones…</option>';
        const resp = modal.querySelector('#agResponsable');
        if (!_usuarios) resp.innerHTML = '<option value="">Cargando usuarios…</option>';
        const fecha = modal.querySelector('#agFecha');
        fecha.value = '';
        const hoy = new Date();
        fecha.min = hoy.getFullYear() + '-' + String(hoy.getMonth() + 1).padStart(2, '0') + '-' + String(hoy.getDate()).padStart(2, '0');
        modal.querySelector('#agMotivo').value = MOTIVOS.includes(o.motivo) ? o.motivo : MOTIVOS[0];
        modal.querySelector('#agNota').value = '';
        modal.querySelector('#agExtEmail').value = '';
        modal.querySelector('#agExtEmail').style.display = 'none';
        modal.querySelector('#agMsg').textContent = '';
        modal.querySelector('#agContexto').textContent = _abierto.contexto;
        modal.querySelector('#agConfirmar').disabled = false;
        modal.classList.add('active');

        // Quien abre puede traer sus instalaciones (Operación: las del cliente
        // filtrado); si no, se ofrecen todas las activas.
        const candidatas = Array.isArray(o.instalaciones) ? o.instalaciones : await cargarPropiedades();
        _abierto.instalaciones = candidatas;
        // El cliente acompaña al nombre sólo cuando la lista mezcla clientes.
        const clientes = new Set(candidatas.map(p => norm(p.cliente)).filter(Boolean));
        const conCliente = clientes.size > 1;
        propSel.innerHTML = '<option value="">— Seleccionar instalación —</option>' + candidatas.map(p =>
            `<option value="${esc(p.id)}">${esc(p.name)}${(conCliente && p.cliente) ? ` (${esc(p.cliente)})` : ''}</option>`).join('');
        propSel.value = preseleccion(candidatas, o);

        const usuarios = await cargarUsuarios();
        resp.innerHTML = '<option value="">— Seleccionar responsable —</option>'
            + usuarios.map(u => `<option value="${esc(u.id)}">${esc(u.name)} (${esc(u.email)})</option>`).join('')
            + `<option value="${EXTERNO}">✉ Ingresar correo externo…</option>`;
    }

    function cerrar() {
        document.getElementById('agendarModal')?.classList.remove('active');
    }

    async function confirmar() {
        const modal = document.getElementById('agendarModal');
        const msg = modal.querySelector('#agMsg');
        const falla = texto => { msg.style.color = '#dc2626'; msg.textContent = texto; };
        const propId  = modal.querySelector('#agPropiedad').value;
        const prop    = ((_abierto && _abierto.instalaciones) || []).find(p => String(p.id) === String(propId));
        const respVal = modal.querySelector('#agResponsable').value;
        const esExt   = respVal === EXTERNO;
        const ext     = modal.querySelector('#agExtEmail').value.trim();
        const fecha   = modal.querySelector('#agFecha').value;
        const motivo  = modal.querySelector('#agMotivo').value;
        const nota    = modal.querySelector('#agNota').value.trim();
        if (!prop)    return falla('Seleccione la instalación a visitar.');
        if (!respVal) return falla('Seleccione un responsable.');
        if (esExt && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(ext)) return falla('Ingrese un correo válido para el responsable externo.');
        if (!fecha)   return falla('Indique la fecha de la visita.');
        if (!motivo)  return falla('Seleccione el motivo de la visita.');

        // Mismo contrato que "Asignar hallazgo": el hallazgo es la visita. El ref
        // lleva instalación y fecha, así que repetir la misma visita al mismo
        // responsable no crea otra fila ni manda otro correo (ya_asignado). El
        // motivo cierra el título ("… — Motivo") y de ahí lo lee la vista para
        // prellenar el acta.
        const cliente = (prop.cliente || '').trim();
        const lugar = (cliente && cliente.toLowerCase() !== String(prop.name).trim().toLowerCase())
            ? `${cliente} · ${prop.name}` : prop.name;
        const payload = {
            form_type:        'visita_programada',
            record_id:        Number(prop.id),
            fecha_limite:     fecha,
            nota:             nota || null,
            hallazgo_ref:     `visita_${prop.id}_${fecha}`,
            hallazgo_titulo:  `Visita programada: ${lugar} — ${motivo}`,
            hallazgo_detalle: _abierto.contexto,
        };
        if (esExt) payload.asignado_email = ext; else payload.asignado_a = parseInt(respVal, 10);

        const btn = modal.querySelector('#agConfirmar');
        btn.disabled = true;
        msg.style.color = '#64748b';
        msg.textContent = 'Programando…';
        try {
            const r = await fetch('/cgeo/api/asignar-hallazgo', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': csrf() },
                body: JSON.stringify(payload),
            });
            const data = await r.json();
            if (!r.ok || !data.success) throw new Error(data.error || 'No se pudo programar la visita.');
            cerrar();
            const fechaTxt = window.FormatoFecha ? window.FormatoFecha.fecha(fecha) : fecha;
            if (data.ya_asignado) {
                toast(`Esta visita ya estaba programada con ${data.responsable} para el ${fechaTxt}`
                    + `${data.asignado_desde ? ` (desde el ${data.asignado_desde})` : ''}. No se envió un nuevo correo.`, false);
            } else {
                toast(`Visita a ${lugar} programada para el ${fechaTxt} con ${data.responsable}. Se le notificó por correo.`, true);
            }
            if (typeof _abierto.onProgramada === 'function') _abierto.onProgramada(data, payload);
        } catch (err) {
            falla(err.message);
        } finally {
            btn.disabled = false;
        }
    }

    window.AgendarVisita = { abrir, cerrar, MOTIVOS };
})();
