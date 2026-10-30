/**
 * form-validation-feedback.js -> Secapp Universal BI Data Integrity & Advisor Engine
 *
 * Arquitectura de Integridad Operativa y Business Intelligence (BI):
 *
 * 1. NIVEL 1 (BI Core Dimensions / Bloqueo Duro e Ineludible):
 *    - Campos críticos que estructuran la base de datos y cálculos de BI:
 *      identidad de propiedad/cliente, fecha/hora, autor/supervisor/rol,
 *      calificaciones cuantitativas (1-5), odómetros/kilometrajes,
 *      totales operativos de inventario, severidad/categoría de incidentes.
 *    - COMPORTAMIENTO: Si falta CUALQUIER campo de Nivel 1, el envío se BLOQUEA
 *      en el acto. Se presenta el modal en modo "Alerta Crítica de Integridad BI"
 *      (borde carmesí, badge ⛔). NO EXISTE opción de "Enviar así".
 *      Al presionar "Entendido, completar datos", se hace foco y scroll automático
 *      al primer campo indispensable faltante.
 *
 * 2. NIVEL 2 (Campos Descriptivos / Accesorios - Advertencia Suave):
 *    - Observaciones adicionales, cheques desglosados de uniforme, anexos
 *      fotográficos secundarios, seriales opcionales, firmas complementarias.
 *    - COMPORTAMIENTO: Si TODOS los campos de Nivel 1 están completos pero faltan
 *      campos de Nivel 2, se presenta el modal en modo "Advertencia de Completitud"
 *      (borde gris/ámbar, badge ⚠️) con 2 alternativas:
 *        [ ← Regresar a llenar ]  y  [ Enviar así → ]
 *
 * 3. TRANSPARENCIA TOTAL:
 *    - Si todos los campos (Nivel 1 y Nivel 2) están completos, el formulario
 *      se envía inmediatamente sin ninguna interrupción.
 */
