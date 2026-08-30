(function () {
    'use strict';

    const PROPERTIES_URL = '/forms/api/properties';
    // v3 adds the `puestos` of each property; v2 added customer_company_id. Older
    // snapshots are still read so a device that went offline before an upgrade keeps
    // working — it just loses the field that snapshot predates (v2 properties offer
    // no puesto list, v1 ones also land in the "sin cliente asignado" group).
    const PROPERTIES_STORAGE_KEY = 'secapp:properties:v3';
    const PROPERTIES_LEGACY_STORAGE_KEYS = ['secapp:properties:v2', 'secapp:properties:v1'];
    const PROPERTIES_FETCH_TIMEOUT_MS = 4000;
    const CACHE_READ_TIMEOUT_MS = 1500;
    // Window after the panel opens in which a tap is treated as a ghost click.
    const GHOST_TAP_MS = 500;

    // Pseudo-client for properties with no customer_company_id. Never a valid id,
    // so the server ignores it and resolves the client from the property instead.
    const NO_CUSTOMER = '__sin_cliente__';
    const NO_CUSTOMER_LABEL = 'Sin cliente asignado';

    // Touch-primary device: no hover, coarse pointer. Drives the "picker, not a
    // text box" behaviour — no keyboard on tap, search lives inside the panel.
    function isTouchDevice() {
        try {
            return window.matchMedia('(hover: none) and (pointer: coarse)').matches;
        } catch {
            return 'ontouchstart' in window;
        }
    }

    function normalize(text) {
        return String(text == null ? '' : text)
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .toLowerCase()
            // Collapse runs of whitespace so "Planta  Tocumen" matches "Planta Tocumen".
            .replace(/\s+/g, ' ')
            .trim();
    }

    // ── Searchable single-select ─────────────────────────────────────────────
    // Enhances an existing <select> instead of replacing it: the select stays in
    // the DOM (hidden) as the submitted value, so FormData, the offline queue and
    // every existing `select.value = x` caller keep working untouched.
    class SearchableSelect {
        constructor(select, options = {}) {
            this.select = select;
            this.placeholder = options.placeholder || 'Seleccione...';
            this.emptyText = options.emptyText || 'Sin resultados';
            this.invalidMessage = options.invalidMessage || 'Seleccione una opción de la lista.';
            this.filter = options.filter || null;
            // allowCustom turns the control into a combobox: whatever the user types
            // can be committed as a value of its own, not just picked from the list.
            this.allowCustom = !!options.allowCustom;
            this.customLabel = options.customLabel || ((text) => '➕ Usar "' + text + '"');
            this.customHint = options.customHint || 'No está en la lista';
            this.isOpen = false;
            this.activeIndex = -1;
            this.rendered = [];

            this.required = select.hasAttribute('required');
            // Validation moves to the visible input: a required control that is
            // display:none makes the browser refuse to submit the whole form.
            select.removeAttribute('required');

            this._build();
            this.syncFromSelect();
        }

        // ── Public API ───────────────────────────────────────────────────────

        setFilter(fn) {
            this.filter = fn;
            if (this.isOpen) this._renderPanel(this.input.value);
        }

        setPlaceholder(text) {
            this.placeholder = text;
            if (!this.select.value) this.input.placeholder = text;
        }

        setEmptyText(text) {
            this.emptyText = text;
        }

        /* Hand the field over to another control (e.g. a free-text box for a plate
         * that is not in the fleet). Disabling the visible input takes it out of
         * constraint validation; the native select stays in the DOM so whatever
         * value it holds is still submitted. */
        setEnabled(on) {
            if (!on) this._close();
            this.wrap.style.display = on ? '' : 'none';
            this.input.disabled = !on;
            if (on) this._updateValidity(); else this.input.setCustomValidity('');
        }

        /* Set a value that is not one of the listed options. An exact match against
         * a real option wins, so typing a plate that does exist selects that one
         * instead of creating a duplicate entry. */
        setCustomValue(text) {
            const trimmed = String(text == null ? '' : text).trim();
            if (!trimmed) {
                // Drop the option entirely, or a discarded entry lingers in the list
                // looking like a real one.
                const stale = this.select.querySelector('option[data-custom="1"]');
                if (stale) stale.remove();
                this.select.selectedIndex = 0;
            } else {
                const needle = normalize(trimmed);
                const existing = Array.from(this.select.options).find((option) =>
                    option.value !== '' && option.dataset.custom !== '1'
                    && normalize(option.value) === needle);
                this.select.selectedIndex = existing
                    ? existing.index
                    : this._customOption(trimmed).index;
            }
            this.select.dispatchEvent(new Event('change', { bubbles: true }));
            this.syncFromSelect();
        }

        syncFromSelect() {
            const option = this._selectedOption();
            this.input.value = option ? option.dataset.label || option.textContent.trim() : '';
            this.input.placeholder = this.placeholder;
            this._updateValidity();
            if (this.isOpen) this._renderPanel('');
        }

        // ── Build ────────────────────────────────────────────────────────────

        _build() {
            injectStyles();

            const wrap = document.createElement('div');
            wrap.className = 'ss-wrap';

            const input = document.createElement('input');
            input.type = 'text';
            input.className = this.select.className + ' ss-input';
            input.autocomplete = 'off';
            input.setAttribute('autocapitalize', 'off');
            input.setAttribute('autocorrect', 'off');
            input.setAttribute('spellcheck', 'false');
            input.setAttribute('role', 'combobox');
            input.setAttribute('aria-autocomplete', 'list');
            input.setAttribute('aria-expanded', 'false');
            input.placeholder = this.placeholder;
            if (this.required) input.required = true;
            if (this.select.id) input.id = this.select.id + '_search';
            if (isTouchDevice()) {
                // This field is picked from a list, so tapping it must not raise the
                // on-screen keyboard. inputmode=none keeps the control focusable —
                // and therefore still constraint-validated, which `readonly` would
                // not be — while suppressing the keyboard. Typing moves into the
                // search box inside the panel below.
                input.setAttribute('inputmode', 'none');
                input.classList.add('ss-input-picker');
            }
            this.input = input;

            const panel = document.createElement('div');
            panel.className = 'ss-panel';
            if (input.id) {
                panel.id = input.id + '_listbox';
                input.setAttribute('aria-controls', panel.id);
            }
            this.panel = panel;

            if (isTouchDevice()) {
                const searchWrap = document.createElement('div');
                searchWrap.className = 'ss-search';
                const search = document.createElement('input');
                search.type = 'search';
                search.className = 'ss-search-input';
                search.placeholder = 'Buscar...';
                search.autocomplete = 'off';
                search.setAttribute('autocapitalize', 'off');
                search.setAttribute('autocorrect', 'off');
                search.setAttribute('spellcheck', 'false');
                search.setAttribute('aria-label', 'Buscar en la lista');
                searchWrap.appendChild(search);
                panel.appendChild(searchWrap);
                this.searchInput = search;
            }

            const list = document.createElement('div');
            list.className = 'ss-list';
            list.setAttribute('role', 'listbox');
            panel.appendChild(list);
            this.list = list;

            this.select.parentNode.insertBefore(wrap, this.select);
            wrap.appendChild(this.select);
            wrap.appendChild(input);
            wrap.appendChild(panel);
            this.select.classList.add('ss-native');
            this.wrap = wrap;

            // Point the existing <label for="..."> at the control the user can focus.
            if (this.select.id) {
                const label = document.querySelector('label[for="' + this.select.id + '"]');
                if (label) label.setAttribute('for', input.id);
            }

            this._bind();
        }

        _bind() {
            const input = this.input;

            input.addEventListener('focus', () => this._open());
            input.addEventListener('click', () => { if (!this.isOpen) this._open(); });
            input.addEventListener('input', () => {
                if (!this.isOpen) this._open(input.value);
                else this._renderPanel(input.value);
            });

            input.addEventListener('keydown', (event) => this._onKeydown(event));

            // Selection commits on RELEASE, and only when the press began on that
            // same row. Committing on pointerdown broke touch in two ways:
            //   · opening the panel scrolls the page (the keyboard slides up), so
            //     the synthetic click that follows the tap lands on whatever row
            //     has moved under the finger — auto-selecting the first option;
            //   · dragging to scroll a long list selected whatever row was touched.
            // A ghost tap has no matching press, and a drag releases somewhere else,
            // so both are rejected without relying on timing heuristics.
            this.panel.addEventListener('pointerdown', (event) => {
                const row = event.target.closest('.ss-opt');
                if (!row) return;
                // Mouse only: hold focus on the input so the panel can't blur-close
                // mid-click. Never for touch — it would stop the list scrolling.
                if (event.pointerType === 'mouse') event.preventDefault();
                this._press = {
                    pos: Number(row.dataset.pos),
                    x: event.clientX,
                    y: event.clientY,
                    pointerType: event.pointerType,
                };
            });

            this.panel.addEventListener('pointerup', (event) => {
                const press = this._press;
                this._press = null;
                if (!press) return;

                const row = event.target.closest('.ss-opt');
                if (!row) return;

                if (press.pointerType === 'mouse') {
                    // Follow the native select: release decides.
                    this._commit(Number(row.dataset.pos));
                    return;
                }
                // Touch/pen: same row, and the finger barely moved (else it's a scroll).
                const moved = Math.abs(event.clientX - press.x) + Math.abs(event.clientY - press.y);
                if (Number(row.dataset.pos) !== press.pos || moved > 12) return;
                this._commit(press.pos);
            });

            // Scrolling the list takes the gesture away from us.
            this.panel.addEventListener('pointercancel', () => { this._press = null; });

            // Last-resort path for engines with no Pointer Events at all. Ignored
            // right after opening, which is exactly when ghost clicks arrive.
            if (!('PointerEvent' in window)) {
                this.panel.addEventListener('click', (event) => {
                    if (Date.now() - (this._openedAt || 0) < GHOST_TAP_MS) return;
                    const row = event.target.closest('.ss-opt');
                    if (!row) return;
                    event.preventDefault();
                    this._commit(Number(row.dataset.pos));
                });
            }

            // In-panel search box (touch): the one place typing is expected, so
            // this is the only control that should raise the keyboard.
            if (this.searchInput) {
                this.searchInput.addEventListener('input', () => this._renderPanel(this.searchInput.value));
                this.searchInput.addEventListener('keydown', (event) => this._onKeydown(event));
                // Tapping the search box must not read as "clicked outside".
                this.searchInput.addEventListener('pointerdown', (event) => event.stopPropagation());
            }

            const closeIfFocusLeft = () => {
                setTimeout(() => {
                    if (!this.isOpen || this._press) return;
                    // Focus moving into the panel's search box is not leaving the field.
                    if (this.wrap.contains(document.activeElement)) return;
                    this._close();
                }, 180);
            };
            // Delayed so a press on the panel wins the race, and never closes
            // while a finger is still down on a row.
            input.addEventListener('blur', closeIfFocusLeft);
            if (this.searchInput) this.searchInput.addEventListener('blur', closeIfFocusLeft);

            document.addEventListener('pointerdown', (event) => {
                if (this.isOpen && !this.wrap.contains(event.target)) this._close();
            });

            // Reflect programmatic changes (draft restore, deep links, reset).
            this.select.addEventListener('change', () => this.syncFromSelect());
            const form = this.select.form;
            if (form) form.addEventListener('reset', () => setTimeout(() => this.syncFromSelect(), 0));
        }

        // ── Panel ────────────────────────────────────────────────────────────

        _onKeydown(event) {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                if (!this.isOpen) { this._open(); return; }
                this._moveActive(event.key === 'ArrowDown' ? 1 : -1);
            } else if (event.key === 'Enter') {
                if (this.isOpen) {
                    event.preventDefault();
                    if (this.rendered[this.activeIndex]) this._commit(this.activeIndex);
                    else this._close();
                }
            } else if (event.key === 'Escape') {
                if (this.isOpen) { event.preventDefault(); this._close(); }
            } else if (event.key === 'Tab') {
                this._close();
            }
        }

        // One reusable <option> holds whatever the user typed, so the value submits
        // through the select exactly like a listed one.
        _customOption(text) {
            let option = this.select.querySelector('option[data-custom="1"]');
            if (!option) {
                option = document.createElement('option');
                option.dataset.custom = '1';
                this.select.appendChild(option);
            }
            option.value = text;
            option.textContent = text;
            option.dataset.label = text;
            option.dataset.sublabel = this.customHint;
            option.dataset.search = text;
            return option;
        }

        _selectedOption() {
            // By index, never by value: client options legitimately share a value
            // when the payload carries no ids, and find-by-value picks the wrong one.
            const option = this.select.options[this.select.selectedIndex];
            return option && option.value !== '' ? option : null;
        }

        _candidates() {
            const options = Array.from(this.select.options).filter((option) => option.value !== '');
            return this.filter ? options.filter(this.filter) : options;
        }

        _open(query) {
            // Selecting an option refocuses the input; that must not reopen the panel.
            if (this._justCommitted) return;
            this._openedAt = Date.now();
            this._press = null;
            this.isOpen = true;
            this.input.setAttribute('aria-expanded', 'true');
            this.panel.classList.add('ss-open');
            if (this.searchInput) this.searchInput.value = '';
            this._renderPanel(query === undefined ? '' : query);
            // Highlighting the text is for typing in place; on a picker there is
            // nothing to type and it only summons the selection handles.
            if (query === undefined && !this.searchInput) this.input.select();
        }

        _close() {
            this.isOpen = false;
            this.activeIndex = -1;
            this.input.setAttribute('aria-expanded', 'false');
            this.panel.classList.remove('ss-open');
            if (this.searchInput) this.searchInput.value = '';
            // Drop any half-typed search text and show the actual selection.
            const option = this._selectedOption();
            this.input.value = option ? option.dataset.label || option.textContent.trim() : '';
            this._updateValidity();
        }

        _renderPanel(query) {
            const needle = normalize(query);
            const pool = this._candidates();
            const matches = needle
                ? pool.filter((option) => normalize(option.dataset.search || option.textContent).includes(needle))
                : pool;

            const typed = String(query == null ? '' : query).trim();
            // With allowCustom, what the user typed can be committed as a new entry —
            // that is how a plate outside the fleet gets in. Only offered when nothing
            // matches: while a partial search like "hilux" is still showing hits,
            // proposing to create an asset literally named "HILUX" invites junk rows.
            const offerCustom = this.allowCustom && typed.length > 0 && matches.length === 0;

            this.rendered = matches.map((option) => ({ kind: 'option', option }));
            if (offerCustom) this.rendered.push({ kind: 'custom', text: typed });
            this.list.innerHTML = '';

            if (!this.rendered.length) {
                const empty = document.createElement('div');
                empty.className = 'ss-empty';
                empty.textContent = pool.length ? 'Sin resultados para "' + query + '"' : this.emptyText;
                this.list.appendChild(empty);
                this.activeIndex = -1;
                return;
            }

            if (matches.length > 8) {
                const count = document.createElement('div');
                count.className = 'ss-count';
                count.textContent = matches.length + ' opciones — escribe para filtrar';
                this.list.appendChild(count);
            }

            const selected = this._selectedOption();
            this.activeIndex = -1;
            this.rendered.forEach((entry, index) => {
                const row = document.createElement('div');
                row.className = 'ss-opt';
                row.setAttribute('role', 'option');
                row.dataset.pos = String(index);

                if (entry.kind === 'custom') {
                    row.classList.add('ss-opt-custom');
                    row.textContent = this.customLabel(entry.text);
                    const sub = document.createElement('span');
                    sub.className = 'ss-opt-sub';
                    sub.textContent = this.customHint;
                    row.appendChild(sub);
                    this.list.appendChild(row);
                    return;
                }

                const option = entry.option;
                if (option === selected) {
                    row.classList.add('ss-selected');
                    row.setAttribute('aria-selected', 'true');
                    this.activeIndex = index;
                }
                row.textContent = option.dataset.label || option.textContent.trim();
                if (option.dataset.sublabel) {
                    const sub = document.createElement('span');
                    sub.className = 'ss-opt-sub';
                    sub.textContent = option.dataset.sublabel;
                    row.appendChild(sub);
                }
                this.list.appendChild(row);
            });

            // Scroll a long list straight to the current selection.
            const scrollToSelection = this.activeIndex >= 0;
            if (this.activeIndex < 0 && this.rendered.length === 1) this.activeIndex = 0;
            this._paintActive(scrollToSelection);
        }

        _moveActive(step) {
            if (!this.rendered.length) return;
            this.activeIndex = (this.activeIndex + step + this.rendered.length) % this.rendered.length;
            this._paintActive(true);
        }

        _paintActive(scroll) {
            const rows = this.list.querySelectorAll('.ss-opt');
            rows.forEach((row, index) => row.classList.toggle('ss-active', index === this.activeIndex));
            const active = scroll ? rows[this.activeIndex] : null;
            if (active && typeof active.scrollIntoView === 'function') {
                active.scrollIntoView({ block: 'nearest' });
            }
        }

        _commit(pos) {
            const entry = this.rendered[pos];
            if (!entry) return;

            let index;
            if (entry.kind === 'custom') {
                index = this._customOption(entry.text).index;
            } else {
                index = entry.option.index;
            }
            if (!Number.isInteger(index) || index < 0) return;
            // selectedIndex bypasses the `value` hook, so always announce the change.
            this.select.selectedIndex = index;
            this.select.dispatchEvent(new Event('change', { bubbles: true }));
            this._close();
            // focus() dispatches synchronously, so the guard closes right after.
            this._justCommitted = true;
            this.input.focus();
            this._justCommitted = false;
        }

        _updateValidity() {
            if (!this.required) return;
            this.input.setCustomValidity(this.select.value ? '' : this.invalidMessage);
        }
    }

    // Make `select.value = x` emit a change event, so the draft-restore code that
    // every form template already ships keeps the comboboxes in sync unmodified.
    function hookValueSetter(select) {
        const descriptor = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
        if (!descriptor || !descriptor.set || select.__secappValueHook) return;
        try {
            Object.defineProperty(select, 'value', {
                configurable: true,
                enumerable: true,
                get() { return descriptor.get.call(this); },
                set(value) {
                    const changed = descriptor.get.call(this) !== value;
                    descriptor.set.call(this, value);
                    if (changed) this.dispatchEvent(new Event('change', { bubbles: true }));
                },
            });
            select.__secappValueHook = true;
        } catch {
            // Leave the native setter in place; _commit dispatches manually.
        }
    }

    /* An explicit way into free text, for lists that cannot cover every case.
     * Typing something the list does not match already offers to use it, but that
     * is invisible until you happen to type — and on a phone the field is a picker
     * whose panel just says "Buscar...". This states it outright, and can be opened
     * automatically when there is nothing to pick from at all.
     *
     * The visible box is disabled while hidden: a hidden `required` input makes the
     * browser refuse to submit the form with nothing to point the user at. */
    function attachManualEntry(select, combo, options) {
        const opts = options || {};
        const host = select.closest('.ss-wrap').parentElement;

        const toManual = document.createElement('button');
        toManual.type = 'button';
        toManual.className = 'ss-switch';
        toManual.textContent = opts.openLabel || '➕ No está en la lista';

        const manual = document.createElement('div');
        manual.className = 'ss-manual';

        const box = document.createElement('input');
        box.type = 'text';
        box.className = select.className.replace('ss-native', '').trim();
        box.placeholder = opts.placeholder || 'Escriba el valor';
        box.autocomplete = 'off';
        box.disabled = true;

        const hint = document.createElement('p');
        hint.className = 'ss-manual-hint';

        const toList = document.createElement('button');
        toList.type = 'button';
        toList.className = 'ss-switch';
        toList.textContent = opts.closeLabel || '← Volver a la lista';

        manual.appendChild(box);
        manual.appendChild(hint);
        manual.appendChild(toList);
        host.appendChild(toManual);
        host.appendChild(manual);

        const required = combo.required;
        let isManual = false;

        function updateValidity() {
            if (!box.required) { box.setCustomValidity(''); return; }
            box.setCustomValidity(box.value.trim() ? '' : (opts.invalidMessage || 'Complete este campo.'));
        }

        function setManual(on, focus) {
            isManual = !!on;
            combo.setEnabled(!isManual);
            manual.classList.toggle('ss-manual-on', isManual);
            toManual.style.display = isManual ? 'none' : '';
            box.disabled = !isManual;
            box.required = isManual && required;
            if (isManual) {
                box.value = select.value;
                if (focus) box.focus();
            }
            updateValidity();
        }

        box.addEventListener('input', () => {
            combo.setCustomValue(box.value);
            updateValidity();
        });

        toManual.addEventListener('click', () => setManual(true, true));
        toList.addEventListener('click', () => {
            setManual(false);
            const option = select.options[select.selectedIndex];
            // Drop a half-typed entry rather than leaving it selected invisibly.
            if (option && option.dataset.custom === '1') combo.setCustomValue('');
        });

        setManual(false);

        return {
            setManual,
            isManual: () => isManual,
            setHint(text) { hint.textContent = text || ''; },
            // For when the value changes underneath an open box (e.g. the field is
            // rescoped) — the box is the only thing the user can see at that point.
            syncBox() { if (isManual) { box.value = select.value; updateValidity(); } },
            // Hides the way back when the list behind it is empty — offering to
            // return to a dropdown with nothing in it is a dead end.
            setLocked(locked) { toList.style.display = locked ? 'none' : ''; },
        };
    }

    function injectStyles() {
        if (document.getElementById('secapp-searchable-select-styles')) return;
        const style = document.createElement('style');
        style.id = 'secapp-searchable-select-styles';
        style.textContent = `
.ss-wrap { position: relative; width: 100%; }
.ss-native { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; margin: 0; padding: 0; border: 0; }
.ss-input { width: 100%; cursor: pointer; padding-right: 2.5rem;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke='%23a0aec0'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M19 9l-7 7-7-7'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 0.75rem center; background-size: 1.1rem; }
.ss-input::placeholder { opacity: 0.75; }
.ss-input-picker { cursor: pointer; caret-color: transparent; }
.ss-panel { position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 9999;
    display: none; background-color: #2d3748; border: 1px solid #718096; border-radius: 0.375rem;
    box-shadow: 0 12px 32px rgba(0,0,0,0.45); overflow: hidden; }
.ss-panel.ss-open { display: block; }
.ss-search { padding: 0.5rem; border-bottom: 1px solid rgba(255,255,255,0.1); }
.ss-search-input { box-sizing: border-box; width: 100%; padding: 0.55rem 0.75rem;
    border: 1px solid #718096; border-radius: 0.375rem; background-color: #4a5568; color: #e2e8f0;
    /* 16px keeps iOS from zooming the page when this gets focus. */
    font-size: 16px; line-height: 1.3; -webkit-appearance: none; appearance: none; }
.ss-search-input:focus { outline: none; border-color: #60a5fa; }
.ss-list { max-height: min(260px, 44vh); overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 0.25rem 0;
    /* Let a finger scroll the list vertically without the page scrolling with it. */
    touch-action: pan-y; overscroll-behavior: contain; }
.ss-opt { padding: 0.65rem 1rem; font-size: 0.9rem; line-height: 1.35; color: #e2e8f0; cursor: pointer;
    /* Big enough to hit reliably, and no long-press text selection on touch. */
    min-height: 44px; display: flex; flex-direction: column; justify-content: center;
    -webkit-user-select: none; user-select: none; -webkit-tap-highlight-color: transparent; }
.ss-opt:hover, .ss-opt.ss-active { background-color: #4a5568; }
.ss-opt.ss-selected { color: #93c5fd; font-weight: 600; }
.ss-opt-sub { display: block; font-size: 0.72rem; color: #a0aec0; margin-top: 0.1rem; font-weight: 400; }
.ss-empty { padding: 0.85rem 1rem; font-size: 0.85rem; color: #a0aec0; }
.ss-count { padding: 0.4rem 1rem; font-size: 0.7rem; color: #a0aec0; border-bottom: 1px solid rgba(255,255,255,0.08); }
body.light-mode .ss-panel { background-color: #ffffff; border-color: #ced4da; box-shadow: 0 12px 32px rgba(0,0,0,0.15); }
body.light-mode .ss-search { border-bottom-color: rgba(0,0,0,0.1); }
body.light-mode .ss-search-input { background-color: #f1f3f5; border-color: #ced4da; color: #495057; }
body.light-mode .ss-opt { color: #212529; }
body.light-mode .ss-opt:hover, body.light-mode .ss-opt.ss-active { background-color: #e9ecef; }
.ss-opt-custom { color: #6ee7b7; font-weight: 600; border-top: 1px solid rgba(255,255,255,0.08); }
.ss-opt-custom .ss-opt-sub { color: #9ca3af; font-weight: 400; }
body.light-mode .ss-opt.ss-selected { color: #1d4ed8; }
body.light-mode .ss-opt-custom { color: #047857; border-top-color: rgba(0,0,0,0.08); }
body.light-mode .ss-opt-sub, body.light-mode .ss-empty, body.light-mode .ss-count { color: #6c757d; }
.ss-switch { background: none; border: none; padding: .35rem 0 0; margin: 0;
    font: inherit; font-size: .8rem; color: #93c5fd; cursor: pointer; text-align: left;
    text-decoration: underline; text-underline-offset: 2px; }
.ss-switch:hover { color: #bfdbfe; }
.ss-manual { display: none; }
.ss-manual.ss-manual-on { display: block; }
.ss-manual-hint { font-size: .75rem; color: #9ca3af; margin: .3rem 0 0; }
body.light-mode .ss-switch { color: #1d4ed8; }
body.light-mode .ss-manual-hint { color: #6b7280; }
`;
        document.head.appendChild(style);
    }

    // ── Offline fallback (unchanged behaviour) ───────────────────────────────
    function showOfflineTextFallback(propertySelect, legacyInput) {
        const wrapper = propertySelect.parentElement;

        const textInput = document.createElement('input');
        textInput.type = 'text';
        textInput.name = 'cliente_instalacion';
        textInput.required = propertySelect.required;
        textInput.placeholder = 'Nombre de la propiedad / instalación (sin conexión)';
        textInput.className = propertySelect.className;
        textInput.style.borderColor = '#f6ad55';

        const offlineFlag = document.createElement('input');
        offlineFlag.type = 'hidden';
        offlineFlag.name = 'property_entered_offline';
        offlineFlag.value = '1';

        const notice = document.createElement('p');
        notice.style.cssText = 'font-size:.75rem;color:#f6ad55;margin-top:.25rem;';
        notice.textContent = '⚠️ Sin conexión — escribe el nombre de la instalación. Se verificará al sincronizar.';

        propertySelect.replaceWith(textInput);
        wrapper.appendChild(offlineFlag);
        wrapper.appendChild(notice);

        if (legacyInput) {
            textInput.addEventListener('input', () => { legacyInput.value = textInput.value; });
        }
    }

    // ── Properties payload ───────────────────────────────────────────────────
    function validPropertiesPayload(data) {
        return data && Array.isArray(data.properties) ? data : null;
    }

    function savePropertiesToLocalStorage(data) {
        const payload = validPropertiesPayload(data);
        if (!payload) return;
        try {
            localStorage.setItem(PROPERTIES_STORAGE_KEY, JSON.stringify(payload));
        } catch {
            // Storage can fail in private browsing or when the device is full.
        }
    }

    function readPropertiesFromLocalStorage() {
        for (const key of [PROPERTIES_STORAGE_KEY, ...PROPERTIES_LEGACY_STORAGE_KEYS]) {
            try {
                const data = validPropertiesPayload(JSON.parse(localStorage.getItem(key) || 'null'));
                if (data) return data;
            } catch {
                // Try the next key.
            }
        }
        return null;
    }

    async function readPropertiesFromCacheStorage() {
        if (!('caches' in window)) return null;
        try {
            const cached = await caches.match(PROPERTIES_URL, { ignoreVary: true });
            if (!cached || !cached.ok) return null;
            const data = validPropertiesPayload(await cached.json());
            if (data) savePropertiesToLocalStorage(data);
            return data;
        } catch {
            return null;
        }
    }

    async function fetchPropertiesFromNetwork() {
        const controller = 'AbortController' in window ? new AbortController() : null;
        const timer = controller
            ? setTimeout(() => controller.abort(), PROPERTIES_FETCH_TIMEOUT_MS)
            : null;

        try {
            const res = await fetch(PROPERTIES_URL, {
                credentials: 'include',
                signal: controller ? controller.signal : undefined,
            });
            if (!res.ok) throw new Error('bad response');
            const data = validPropertiesPayload(await res.json());
            if (!data) throw new Error('invalid properties payload');
            savePropertiesToLocalStorage(data);
            return data;
        } finally {
            if (timer) clearTimeout(timer);
        }
    }

    // Some engines (and locked-down/private modes) leave caches.match() pending
    // forever. Without this the field would sit on "Cargando propiedades..." for good.
    function withTimeout(promise, ms, fallback) {
        return Promise.race([
            promise,
            new Promise((resolve) => setTimeout(() => resolve(fallback), ms)),
        ]);
    }

    async function loadProperties() {
        const cached = await withTimeout(readPropertiesFromCacheStorage(), CACHE_READ_TIMEOUT_MS, null)
            || readPropertiesFromLocalStorage();
        if (navigator.onLine === false && cached) return cached;

        try {
            return await fetchPropertiesFromNetwork();
        } catch (err) {
            if (cached) return cached;
            throw err;
        }
    }

    function renderEmptyState(propertySelect, message) {
        propertySelect.innerHTML = '';
        const option = document.createElement('option');
        option.value = '';
        option.disabled = true;
        option.selected = true;
        option.textContent = message;
        propertySelect.appendChild(option);
    }

    // ── Client → properties tree ─────────────────────────────────────────────
    // Neither `customer_companies` nor `propiedades` enforces a unique name, so
    // the payload can carry the same client (or the same installation) more than
    // once. Both filters must still read as a clean list, so:
    //   · clients are merged by name — duplicate rows become one entry whose
    //     properties are pooled;
    //   · inside a client, installations with the same name collapse to the
    //     lowest id, which is the original row the history hangs off.
    // Comparison is accent- and case-insensitive: "Bodega Colón" and
    // "Bodega Colon" are the same place.
    function buildClientTree(properties, allowSesursa = false) {
        const byName = new Map();
        const orphans = { groupKey: NO_CUSTOMER, name: NO_CUSTOMER_LABEL, properties: [] };

        properties.forEach((property) => {
            const clientName = String(property.cliente || '').trim();
            const nameKey = normalize(clientName);
            if (!nameKey) { orphans.properties.push(property); return; }

            let client = byName.get(nameKey);
            if (!client) {
                // Grouping is keyed on the NAME, never on customer_company_id: an
                // offline snapshot cached before that field existed would otherwise
                // collapse every client onto one key and defeat the filter entirely.
                client = { groupKey: 'n:' + nameKey, name: clientName, properties: [] };
                byName.set(nameKey, client);
            }
            client.properties.push(property);
        });

        const clients = Array.from(byName.values());
        if (orphans.properties.length) clients.push(orphans);

        clients.forEach((client) => {
            // The id is only what gets submitted; the server re-derives it from the
            // property anyway, so a payload without it stays perfectly usable.
            const withId = client.properties.find((p) => p.customer_company_id != null);
            client.value = withId ? String(withId.customer_company_id) : (client.groupKey === 'n:sesursa' ? 'Sesursa' : NO_CUSTOMER);
        });

        clients.forEach((client) => {
            const unique = new Map();
            client.properties.forEach((property) => {
                const nameKey = normalize(property.name);
                const kept = unique.get(nameKey);
                if (!kept || Number(property.id) < Number(kept.id)) unique.set(nameKey, property);
            });
            client.properties = Array.from(unique.values())
                .sort((a, b) => String(a.name).localeCompare(String(b.name), 'es'));
        });

        if (allowSesursa) {
            const sesursaGroupKey = 'n:sesursa';
            let sesursaClient = clients.find((c) => normalize(c.name) === 'sesursa' || c.groupKey === sesursaGroupKey);
            if (!sesursaClient) {
                sesursaClient = {
                    groupKey: sesursaGroupKey,
                    name: 'SESURSA',
                    value: 'Sesursa',
                    properties: [
                        { id: 'NO APLICA', name: 'NO APLICA', cliente: 'SESURSA' }
                    ]
                };
                clients.push(sesursaClient);
            } else {
                const hasNoAplica = sesursaClient.properties.some((p) => normalize(p.name) === 'no aplica');
                if (!hasNoAplica) {
                    sesursaClient.properties.unshift({ id: 'NO APLICA', name: 'NO APLICA', cliente: sesursaClient.name });
                }
            }
        }

        return clients.sort((a, b) => {
            if (a.groupKey === NO_CUSTOMER) return 1;
            if (b.groupKey === NO_CUSTOMER) return -1;
            return a.name.localeCompare(b.name, 'es');
        });
    }

    function buildClientField(propertySelect, clients) {
        const propertyWrapper = propertySelect.parentElement;
        const propertyLabel = document.querySelector('label[for="id_propiedad"]');

        const wrapper = document.createElement('div');
        wrapper.className = propertyWrapper.className;

        const label = document.createElement('label');
        label.className = propertyLabel ? propertyLabel.className : 'form-label';
        label.setAttribute('for', 'customer_company_id');
        label.textContent = 'Cliente / Empresa ';
        // Reuse the template's own "required" marker markup when it has one.
        const marker = propertyLabel ? propertyLabel.querySelector('span') : null;
        if (marker) label.appendChild(marker.cloneNode(true));

        const select = document.createElement('select');
        select.name = 'customer_company_id';
        select.id = 'customer_company_id';
        select.className = propertySelect.className;
        if (propertySelect.hasAttribute('required')) select.setAttribute('required', 'required');

        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = '';
        select.appendChild(empty);

        clients.forEach((client) => {
            const option = document.createElement('option');
            option.value = client.value;
            option.dataset.groupKey = client.groupKey;
            option.textContent = client.name;
            option.dataset.label = client.name;
            option.dataset.sublabel = client.properties.length === 1
                ? '1 propiedad'
                : client.properties.length + ' propiedades';
            option.dataset.search = client.name;
            select.appendChild(option);
        });

        wrapper.appendChild(label);
        wrapper.appendChild(select);
        propertyWrapper.parentNode.insertBefore(wrapper, propertyWrapper);

        return select;
    }

    function fillPropertyOptions(propertySelect, clients) {
        propertySelect.innerHTML = '';

        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = '';
        propertySelect.appendChild(empty);

        clients.forEach((client) => {
            client.properties.forEach((property) => {
                const option = document.createElement('option');
                option.value = String(property.id);
                option.textContent = property.cliente
                    ? property.name + ' (' + property.cliente + ')'
                    : property.name;
                option.dataset.label = property.name;
                if (property.cliente) option.dataset.sublabel = property.cliente;
                // Searching the property list by client name works too.
                option.dataset.search = property.name + ' ' + (property.cliente || '');
                option.dataset.groupKey = client.groupKey;
                propertySelect.appendChild(option);
            });
        });
    }

    // ── Puesto o Área Específica (third level) ───────────────────────────────
    // The client and property comboboxes above narrow down to one installation;
    // this narrows down to the post inside it. The field was free text before, and
    // stays free text whenever the list cannot answer — a property with no `puestos`
    // rows, or a post that simply is not registered yet. What gets submitted is the
    // puesto NAME either way, exactly as the column has always stored it, so nothing
    // downstream (dashboards, expedientes, the offline queue) needs to know.

    // supervision_puesto keeps this value in `detalles_puestos`; every other form in
    // `puesto_area_especifica`. Both are matched bare and in the supervisions[N][...]
    // shape the multi-block builder emits.
    const PUESTO_FIELD_NAMES = ['puesto_area_especifica', 'detalles_puestos'];

    const PUESTO_FIELD_SELECTOR = PUESTO_FIELD_NAMES
        .map((name) => 'input[name="' + name + '"], input[name$="[' + name + ']"]')
        .join(', ');

    function buildPuestoIndex(properties) {
        const byProperty = new Map();
        properties.forEach((property) => {
            const unique = new Map();
            (property.puestos || []).forEach((puesto) => {
                const name = String(puesto && puesto.name != null ? puesto.name : '').trim();
                // Same guard the property list needs: `puestos` does not enforce a
                // unique name, and a duplicate row must not show up as two choices.
                if (name && !unique.has(normalize(name))) unique.set(normalize(name), name);
            });
            if (unique.size) {
                byProperty.set(String(property.id), Array.from(unique.values())
                    .sort((a, b) => a.localeCompare(b, 'es')));
            }
        });
        return byProperty;
    }

    function initPuestoField(input, byProperty, propertySelect) {
        if (input.dataset.secappPuesto) return null;
        input.dataset.secappPuesto = '1';

        // Edit mode and restored drafts write into the field before we get here.
        const preset = String(input.value || '').trim();

        const select = document.createElement('select');
        select.name = input.name;
        if (input.id) select.id = input.id;
        select.className = input.className;
        if (input.hasAttribute('required')) select.setAttribute('required', 'required');
        select.appendChild(document.createElement('option'));

        input.replaceWith(select);
        hookValueSetter(select);

        const combo = new SearchableSelect(select, {
            placeholder: 'Seleccione primero una propiedad...',
            emptyText: 'Seleccione primero una propiedad',
            invalidMessage: 'Seleccione un puesto de la lista o escriba uno.',
            allowCustom: true,
            customHint: 'No está en la lista de puestos',
        });

        const manual = attachManualEntry(select, combo, {
            openLabel: '➕ El puesto no está en la lista',
            closeLabel: '← Volver a la lista de puestos',
            placeholder: 'Escriba el puesto o área específica',
            invalidMessage: 'Escriba el puesto o área específica.',
        });

        function selectedIsCustom() {
            const option = select.options[select.selectedIndex];
            return !!(option && option.value && option.dataset.custom === '1');
        }

        // null = no property chosen yet; [] = chosen, and it has no puestos.
        function scopedNames() {
            return propertySelect.value ? (byProperty.get(propertySelect.value) || []) : null;
        }

        // The options are rebuilt per property rather than rendered once and
        // filtered: the same puesto name legitimately exists under several
        // properties ("GARITA DE ACCESO" under half the list), and matching a value
        // against one flat option set would keep landing on the wrong property's row.
        function fillOptions(names) {
            select.innerHTML = '';
            select.appendChild(document.createElement('option'));
            names.forEach((name) => {
                const option = document.createElement('option');
                option.value = name;
                option.textContent = name;
                option.dataset.label = name;
                option.dataset.search = name;
                select.appendChild(option);
            });
        }

        function scopeHas(names, value) {
            const needle = normalize(value);
            return !!needle && names.some((name) => normalize(name) === needle);
        }

        function applyScope() {
            const scoped = scopedNames();
            const hasProperty = scoped !== null;
            const names = scoped || [];
            const hasList = names.length > 0;
            // Keep whatever still means something here: text the user typed, or a
            // puesto this property registers too (half the properties have their own
            // "GARITA DE ACCESO"). Only a listed puesto the new property does not
            // have is dropped — that one belonged to the property just replaced.
            // Deciding on the value rather than on "the property changed" also makes
            // this idempotent: the client/property sync can emit a second change
            // event for the same property, and it must not clear a valid answer.
            const previous = select.value;
            const keep = selectedIsCustom() || scopeHas(names, previous) ? previous : '';

            fillOptions(names);
            // Resolves against the new scope: a name registered under this property
            // becomes a real selection, anything else is parked as free text rather
            // than dropped, so an existing record never loses its puesto.
            combo.setCustomValue(keep);

            combo.setPlaceholder(hasList
                ? 'Seleccione o busque un puesto...'
                : 'Seleccione primero una propiedad...');
            combo.setEmptyText(hasProperty
                ? 'Esta propiedad no tiene puestos registrados'
                : 'Seleccione primero una propiedad');

            if (hasProperty && !hasList) {
                // Nothing to pick from — go straight to free text rather than show a
                // dropdown that can only ever say "no hay puestos".
                manual.setHint('Esta propiedad no tiene puestos registrados.');
                manual.setLocked(true);
                manual.setManual(true);
            } else {
                manual.setHint('Se guardará tal como lo escriba.');
                manual.setLocked(false);
                // Back to the picker once a real list is available again, unless the
                // user is standing on something they typed themselves.
                if (hasList && manual.isManual() && !selectedIsCustom()) manual.setManual(false);
            }
            manual.syncBox();
        }

        // Edit mode and restored drafts wrote into the original input before we got
        // here; applyScope re-resolves that text against whatever property is set.
        if (preset) combo.setCustomValue(preset);
        applyScope();
        // A preset matching no registered puesto must stay visible and editable.
        if (selectedIsCustom()) manual.setManual(true);

        return { onPropertyChange: applyScope };
    }

    function initPuestoFields(propertySelect, properties) {
        const byProperty = buildPuestoIndex(properties);
        const fields = [];

        function enhanceAll() {
            document.querySelectorAll(PUESTO_FIELD_SELECTOR).forEach((input) => {
                const field = initPuestoField(input, byProperty, propertySelect);
                if (field) fields.push(field);
            });
        }

        enhanceAll();

        // Control de Supervisión builds its blocks in JS and adds more on demand.
        const blocks = document.getElementById('supervisionsContainer');
        if (blocks) new MutationObserver(enhanceAll).observe(blocks, { childList: true });

        propertySelect.addEventListener('change', () => {
            fields.forEach((field) => field.onPropertyChange());
        });
    }

    // ── Init ─────────────────────────────────────────────────────────────────
    async function initPropertySelector() {
        const propertySelect = document.getElementById('id_propiedad');
        if (!propertySelect) return;
        // Skip enhancement if form or select explicitly opts out (e.g. fixed Sesursa / internal forms)
        if (propertySelect.dataset.skipHierarchy === 'true' ||
            propertySelect.dataset.skipHierarchy === '1' ||
            propertySelect.dataset.static === 'true' ||
            propertySelect.dataset.secappFixed === 'true' ||
            propertySelect.closest('form[data-skip-customer-hierarchy]')) {
            return;
        }
        // Enhance once, whatever fires us (duplicate script tag, bfcache restore).
        if (propertySelect.dataset.secappEnhanced) return;
        propertySelect.dataset.secappEnhanced = '1';

        const allowSesursa = propertySelect.dataset.allowSesursa === 'true' ||
            propertySelect.dataset.allowSesursa === '1' ||
            propertySelect.dataset.includeSesursa === 'true' ||
            (propertySelect.form && propertySelect.form.id === 'capacitacionForm') ||
            (propertySelect.form && propertySelect.form.getAttribute('action') && propertySelect.form.getAttribute('action').includes('capacitacion'));

        const legacyInput = document.getElementById('cliente_instalacion')
                         || document.getElementById('cliente_visitado');

        let data;
        try {
            data = await loadProperties();
        } catch {
            showOfflineTextFallback(propertySelect, legacyInput);
            return;
        }

        if (!data.properties.length && !allowSesursa) {
            renderEmptyState(propertySelect, 'No hay propiedades disponibles');
            return;
        }

        const clients = buildClientTree(data.properties, allowSesursa);
        const clientSelect = buildClientField(propertySelect, clients);
        fillPropertyOptions(propertySelect, clients);

        hookValueSetter(clientSelect);
        hookValueSetter(propertySelect);

        const clientCombo = new SearchableSelect(clientSelect, {
            placeholder: 'Seleccione o busque un cliente...',
            emptyText: 'No hay clientes disponibles',
            invalidMessage: 'Seleccione un cliente de la lista.',
        });

        // The client is identified by its group key, not by the submitted value:
        // several clients share the "sin cliente" value when the payload has no ids.
        function selectedClientKey() {
            const option = clientSelect.options[clientSelect.selectedIndex];
            return option && option.value !== '' ? (option.dataset.groupKey || '') : '';
        }

        function selectClientByKey(groupKey) {
            const option = Array.from(clientSelect.options)
                .find((candidate) => candidate.dataset.groupKey === groupKey);
            const index = option ? option.index : 0;
            if (clientSelect.selectedIndex === index) return;
            clientSelect.selectedIndex = index;
            clientSelect.dispatchEvent(new Event('change', { bubbles: true }));
        }

        const propertyCombo = new SearchableSelect(propertySelect, {
            placeholder: 'Seleccione primero un cliente...',
            emptyText: 'Seleccione primero un cliente',
            invalidMessage: 'Seleccione una propiedad / instalación de la lista.',
            filter: (option) => option.dataset.groupKey === selectedClientKey(),
        });

        function applyClientScope() {
            const key = selectedClientKey();
            propertyCombo.setFilter((option) => option.dataset.groupKey === key);
            propertyCombo.setPlaceholder(key
                ? 'Seleccione o busque una propiedad...'
                : 'Seleccione primero un cliente...');
            propertyCombo.setEmptyText(key
                ? 'Este cliente no tiene propiedades activas'
                : 'Seleccione primero un cliente');
        }

        clientSelect.addEventListener('change', () => {
            const currentKey = selectedClientKey();
            const isSesursa = currentKey === 'n:sesursa' || currentKey === 'sesursa';
            const selected = propertySelect.options[propertySelect.selectedIndex];

            // Drop a property that no longer belongs to the chosen client.
            if (selected && selected.value && selected.dataset.groupKey !== currentKey) {
                propertySelect.selectedIndex = 0;
            }
            applyClientScope();

            // When SESURSA is selected, automatically select 'NO APLICA' by default
            if (isSesursa) {
                const noAplicaOpt = Array.from(propertySelect.options).find(
                    (opt) => opt.dataset.groupKey === currentKey && normalize(opt.dataset.label || opt.textContent) === 'no aplica'
                );
                if (noAplicaOpt) {
                    propertySelect.selectedIndex = noAplicaOpt.index;
                    if (legacyInput) legacyInput.value = noAplicaOpt.dataset.label || 'NO APLICA';
                }
            } else if (selected && selected.value && selected.dataset.groupKey === currentKey) {
                if (legacyInput) legacyInput.value = selected.dataset.label || selected.value;
            } else {
                if (legacyInput) legacyInput.value = '';
            }

            propertyCombo.syncFromSelect();
            if (isSesursa && legacyInput && !legacyInput.value) {
                legacyInput.value = 'NO APLICA';
            }
        });

        propertySelect.addEventListener('change', () => {
            const selected = propertySelect.options[propertySelect.selectedIndex];
            if (legacyInput) legacyInput.value = selected && selected.value ? selected.dataset.label : '';
            // Keep the client in step when the property is set from elsewhere
            // (draft restore, deep link) — this re-enters the handler above once.
            if (selected && selected.value && selected.dataset.groupKey !== selectedClientKey()) {
                selectClientByKey(selected.dataset.groupKey);
            }
        });

        applyClientScope();

        // Third level. Registered before the deep-link handling below so a
        // pre-selected property scopes the puesto list on the way in.
        initPuestoFields(propertySelect, data.properties);

        // A single client is not a choice — pick it so the user goes straight to
        // the property list.
        if (clients.length === 1) {
            selectClientByKey(clients[0].groupKey);
        }

        const params = new URLSearchParams(window.location.search);
        // Match against the rendered options, not the raw payload: a deep link may
        // point at a duplicate row that was collapsed away.
        const options = Array.from(propertySelect.options).filter((option) => option.value !== '');

        // Deep link by numeric id (?id_propiedad=): used by "Agendar visita" pre-fill
        const idParam = (params.get('id_propiedad') || '').trim();
        let target = idParam
            ? options.find((option) => option.value === idParam)
            : null;

        // The linked id may be the duplicate that lost — fall back to its name,
        // matched within its own client (the same name can exist under several).
        if (idParam && !target) {
            const linked = data.properties.find((p) => String(p.id) === idParam);
            if (linked) {
                target = options.find((option) =>
                    normalize(option.dataset.label) === normalize(linked.name)
                    && normalize(option.dataset.sublabel) === normalize(linked.cliente));
            }
        }

        // Deep link by name (?cliente=): e.g. Morning Briefing alerts
        const clienteParam = normalize(params.get('cliente') || '');
        if (!target && !idParam && clienteParam) {
            target = options.find((option) =>
                normalize(option.dataset.label) === clienteParam
                || normalize(option.dataset.sublabel) === clienteParam);
        }

        if (target) propertySelect.value = target.value;
    }

    // Shared with other form scripts (e.g. the fleet plate selector) so there is
    // exactly one combobox implementation in the app.
    window.SecappSearchableSelect = SearchableSelect;
    window.SecappManualEntry = attachManualEntry;
    window.SecappNormalizeText = normalize;

    // Exposed for the "Preparar modo offline" button — fetches and lets the SW cache the response
    window.secappPrefetchProperties = async function () {
        return fetchPropertiesFromNetwork();
    };

    document.addEventListener('DOMContentLoaded', initPropertySelector);
})();
