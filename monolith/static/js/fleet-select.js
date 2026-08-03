/* Fleet (flota) plate selector for the vehicle and motorcycle inspection forms.
 *
 * Turns the plate field into a searchable list of the company's fleet, while still
 * accepting a plate that is not on it — the server registers those into `flota`
 * when the form is submitted.
 *
 * Reuses the combobox from customer-hierarchy.js (window.SecappSearchableSelect)
 * so there is one implementation of the dropdown behaviour in the app.
 */
(function () {
    'use strict';

    const FLEET_URL = '/forms/api/fleet';
    const STORAGE_KEY = 'secapp:fleet:v1';
    const FETCH_TIMEOUT_MS = 4000;

    // id -> the `tipo` this form inspects.
    const FIELDS = [
        { id: 'placa_vehiculo', kind: 'vehiculo', noun: 'vehículo' },
        { id: 'placa_motocicleta', kind: 'moto', noun: 'motocicleta' },
    ];

    function plate(value) {
        return String(value == null ? '' : value).trim().toUpperCase().replace(/\s+/g, ' ');
    }

    function validPayload(data) {
        return data && Array.isArray(data.assets) ? data : null;
    }

    function cacheKey(kind) {
        return STORAGE_KEY + ':' + kind;
    }

    function saveSnapshot(kind, data) {
        try {
            localStorage.setItem(cacheKey(kind), JSON.stringify(data));
        } catch {
            // Private browsing or a full device — the list just will not be offline.
        }
    }

    function readSnapshot(kind) {
        try {
            return validPayload(JSON.parse(localStorage.getItem(cacheKey(kind)) || 'null'));
        } catch {
            return null;
        }
    }

    async function fetchFleet(kind) {
        const controller = 'AbortController' in window ? new AbortController() : null;
        const timer = controller ? setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS) : null;
        try {
            const res = await fetch(FLEET_URL + '?tipo=' + encodeURIComponent(kind), {
                credentials: 'include',
                signal: controller ? controller.signal : undefined,
            });
            if (!res.ok) throw new Error('bad response');
            const data = validPayload(await res.json());
            if (!data) throw new Error('invalid fleet payload');
            saveSnapshot(kind, data);
            return data;
        } finally {
            if (timer) clearTimeout(timer);
        }
    }

    async function loadFleet(kind) {
        try {
            return await fetchFleet(kind);
        } catch {
            // Offline or the endpoint is unreachable: fall back to the last snapshot.
            // With none, the field still accepts a hand-typed plate.
            return readSnapshot(kind) || { assets: [] };
        }
    }

    function describe(asset) {
        const parts = [asset.marca, asset.modelo].filter(Boolean).join(' ').trim();
        const bits = [];
        if (parts) bits.push(parts);
        if (asset.anio) bits.push(String(asset.anio));
        // Surface an out-of-service unit rather than hiding it — a Supervisor may be
        // filing the very inspection that changes its state.
        if (asset.estado && !/^activ/i.test(asset.estado)) bits.push(asset.estado);
        return bits.join(' · ');
    }

    function fillOptions(select, assets, keepValue) {
        select.innerHTML = '';

        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = '';
        select.appendChild(empty);

        const seen = new Set();
        assets.forEach((asset) => {
            const placa = plate(asset.placa);
            if (!placa || seen.has(placa)) return;
            seen.add(placa);

            const option = document.createElement('option');
            option.value = placa;
            option.textContent = placa;
            option.dataset.label = placa;
            const sub = describe(asset);
            if (sub) option.dataset.sublabel = sub;
            option.dataset.search = placa + ' ' + sub;
            select.appendChild(option);
        });

        // A plate already on the record (edit mode) or deep-linked must stay
        // selectable even when it is no longer in the fleet.
        const wanted = plate(keepValue);
        if (wanted && !seen.has(wanted)) {
            const option = document.createElement('option');
            option.dataset.custom = '1';
            option.value = wanted;
            option.textContent = wanted;
            option.dataset.label = wanted;
            option.dataset.sublabel = 'Fuera de la flota registrada';
            option.dataset.search = wanted;
            select.appendChild(option);
        }
        return wanted;
    }

    function injectStyles() {
        if (document.getElementById('secapp-fleet-styles')) return;
        const style = document.createElement('style');
        style.id = 'secapp-fleet-styles';
        style.textContent = `
.fleet-switch { background: none; border: none; padding: .35rem 0 0; margin: 0;
    font: inherit; font-size: .8rem; color: #93c5fd; cursor: pointer; text-align: left;
    text-decoration: underline; text-underline-offset: 2px; }
.fleet-switch:hover { color: #bfdbfe; }
.fleet-manual { display: none; }
.fleet-manual.fleet-on { display: block; }
.fleet-manual-hint { font-size: .75rem; color: #9ca3af; margin: .3rem 0 0; }
body.light-mode .fleet-switch { color: #1d4ed8; }
body.light-mode .fleet-manual-hint { color: #6b7280; }
`;
        document.head.appendChild(style);
    }

    /* An explicit way in. Typing a plate that matches nothing already offers to add
     * it, but that is invisible until you happen to type — and on a phone the field
     * is a picker whose box just says "Buscar...". This button says it outright. */
    function addManualEntry(select, combo, field) {
        injectStyles();
        const host = select.closest('.ss-wrap').parentElement;

        const toList = document.createElement('button');
        toList.type = 'button';
        toList.className = 'fleet-switch';
        toList.textContent = '← Volver a la lista de la flota';

        const manual = document.createElement('div');
        manual.className = 'fleet-manual';

        const box = document.createElement('input');
        box.type = 'text';
        box.className = select.className.replace('ss-native', '').trim();
        box.placeholder = 'Escriba la placa (ej. ABC-1234)';
        box.autocomplete = 'off';
        box.setAttribute('autocapitalize', 'characters');
        box.setAttribute('autocorrect', 'off');
        box.setAttribute('spellcheck', 'false');
        box.disabled = true;

        const hint = document.createElement('p');
        hint.className = 'fleet-manual-hint';
        hint.textContent = 'Se agregará a la flota al enviar el formulario.';

        manual.appendChild(box);
        manual.appendChild(hint);
        manual.appendChild(toList);

        const toManual = document.createElement('button');
        toManual.type = 'button';
        toManual.className = 'fleet-switch';
        toManual.textContent = '➕ La placa no está en la lista';

        host.appendChild(toManual);
        host.appendChild(manual);

        const required = combo.required;

        function setManual(on) {
            combo.setEnabled(!on);
            manual.classList.toggle('fleet-on', on);
            toManual.style.display = on ? 'none' : '';
            box.disabled = !on;
            box.required = on && required;
            if (on) {
                box.value = plate(select.value);
                box.focus();
            }
            updateBoxValidity();
        }

        function updateBoxValidity() {
            if (!box.required) { box.setCustomValidity(''); return; }
            box.setCustomValidity(plate(box.value) ? '' : 'Escriba la placa del ' + field.noun + '.');
        }

        box.addEventListener('input', () => {
            const caretAtEnd = box.selectionStart === box.value.length;
            const upper = box.value.toUpperCase();
            if (upper !== box.value) {
                box.value = upper;
                if (caretAtEnd) box.setSelectionRange(upper.length, upper.length);
            }
            combo.setCustomValue(box.value);
            updateBoxValidity();
        });

        toManual.addEventListener('click', () => setManual(true));
        toList.addEventListener('click', () => {
            setManual(false);
            const option = select.options[select.selectedIndex];
            // Drop a half-typed plate rather than leaving it selected invisibly.
            if (option && option.dataset.custom === '1') combo.setCustomValue('');
        });

        return { setManual };
    }

    async function initField(field) {
        const select = document.getElementById(field.id);
        if (!select || select.tagName !== 'SELECT') return;
        if (select.dataset.secappFleet) return;
        select.dataset.secappFleet = '1';

        const SearchableSelect = window.SecappSearchableSelect;
        if (!SearchableSelect) {
            console.warn('[fleet-select] combobox unavailable; leaving the native select in place');
            return;
        }

        // Pre-selected plate: the record being edited, or a ?placa= deep link.
        const params = new URLSearchParams(window.location.search);
        const record = window.EDIT_RECORD || {};
        const preset = plate(record[field.id] || params.get('placa') || '');

        const data = await loadFleet(field.kind);
        const kept = fillOptions(select, data.assets, preset);

        const combo = new SearchableSelect(select, {
            placeholder: 'Seleccione o busque una placa...',
            emptyText: data.assets.length
                ? 'Sin resultados'
                : 'No hay ' + field.noun + 's en la flota — escriba la placa',
            invalidMessage: 'Seleccione una placa de la lista o escriba una nueva.',
            allowCustom: true,
            customLabel: (text) => '➕ Usar "' + plate(text) + '"',
            customHint: 'No está en la flota — se agregará al enviar',
        });

        // Plates are stored upper-case; normalise whatever was typed so "abc 123"
        // and "ABC 123" cannot become two fleet rows.
        select.addEventListener('change', () => {
            const option = select.options[select.selectedIndex];
            if (!option || option.dataset.custom !== '1') return;
            const normalized = plate(option.value);
            if (normalized === option.value) return;
            option.value = normalized;
            option.textContent = normalized;
            option.dataset.label = normalized;
            combo.syncFromSelect();
        });

        const manual = addManualEntry(select, combo, field);

        if (kept || preset) {
            select.value = preset;
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }

        // Nothing to pick from (empty fleet, or offline with no cached list) —
        // go straight to manual entry instead of showing a dead dropdown.
        if (!data.assets.length && !preset) manual.setManual(true);
    }

    function init() {
        FIELDS.forEach((field) => { initField(field); });
    }

    document.addEventListener('DOMContentLoaded', init);
})();