(function () {
    'use strict';

    if (window.secappFormAdvisorLoaded) return;
    window.secappFormAdvisorLoaded = true;

    var ESTILOS = [
        '#secappIncompleteModal {',
        '  position: fixed;',
        '  inset: 0;',
        '  z-index: 99999;',
        '  display: flex;',
        '  align-items: center;',
        '  justify-content: center;',
        '  padding: 1rem;',
        '  background-color: rgba(0, 0, 0, 0.78);',
        '  backdrop-filter: blur(4px);',
        '  -webkit-backdrop-filter: blur(4px);',
        '}',
        '#secappIncompleteModal.hidden {',
        '  display: none !important;',
        '}',
        '.secapp-inc-card {',
        '  background-color: #111827;',
        '  border: 1px solid rgba(75, 85, 99, 0.7);',
        '  border-radius: 1rem;',
        '  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.65);',
        '  max-width: 34rem;',
        '  width: 100%;',
        '  overflow: hidden;',
        '  display: flex;',
        '  flex-direction: column;',
        '  max-height: 85vh;',
        '  animation: secappIncFadeIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);',
        '}',
        '.secapp-inc-card.is-hard-block {',
        '  border-color: rgba(239, 68, 68, 0.75);',
        '  box-shadow: 0 25px 50px -12px rgba(220, 38, 38, 0.35);',
        '}',
        '@keyframes secappIncFadeIn {',
        '  from { opacity: 0; transform: scale(0.96); }',
        '  to { opacity: 1; transform: scale(1); }',
        '}',
        '.secapp-inc-header {',
        '  padding: 1rem 1.25rem;',
        '  background: rgba(31, 41, 55, 0.9);',
        '  border-bottom: 1px solid rgba(75, 85, 99, 0.6);',
        '  display: flex;',
        '  align-items: center;',
        '  justify-content: space-between;',
        '}',
        '.is-hard-block .secapp-inc-header {',
        '  background: rgba(45, 17, 21, 0.95);',
        '  border-bottom-color: rgba(239, 68, 68, 0.4);',
        '}',
        '.secapp-inc-badge {',
        '  width: 2.6rem;',
        '  height: 2.6rem;',
        '  border-radius: 0.75rem;',
        '  background: rgba(245, 158, 11, 0.15);',
        '  color: #fbbf24;',
        '  display: flex;',
        '  align-items: center;',
        '  justify-content: center;',
        '  font-size: 1.35rem;',
        '  border: 1px solid rgba(245, 158, 11, 0.35);',
        '  flex-shrink: 0;',
        '}',
        '.is-hard-block .secapp-inc-badge {',
        '  background: rgba(239, 68, 68, 0.18);',
        '  color: #ef4444;',
        '  border-color: rgba(239, 68, 68, 0.45);',
        '}',
        '.secapp-inc-body {',
        '  padding: 1.25rem;',
        '  overflow-y: auto;',
        '  color: #d1d5db;',
        '  font-size: 0.875rem;',
        '  line-height: 1.5;',
        '  display: flex;',
        '  flex-direction: column;',
        '  gap: 0.875rem;',
        '}',
        '.secapp-inc-list-box {',
        '  background: rgba(17, 24, 39, 0.75);',
        '  border: 1px solid rgba(55, 65, 81, 0.8);',
        '  border-radius: 0.75rem;',
        '  padding: 0.875rem;',
        '  max-height: 42vh;',
        '  overflow-y: auto;',
        '  display: flex;',
        '  flex-direction: column;',
        '  gap: 0.75rem;',
        '}',
        '.is-hard-block .secapp-inc-list-box {',
        '  border-color: rgba(239, 68, 68, 0.3);',
        '  background: rgba(24, 15, 18, 0.85);',
        '}',
        '.secapp-inc-group {',
        '  border-left: 3px solid #f59e0b;',
        '  padding-left: 0.625rem;',
        '}',
        '.is-hard-block .secapp-inc-group {',
        '  border-left-color: #ef4444;',
        '}',
        '.secapp-inc-group-title {',
        '  font-size: 0.72rem;',
        '  font-weight: 700;',
        '  text-transform: uppercase;',
        '  letter-spacing: 0.05em;',
        '  color: #fbbf24;',
        '  margin-bottom: 0.25rem;',
        '}',
        '.is-hard-block .secapp-inc-group-title {',
        '  color: #f87171;',
        '}',
        '.secapp-inc-item {',
        '  display: flex;',
        '  align-items: center;',
        '  gap: 0.45rem;',
        '  font-size: 0.825rem;',
        '  color: #e5e7eb;',
        '  padding: 0.2rem 0;',
        '  cursor: pointer;',
        '  transition: color 0.15s ease;',
        '}',
        '.secapp-inc-item:hover {',
        '  color: #60a5fa;',
        '  text-decoration: underline;',
        '}',
        '.is-hard-block .secapp-inc-item:hover {',
        '  color: #fca5a5;',
        '}',
        '.secapp-inc-bullet {',
        '  color: #f59e0b;',
        '  font-weight: bold;',
        '}',
        '.is-hard-block .secapp-inc-bullet {',
        '  color: #ef4444;',
        '}',
        '.secapp-inc-tip {',
        '  padding: 0.65rem 0.875rem;',
        '  background: rgba(245, 158, 11, 0.08);',
        '  border: 1px solid rgba(245, 158, 11, 0.25);',
        '  border-radius: 0.625rem;',
        '  font-size: 0.75rem;',
        '  color: #fde68a;',
        '  display: flex;',
        '  align-items: center;',
        '  gap: 0.5rem;',
        '}',
        '.is-hard-block .secapp-inc-tip {',
        '  background: rgba(239, 68, 68, 0.1);',
        '  border-color: rgba(239, 68, 68, 0.35);',
        '  color: #fca5a5;',
        '}',
        '.secapp-inc-footer {',
        '  padding: 0.875rem 1.25rem;',
        '  background: rgba(31, 41, 55, 0.95);',
        '  border-top: 1px solid rgba(75, 85, 99, 0.6);',
        '  display: flex;',
        '  flex-direction: row;',
        '  justify-content: flex-end;',
        '  gap: 0.75rem;',
        '}',
        '.is-hard-block .secapp-inc-footer {',
        '  background: rgba(35, 18, 22, 0.95);',
        '  border-top-color: rgba(239, 68, 68, 0.3);',
        '}',
        '.secapp-btn-secondary {',
        '  padding: 0.625rem 1.125rem;',
        '  background: #374151;',
        '  color: #f9fafb;',
        '  border-radius: 0.625rem;',
        '  font-size: 0.875rem;',
        '  font-weight: 600;',
        '  border: 1px solid rgba(107, 114, 128, 0.5);',
        '  cursor: pointer;',
        '  transition: all 0.15s ease;',
        '}',
        '.secapp-btn-secondary:hover {',
        '  background: #4b5563;',
        '}',
        '.secapp-btn-primary {',
        '  padding: 0.625rem 1.25rem;',
        '  background: #2563eb;',
        '  color: #ffffff;',
        '  border-radius: 0.625rem;',
        '  font-size: 0.875rem;',
        '  font-weight: 700;',
        '  border: 1px solid rgba(37, 99, 235, 0.8);',
        '  cursor: pointer;',
        '  display: flex;',
        '  align-items: center;',
        '  gap: 0.4rem;',
        '  box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35);',
        '  transition: all 0.15s ease;',
        '}',
        '.secapp-btn-primary:hover {',
        '  background: #1d4ed8;',
        '}',
        '.secapp-btn-danger {',
        '  padding: 0.625rem 1.35rem;',
        '  background: #dc2626;',
        '  color: #ffffff;',
        '  border-radius: 0.625rem;',
        '  font-size: 0.875rem;',
        '  font-weight: 700;',
        '  border: 1px solid rgba(220, 38, 38, 0.8);',
        '  cursor: pointer;',
        '  display: flex;',
        '  align-items: center;',
        '  gap: 0.45rem;',
        '  box-shadow: 0 4px 14px rgba(220, 38, 38, 0.4);',
        '  transition: all 0.15s ease;',
        '}',
        '.secapp-btn-danger:hover {',
        '  background: #b91c1c;',
        '}',
        '@keyframes secappFieldPulse {',
        '  0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); }',
        '  70% { box-shadow: 0 0 0 8px rgba(239, 68, 68, 0); }',
        '  100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }',
        '}',
        '.secapp-pulse-highlight {',
        '  animation: secappFieldPulse 1s ease-out 2 !important;',
        '  outline: 2px solid #ef4444 !important;',
        '}'
    ].join('\n');

    function inyectarEstilos() {
        if (document.getElementById('secapp-inc-styles')) return;
        var s = document.createElement('style');
        s.id = 'secapp-inc-styles';
        s.textContent = ESTILOS;
        (document.head || document.documentElement).appendChild(s);
    }

    function texto(valor) {
        return String(valor == null ? '' : valor)
            .replace(/\s+/g, ' ')
            .replace(/[*:·]+$/, '')
            .trim()
            .slice(0, 95);
    }

    function esVisible(el) {
        return !!(el && el.getClientRects && el.getClientRects().length && getComputedStyle(el).display !== 'none');
    }

    function anclaDe(el) {
        for (var n = el; n && n !== document.body; n = n.parentElement) {
            if (esVisible(n)) return n;
        }
        return el;
    }

    function etiquetaDe(el) {
        if (!el) return 'Campo requerido';
        var conDato = el.closest('[data-label]');
        if (conDato && conDato.dataset.label) return texto(conDato.dataset.label);

        if (el.id) {
            try {
                var puesta = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]');
                if (puesta) return texto(puesta.textContent);
            } catch (err) { }
        }

        var host = el.closest('.ss-wrap') || el.closest('.ss-manual') || el;
        for (var n = host.parentElement; n && n.tagName !== 'FORM'; n = n.parentElement) {
            var lab = n.querySelector('label');
            if (lab && texto(lab.textContent)) return texto(lab.textContent);
            if (n.classList && n.classList.contains('form-section')) break;
        }

        return texto(el.getAttribute('aria-label') || el.placeholder || el.name) || 'Campo pendiente';
    }

    function seccionDe(el) {
        if (!el) return 'Datos Principales';
        var bloqueSup = el.closest('.supervision-block');
        if (bloqueSup) {
            var h2 = bloqueSup.querySelector('h2, .block-title');
            if (h2) return texto(h2.textContent);
            var idx = bloqueSup.dataset.index;
            if (idx !== undefined) return 'Supervisión de Puesto #' + (parseInt(idx, 10) + 1);
        }
        var bloqueChk = el.closest('.checklist-block');
        if (bloqueChk) {
            var h3 = bloqueChk.querySelector('h3, .checklist-title');
            if (h3) return texto(h3.textContent);
        }
        var bloqueInv = el.closest('.inventory-row, [data-inventory-row], tr');
        if (bloqueInv) {
            var hInv = bloqueInv.querySelector('.inv-title, [data-row-title]');
            if (hInv) return texto(hInv.textContent);
            return 'Inventario de Equipos';
        }
        var seccion = el.closest('.form-section');
        var titulo = seccion && seccion.querySelector('h2, h3, .section-title');
        return titulo ? texto(titulo.textContent) : 'Datos Principales';
    }

    function irA(el) {
        if (!el) return;
        var ancla = anclaDe(el);
        if (esVisible(el) && typeof el.focus === 'function') {
            try { el.focus({ preventScroll: true }); } catch (err) { }
        }
        try { ancla.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
        catch (err) { ancla.scrollIntoView(true); }

        // Destacar temporalmente con pulso visual
        ancla.classList.add('secapp-pulse-highlight');
        setTimeout(function () {
            ancla.classList.remove('secapp-pulse-highlight');
        }, 2200);
    }

    function isOperationalForm(form) {
        if (!form || form.tagName !== 'FORM') return false;
        if (form.id === 'loginForm' || form.id === 'filtrosForm' || form.id === 'email-form' || form.id === 'sesionForm') return false;
        if (form.action && (form.action.includes('/login') || form.action.includes('/register') || form.action.includes('/password'))) return false;

        var knownIds = [
            'supervisionForm',
            'confiabilidadForm',
            'checklistForm',
            'incidenteForm',
            'novedadesForm',
            'visitaForm',
            'planillaVehicularForm',
            'motocicletaForm',
            'capacitacionForm',
            'experienciaForm',
            'patrullaForm'
        ];
        if (knownIds.indexOf(form.id) !== -1) return true;
        if (form.dataset.advisor === 'true') return true;
        if (form.closest && form.closest('.sidebar-main-content')) return true;
        return false;
    }

    // ── REGLAS DE INTEGRIDAD DE BUSINESS INTELLIGENCE (NIVEL 1) ─────────────
    // Cada regla define los campos indispensables para no corromper la BD ni los tableros.
    var BI_SPECS = {
        'supervisionForm': {
            commonSelectors: [
                '#id_propiedad, [name="id_propiedad"]',
                '[name="fecha_hora"]',
                '#supervisor, [name="supervisor"]',
                '#rol_aplicador, [name="rol_aplicador"]'
            ],
            customValidation: function (form, l1) {
                var isEdit = !!(window.EDIT_MODE || (form.action && form.action.indexOf('_editar') !== -1));
                if (isEdit) {
                    var puesto = form.querySelector('[name="detalles_puestos"], #detalles_puestos');
                    if (puesto && !puesto.value.trim()) {
                        l1.push({ element: puesto, label: 'Puesto o Área Específica', group: 'Datos de la Supervisión' });
                    }
                    var mod = form.querySelector('[name="modalidad_servicio"], #modalidadServicio');
                    if (mod && !mod.value.trim()) {
                        l1.push({ element: mod, label: 'Modalidad de Servicio', group: 'Datos de la Supervisión' });
                    }
                    var guard = form.querySelector('[name="nombre_guardia"]');
                    if (guard && !guard.value.trim()) {
                        l1.push({ element: guard, label: 'Nombre del Guardia de Seguridad', group: 'Datos de la Supervisión' });
                    }
                    var ratings = [
                        { n: 'asistencia_puntualidad', l: 'Inspección: Asistencia y puntualidad' },
                        { n: 'presentacion_uniforme', l: 'Inspección: Presentación y uniforme' },
                        { n: 'estado_limpieza_puesto', l: 'Inspección: Estado y limpieza del puesto' },
                        { n: 'equipamiento_completo', l: 'Inspección: Equipamiento' },
                        { n: 'estado_bitacora', l: 'Inspección: Estado de bitácora y registros' }
                    ];
                    ratings.forEach(function (r) {
                        var sel = form.querySelector('[name="' + r.n + '"]');
                        if (sel && (!sel.value || sel.value.trim() === '')) {
                            l1.push({ element: sel, label: r.l, group: 'Inspección de Puesto (BI)' });
                        }
                    });
                    var mot = form.querySelector('#motivo, [name="motivo"]');
                    if (mot && !mot.value.trim()) {
                        l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                    }
                } else {
                    var blocks = form.querySelectorAll('.supervision-block');
                    if (blocks.length === 0) {
                        l1.push({ element: form, label: 'Debe agregar al menos una supervisión de puesto', group: 'Supervisiones' });
                    }
                    blocks.forEach(function (b) {
                        var idx = b.dataset.index || '0';
                        var puestoInput = b.querySelector('[name="supervisions[' + idx + '][detalles_puestos]"]');
                        var puestoNombre = (puestoInput && puestoInput.value.trim()) ? puestoInput.value.trim() : null;
                        var grp = puestoNombre ? 'Supervisión #' + (parseInt(idx, 10) + 1) + ' (' + puestoNombre + ')' : 'Supervisión #' + (parseInt(idx, 10) + 1);

                        if (!puestoInput || !puestoInput.value.trim()) {
                            l1.push({ element: puestoInput || b, label: 'Puesto o Área Específica', group: grp });
                        }
                        var modSel = b.querySelector('[name="supervisions[' + idx + '][modalidad_servicio]"]');
                        if (!modSel || !modSel.value.trim()) {
                            l1.push({ element: modSel || b, label: 'Modalidad de Servicio', group: grp });
                        }
                        var guardInput = b.querySelector('[name="supervisions[' + idx + '][nombre_guardia]"]');
                        if (!guardInput || !guardInput.value.trim()) {
                            l1.push({ element: guardInput || b, label: 'Nombre del Guardia de Seguridad', group: grp });
                        }
                        var blockRatings = [
                            { n: 'asistencia_puntualidad', l: 'Inspección: Asistencia y puntualidad' },
                            { n: 'presentacion_uniforme', l: 'Inspección: Presentación y uniforme' },
                            { n: 'estado_limpieza_puesto', l: 'Inspección: Estado y limpieza del puesto' },
                            { n: 'equipamiento_completo', l: 'Inspección: Equipamiento' },
                            { n: 'estado_bitacora', l: 'Inspección: Estado de bitácora y registros' }
                        ];
                        blockRatings.forEach(function (r) {
                            var sel = b.querySelector('[name="supervisions[' + idx + '][' + r.n + ']"]');
                            if (!sel || !sel.value || sel.value.trim() === '') {
                                l1.push({ element: sel || b, label: r.l, group: grp });
                            }
                        });
                    });
                }
            }
        },

        'confiabilidadForm': {
            commonSelectors: [
                '[name="id_propiedad"], #id_propiedad',
                '[name="fecha"], #fecha'
            ],
            customValidation: function (form, l1) {
                var sup = form.querySelector('[name="supervisor_seguridad"]') || form.querySelector('[name="tecnico_mantenimiento"]');
                if (sup && !sup.value.trim()) {
                    l1.push({ element: sup, label: 'Supervisor / Técnico Responsable', group: 'Datos Principales' });
                }
                var invTypes = form.querySelectorAll('input[name*="[tipo_equipo]"], select[name*="[tipo_equipo]"]');
                if (invTypes.length === 0) {
                    l1.push({ element: form, label: 'Debe agregar al menos una fila en el inventario de equipos', group: 'Inventario de Equipos (BI)' });
                } else {
                    var hasFilledRow = false;
                    invTypes.forEach(function (tInput) {
                        var val = (tInput.value || '').trim();
                        if (val !== '' && val !== 'Seleccione...') {
                            hasFilledRow = true;
                            var row = tInput.closest('.inventory-row, tr') || tInput.parentElement;
                            var totInput = row ? row.querySelector('[name*="[total_equipos]"]') : null;
                            var opInput = row ? row.querySelector('[name*="[equipos_operativos]"]') : null;

                            var tNum = totInput ? parseInt(totInput.value, 10) : NaN;
                            var oNum = opInput ? parseInt(opInput.value, 10) : NaN;

                            if (isNaN(tNum) || tNum < 0) {
                                l1.push({ element: totInput || tInput, label: 'Total de equipos (entero ≥ 0) para ' + val, group: 'Inventario de Equipos (BI)' });
                            }
                            if (isNaN(oNum) || oNum < 0) {
                                l1.push({ element: opInput || tInput, label: 'Equipos operativos (entero ≥ 0) para ' + val, group: 'Inventario de Equipos (BI)' });
                            } else if (!isNaN(tNum) && oNum > tNum) {
                                l1.push({ element: opInput || tInput, label: 'Equipos operativos no puede exceder el total para ' + val, group: 'Inventario de Equipos (BI)' });
                            }
                        }
                    });
                    if (!hasFilledRow) {
                        l1.push({ element: invTypes[0], label: 'Seleccione el tipo de equipo para al menos un registro', group: 'Inventario de Equipos (BI)' });
                    }
                }
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'checklistForm': {
            commonSelectors: [
                '[name="id_propiedad"], #id_propiedad',
                '[name="puesto_area_especifica"]',
                '[name="fecha_hora"]',
                '[name="rol_aplicador"]',
                '[name="nombre_auditor"]'
            ],
            customValidation: function (form, l1) {
                var blocks = form.querySelectorAll('.checklist-block');
                blocks.forEach(function (b, i) {
                    var grp = 'Checklist #' + (i + 1);
                    var nom = b.querySelector('[name^="agente_nombre_completo"]');
                    if (nom && !nom.value.trim()) {
                        l1.push({ element: nom, label: 'Nombre Completo del Agente', group: grp });
                    }
                    var niv = b.querySelector('[name^="nivel_cumplimiento"]');
                    if (niv && (!niv.value || niv.value.trim() === '')) {
                        l1.push({ element: niv, label: 'Nivel de Cumplimiento (BI)', group: grp });
                    }
                });
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'incidenteForm': {
            commonSelectors: [
                '[name="id_propiedad"], #id_propiedad',
                '[name="puesto_area_especifica"]',
                '[name="fecha_hora"]',
                '[name="rol_aplicador"]',
                '[name="nombre_responsable"]',
                '[name="categoria"]',
                '[name="nivel_severidad"]',
                '[name="descripcion"]'
            ],
            customValidation: function (form, l1) {
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'novedadesForm': {
            commonSelectors: [
                '[name="id_propiedad"], #id_propiedad',
                '[name="fecha_hora"]',
                '[name="rol_aplicador"]',
                '[name="nombre_responsable"]',
                '[name="empleado_nombre"]',
                '[name="tipo_novedad"]',
                '[name="descripcion_novedad"]'
            ],
            customValidation: function (form, l1) {
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'visitaForm': {
            commonSelectors: [
                '[name="id_propiedad"], #id_propiedad',
                '[name="fecha_hora"]',
                '[name="motivo_visita"]',
                '[name="nombre_visitante"]',
                '[name="cliente_visitado"]'
            ],
            customValidation: function (form, l1) {
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'planillaVehicularForm': {
            commonSelectors: [
                '[name="id_propiedad"], [name="cliente_instalacion"]',
                '[name="rol_aplicador"]',
                '[name="nombre_responsable"]',
                '[name="fecha_hora"]',
                '[name="vehiculo_tipo"]',
                '[name="placa_vehiculo"]',
                '[name="kilometraje_vehiculo"]'
            ],
            customValidation: function (form, l1) {
                var km = form.querySelector('[name="kilometraje_vehiculo"]');
                if (km && km.value.trim() !== '') {
                    var s = km.value.replace(/[.,\s]/g, '');
                    if (!/^\d+$/.test(s)) {
                        l1.push({ element: km, label: 'Kilometraje debe ser un número entero válido', group: 'Datos del Vehículo (BI)' });
                    }
                }
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'motocicletaForm': {
            commonSelectors: [
                '[name="id_propiedad"], [name="cliente_instalacion"]',
                '[name="rol_aplicador"]',
                '[name="nombre_responsable"]',
                '[name="fecha_hora"]',
                '[name="motocicleta_tipo"]',
                '[name="placa_motocicleta"]',
                '[name="kilometraje_motocicleta"]'
            ],
            customValidation: function (form, l1) {
                var km = form.querySelector('[name="kilometraje_motocicleta"]');
                if (km && km.value.trim() !== '') {
                    var s = km.value.replace(/[.,\s]/g, '');
                    if (!/^\d+$/.test(s)) {
                        l1.push({ element: km, label: 'Kilometraje debe ser un número entero válido', group: 'Datos de la Motocicleta (BI)' });
                    }
                }
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'capacitacionForm': {
            commonSelectors: [
                '[name="id_propiedad"], #id_propiedad',
                '[name="fecha_hora"]',
                '[name="tema_capacitacion"]',
                '[name="nombre_responsable"]'
            ],
            customValidation: function (form, l1) {
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'experienciaForm': {
            commonSelectors: [
                '[name="id_propiedad"], #id_propiedad',
                '[name="fecha_hora"]',
                '[name="atencion_cliente"]',
                '[name="comunicacion"]',
                '[name="confiabilidad"]',
                '[name="capacidad_reaccion"]',
                '[name="cumplimiento"]',
                '[name="competencia_personal"]'
            ],
            customValidation: function (form, l1) {
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        },

        'patrullaForm': {
            commonSelectors: [
                '[name="sitio_ubicacion"], [name="id_propiedad"]',
                '[name="fecha"]',
                '[name="hora_inicio"]',
                '[name="hora_fin"]',
                '[name="id_guardia_nombre_guardia"]',
                '[name="estado_patrulla"]'
            ],
            customValidation: function (form, l1) {
                var mot = form.querySelector('#motivo, [name="motivo"]');
                if (mot && !mot.value.trim()) {
                    l1.push({ element: mot, label: 'Motivo de modificación (Auditoría)', group: 'Auditoría de Edición' });
                }
            }
        }
    };

    /**
     * Escanea el formulario separando estrictamente:
     * - missingLevel1: Campos indispensables para integridad de BI (Bloqueo Duro).
     * - missingLevel2: Campos descriptivos / accesorios (Advertencia Suave).
     */
    function scanForm(form) {
        var missingLevel1 = [];
        var missingLevel2 = [];
        var level1ElementsSet = [];

        var spec = BI_SPECS[form.id];

        // 1. Evaluar selectores comunes de Nivel 1
        if (spec && spec.commonSelectors) {
            spec.commonSelectors.forEach(function (selector) {
                var el = form.querySelector(selector);
                if (el && !el.disabled) {
                    level1ElementsSet.push(el);
                    var val = (el.value || '').trim();
                    if (!val || val === 'Seleccione...' || val === '') {
                        missingLevel1.push({
                            element: anclaDe(el),
                            label: etiquetaDe(el),
                            group: seccionDe(el),
                            level: 1
                        });
                    }
                }
            });
        }

        // 2. Ejecutar validación personalizada de Nivel 1 (bloques dinámicos, ratings 1-5, odómetros, etc.)
        if (spec && typeof spec.customValidation === 'function') {
            spec.customValidation(form, missingLevel1);
        }

        // 3. Evaluar controles de Nivel 2 (accesorios, recomendados, firmas, fotos)
        var seenNames = {};
        var allControls = form.querySelectorAll('input, select, textarea');
        allControls.forEach(function (el) {
            if (el.disabled || el.type === 'button' || el.type === 'submit' || el.type === 'reset' || el.name === 'csrf_token') {
                return;
            }

            // Si ya fue catalogado o pertenece a Nivel 1, no duplicar en Nivel 2
            if (level1ElementsSet.indexOf(el) !== -1) return;

            var isPhotoCard = el.type === 'file' && (el.required || el.dataset.required === 'true' || el.dataset.recommended === 'true');
            if (el.type === 'hidden' && !isPhotoCard) return;

            // En este punto, cualquier campo con required o data-recommended que no sea Nivel 1 se considera Nivel 2
            var isCandidate = el.hasAttribute('required') || el.dataset.required === 'true' || el.dataset.recommended === 'true';
            if (!isCandidate) return;

            // Radio groups
            if (el.type === 'radio') {
                if (seenNames[el.name]) return;
                seenNames[el.name] = true;
                var checked = form.querySelector('input[type="radio"][name="' + CSS.escape(el.name) + '"]:checked');
                if (!checked) {
                    missingLevel2.push({
                        element: anclaDe(el),
                        label: etiquetaDe(el),
                        group: seccionDe(el),
                        level: 2
                    });
                }
                return;
            }

            // Checkboxes individuales requeridos
            if (el.type === 'checkbox') {
                if (!el.checked) {
                    missingLevel2.push({
                        element: anclaDe(el),
                        label: etiquetaDe(el),
                        group: seccionDe(el),
                        level: 2
                    });
                }
                return;
            }

            // Fotos / Archivos
            if (el.type === 'file') {
                var hasFiles = el.files && el.files.length > 0;
                var card = el.closest('.photo-card') || el.closest('[data-photo-preview]') || el.parentElement;
                var hasPreview = card && (card.querySelector('img') || card.classList.contains('has-photo'));
                if (!hasFiles && !hasPreview) {
                    missingLevel2.push({
                        element: anclaDe(el),
                        label: etiquetaDe(el),
                        group: seccionDe(el),
                        level: 2
                    });
                }
                return;
            }

            // Selects
            if (el.tagName === 'SELECT') {
                var sVal = (el.value || '').trim();
                if (!sVal || sVal === '' || sVal === 'Seleccione...') {
                    missingLevel2.push({
                        element: anclaDe(el),
                        label: etiquetaDe(el),
                        group: seccionDe(el),
                        level: 2
                    });
                }
                return;
            }

            // Textos y textareas
            var tVal = (el.value || '').trim();
            if (!tVal) {
                missingLevel2.push({
                    element: anclaDe(el),
                    label: etiquetaDe(el),
                    group: seccionDe(el),
                    level: 2
                });
            }
        });

        // 4. Firmas accesorias en SignaturePad
        var sigCanvases = form.querySelectorAll('canvas.signature-pad, canvas[id*="signaturePad"], canvas[id*="sigCanvas"], canvas[id*="Canvas"], canvas[id*="Firma"]');
        sigCanvases.forEach(function (canvas) {
            if (!esVisible(canvas)) return;
            var padKey = canvas.id;
            var padInstance = (window.signaturePads && window.signaturePads[padKey])
                || window[padKey]
                || window.signaturePad
                || window.pad;

            var isBlank = false;
            if (padInstance && typeof padInstance.isEmpty === 'function') {
                isBlank = padInstance.isEmpty();
            }

            if (isBlank) {
                missingLevel2.push({
                    element: canvas,
                    label: etiquetaDe(canvas) || 'Firma de conformidad',
                    group: seccionDe(canvas) || 'Firmas y Conformidad',
                    level: 2
                });
            }
        });

        return {
            level1: missingLevel1,
            level2: missingLevel2
        };
    }

    var activeForm = null;
    var firstPendingElement = null;

    function obtenerOInyectarModal() {
        var modal = document.getElementById('secappIncompleteModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'secappIncompleteModal';
            modal.className = 'hidden';
            modal.setAttribute('role', 'dialog');
            modal.setAttribute('aria-modal', 'true');
            modal.setAttribute('aria-labelledby', 'incompleteModalTitle');
            modal.innerHTML = [
                '<div class="secapp-inc-card" id="secappIncCard">',
                '  <div class="secapp-inc-header">',
                '    <div style="display:flex;align-items:center;gap:0.75rem;">',
                '      <div class="secapp-inc-badge" id="incompleteModalBadge">⚠️</div>',
                '      <div>',
                '        <h3 id="incompleteModalTitle" style="margin:0;font-size:1.05rem;font-weight:700;color:#ffffff;">Campos pendientes de completar</h3>',
                '        <p id="incompleteModalSubtitle" style="margin:0;font-size:0.75rem;color:#9ca3af;">Verifique antes de enviar el informe</p>',
                '      </div>',
                '    </div>',
                '    <button type="button" id="incompleteCloseBtn" style="background:none;border:none;color:#9ca3af;font-size:1.5rem;line-height:1;cursor:pointer;padding:0.25rem;" aria-label="Cerrar">&times;</button>',
                '  </div>',
                '  <div class="secapp-inc-body">',
                '    <p id="incompleteModalDesc" style="margin:0;color:#e5e7eb;font-weight:500;">',
                '      Los siguientes datos no han sido completados en este informe:',
                '    </p>',
                '    <div id="incompleteListContainer" class="secapp-inc-list-box">',
                '    </div>',
                '    <div class="secapp-inc-tip" id="incompleteModalTip">',
                '      <span id="incompleteModalTipIcon">💡</span>',
                '      <span id="incompleteModalTipText">Puede regresar a completar los datos o enviar el reporte tal como está.</span>',
                '    </div>',
                '  </div>',
                '  <div class="secapp-inc-footer" id="incompleteModalFooter">',
                '    <button type="button" id="incompleteReturnBtn" class="secapp-btn-secondary">',
                '      ← Regresar a llenar',
                '    </button>',
                '    <button type="button" id="incompleteSubmitAnywayBtn" class="secapp-btn-primary">',
                '      <span>Enviar así</span>',
                '      <svg xmlns="http://www.w3.org/2000/svg" style="width:1rem;height:1rem;" fill="none" viewBox="0 0 24 24" stroke="currentColor">',
                '        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M14 5l7 7m0 0l-7 7m7-7H3" />',
                '      </svg>',
                '    </button>',
                '  </div>',
                '</div>'
            ].join('\n');
            document.body.appendChild(modal);
        }
        return modal;
    }

    /**
     * Muestra el modal configurando dinámicamente si es Bloqueo Duro (Nivel 1)
     * o Advertencia Suave (Nivel 2).
     */
    function mostrarModal(form, isHardBlock, missingItems) {
        inyectarEstilos();
        var modal = obtenerOInyectarModal();
        activeForm = form;
        firstPendingElement = missingItems[0] ? missingItems[0].element : null;

        var card = modal.querySelector('#secappIncCard') || modal.querySelector('.secapp-inc-card');
        var badge = modal.querySelector('#incompleteModalBadge');
        var title = modal.querySelector('#incompleteModalTitle');
        var subtitle = modal.querySelector('#incompleteModalSubtitle');
        var desc = modal.querySelector('#incompleteModalDesc');
        var tipIcon = modal.querySelector('#incompleteModalTipIcon');
        var tipText = modal.querySelector('#incompleteModalTipText');
        var returnBtn = modal.querySelector('#incompleteReturnBtn');
        var submitAnywayBtn = modal.querySelector('#incompleteSubmitAnywayBtn');
        var listContainer = modal.querySelector('#incompleteListContainer');

        if (isHardBlock) {
            // MODO BLOQUEO DURO (NIVEL 1 - INTEGRIDAD DE BI)
            if (card) card.classList.add('is-hard-block');
            if (badge) badge.textContent = '⛔';
            if (title) title.textContent = 'Datos Críticos Obligatorios (Integridad de BI)';
            if (subtitle) subtitle.textContent = 'Dimensiones y métricas indispensables para el sistema';
            if (desc) desc.textContent = 'Los siguientes campos alimentan directamente los modelos de datos, indicadores y tableros de BI. Por seguridad y congruencia del sistema, deben completarse antes de guardar:';
            if (tipIcon) tipIcon.textContent = '🔒';
            if (tipText) tipText.textContent = 'Ninguna acción de guardado puede realizarse omitiendo datos críticos de BI.';

            if (returnBtn) {
                returnBtn.className = 'secapp-btn-danger';
                returnBtn.textContent = 'Entendido, completar datos';
            }
            if (submitAnywayBtn) {
                submitAnywayBtn.style.display = 'none'; // IMPOSIBLE OMITIR EN NIVEL 1
            }
        } else {
            // MODO ADVERTENCIA SUAVE (NIVEL 2 - DATOS ACCESORIOS)
            if (card) card.classList.remove('is-hard-block');
            if (badge) badge.textContent = '⚠️';
            if (title) title.textContent = 'Campos pendientes de completar';
            if (subtitle) subtitle.textContent = 'Verifique antes de enviar el informe';
            if (desc) desc.textContent = 'Los siguientes datos accesorios u opcionales no han sido completados en este informe:';
            if (tipIcon) tipIcon.textContent = '💡';
            if (tipText) tipText.textContent = 'Puede regresar a completar los datos o enviar el reporte tal como está.';

            if (returnBtn) {
                returnBtn.className = 'secapp-btn-secondary';
                returnBtn.textContent = '← Regresar a llenar';
            }
            if (submitAnywayBtn) {
                submitAnywayBtn.style.display = 'inline-flex';
            }
        }

        // Renderizar lista agrupada
        listContainer.innerHTML = '';
        var grouped = {};
        missingItems.forEach(function (item) {
            var grp = item.group || (isHardBlock ? 'Campos Críticos' : 'Campos Opcionales');
            if (!grouped[grp]) grouped[grp] = [];
            grouped[grp].push(item);
        });

        Object.keys(grouped).forEach(function (grp) {
            var grpDiv = document.createElement('div');
            grpDiv.className = 'secapp-inc-group';

            var grpHeader = document.createElement('div');
            grpHeader.className = 'secapp-inc-group-title';
            grpHeader.textContent = grp;
            grpDiv.appendChild(grpHeader);

            var ul = document.createElement('div');
            ul.style.display = 'flex';
            ul.style.flexDirection = 'column';
            ul.style.gap = '0.2rem';

            grouped[grp].forEach(function (item) {
                var li = document.createElement('div');
                li.className = 'secapp-inc-item';
                var bullet = isHardBlock ? '⛔' : '•';
                li.innerHTML = '<span class="secapp-inc-bullet">' + bullet + '</span> <span>' + item.label + '</span>';
                li.addEventListener('click', function () {
                    modal.classList.add('hidden');
                    if (item.element) irA(item.element);
                });
                ul.appendChild(li);
            });

            grpDiv.appendChild(ul);
            listContainer.appendChild(grpDiv);
        });

        // Configurar controladores de eventos
        var closeBtn = modal.querySelector('#incompleteCloseBtn');
        closeBtn.onclick = function () {
            modal.classList.add('hidden');
        };

        returnBtn.onclick = function () {
            modal.classList.add('hidden');
            if (firstPendingElement) irA(firstPendingElement);
        };

        if (submitAnywayBtn) {
            submitAnywayBtn.onclick = function () {
                if (isHardBlock) {
                    // Doble protección: nunca omitir Nivel 1
                    return;
                }
                modal.classList.add('hidden');
                if (activeForm) {
                    activeForm.dataset.bypassIncomplete = 'true';
                    var btn = activeForm.querySelector('button[type="submit"]:not(#incompleteSubmitAnywayBtn), input[type="submit"], button#submitBtn, button#submit-btn');
                    if (btn) {
                        btn.click();
                    } else if (typeof activeForm.requestSubmit === 'function') {
                        activeForm.requestSubmit();
                    } else {
                        activeForm.submit();
                    }
                }
            };
        }

        modal.onclick = function (e) {
            if (e.target === modal) modal.classList.add('hidden');
        };

        modal.classList.remove('hidden');
    }

    // ── INTERCEPCIÓN UNIVERSAL DE SUBMIT ─────────────────────────────────────
    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!isOperationalForm(form)) return;

        // Neutralizar bloqueo nativo del navegador para tomar control institucional
        form.noValidate = true;

        var scanResult = scanForm(form);
        var missingLevel1 = scanResult.level1;
        var missingLevel2 = scanResult.level2;

        // CASO 1: Faltan campos de NIVEL 1 (Integridad BI) -> BLOQUEO DURO INELUDIBLE
        if (missingLevel1.length > 0) {
            event.preventDefault();
            event.stopImmediatePropagation();
            // Restablecer bypass: con errores de Nivel 1 no se puede forzar el envío
            delete form.dataset.bypassIncomplete;
            mostrarModal(form, true, missingLevel1);
            return false;
        }

        // CASO 2: Nivel 1 completo, pero faltan campos de NIVEL 2 -> ADVERTENCIA SUAVE
        if (missingLevel2.length > 0) {
            // Si el usuario ya confirmó explícitamente "Enviar así", permitir submit
            if (form.dataset.bypassIncomplete === 'true') {
                return;
            }

            event.preventDefault();
            event.stopImmediatePropagation();
            mostrarModal(form, false, missingLevel2);
            return false;
        }

        // CASO 3: Todo completo -> Permitir envío fluido
    }, true);

    // Preparar formularios al cargar la página
    function prepararFormularios() {
        document.querySelectorAll('form').forEach(function (f) {
            if (isOperationalForm(f)) {
                f.noValidate = true;
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', prepararFormularios);
    } else {
        prepararFormularios();
    }

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            var modal = document.getElementById('secappIncompleteModal');
            if (modal && !modal.classList.contains('hidden')) {
                modal.classList.add('hidden');
            }
        }
    });
})();
