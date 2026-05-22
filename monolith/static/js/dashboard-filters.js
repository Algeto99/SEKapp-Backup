/**
 * DashboardFilters
 * Reusable filter bar for all dashboard sub-pages.
 *
 * Usage (after including _dashboard_filters.html in the template):
 *
 *   const filters = new DashboardFilters();
 *   filters.init();
 *
 *   document.addEventListener('filtersChanged', (e) => {
 *     const { propertyId, year, month, day } = e.detail;
 *     // re-fetch your dashboard data here
 *   });
 *
 * Filter state shape:
 *   propertyId  : string | null   — null = all properties
 *   year        : number | null   — null = all years
 *   month       : number | null   — 1-12, null = all months
 *   day         : number | null   — 1-31, null = all days
 *   responsable : string | null   — null = all; activated via activateResponsable()
 */
class DashboardFilters {
    constructor() {
        this.state = {
            propertyId: null,
            year: null,
            month: null,
            day: null,
            responsable: null,
        };

        // DOM refs — populated in init()
        this._propertySelect    = null;
        this._yearSelect        = null;
        this._monthBtns         = null;
        this._dayRow            = null;
        this._daySelect         = null;
        this._resetBtn          = null;
        this._chipsRow          = null;
        this._responsableSelect = null;

        this._MONTH_NAMES = ['Enero','Febrero','Marzo','Abril','Mayo','Junio',
                             'Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
    }

    /** Call once after the DOM is ready. */
    init() {
        this._propertySelect = document.getElementById('df-property');
        this._yearSelect     = document.getElementById('df-year');
        this._monthBtns      = document.querySelectorAll('.df-month-btn');
        this._dayRow         = document.getElementById('df-day-row');
        this._daySelect      = document.getElementById('df-day');
        this._resetBtn       = document.getElementById('df-reset');
        this._chipsRow       = document.getElementById('df-chips');

        if (!this._propertySelect || !this._yearSelect) {
            console.warn('DashboardFilters: required elements not found.');
            return;
        }

        this._populateYears();
        this._loadProperties();
        this._bindEvents();

        // Apply any state already in the URL query string
        this._readFromURL();
    }

    // ─── Public API ──────────────────────────────────────────────────────────

    /** Returns a copy of the current filter state. */
    getState() {
        return { ...this.state };
    }

    /**
     * Returns query-string params suitable for appending to a fetch URL.
     * Only includes non-null values.
     */
    toQueryString() {
        const params = new URLSearchParams();
        if (this.state.propertyId)  params.set('property_id',  this.state.propertyId);
        if (this.state.year)        params.set('year',          this.state.year);
        if (this.state.month)       params.set('month',         this.state.month);
        if (this.state.day)         params.set('day',           this.state.day);
        if (this.state.responsable) params.set('responsable',   this.state.responsable);
        return params.toString();
    }

    /**
     * Show the Responsable / Rol filter section and load options.
     * @param {object} opts
     *   url      — endpoint that returns { responsables: string[] }
     *   label    — optional label text (default 'Responsable / Rol')
     */
    async activateResponsable({ url, label = 'RESPONSABLE / ROL' } = {}) {
        const wrap = document.getElementById('df-responsable-wrap');
        const sel  = document.getElementById('df-responsable');
        if (!wrap || !sel) return;
        wrap.style.display = 'contents'; // transparent to flex layout
        this._responsableSelect = sel;

        try {
            const res  = await fetch(url);
            const data = await res.json();
            const list = data.responsables || [];
            while (sel.options.length > 1) sel.remove(1);
            list.forEach(r => {
                const o = document.createElement('option');
                o.value = r; o.textContent = r;
                sel.appendChild(o);
            });
        } catch (e) { console.warn('DashboardFilters: could not load responsables', e); }

        const labelEl = wrap.querySelector('.df-label');
        if (labelEl) labelEl.textContent = label;

        sel.addEventListener('change', () => {
            this.state.responsable = sel.value || null;
            this._emit();
        });
    }

    // ─── Private ─────────────────────────────────────────────────────────────

    _populateYears() {
        const currentYear = new Date().getFullYear();
        const startYear   = 2022;
        const frag        = document.createDocumentFragment();

        for (let y = currentYear; y >= startYear; y--) {
            const opt = document.createElement('option');
            opt.value       = y;
            opt.textContent = y;
            frag.appendChild(opt);
        }
        this._yearSelect.appendChild(frag);
    }

    async _loadProperties() {
        try {
            const res  = await fetch('/dashboard/api/properties');
            if (!res.ok) return;
            const data = await res.json();
            const props = data.properties || data; // handle both shapes

            const frag = document.createDocumentFragment();
            props.forEach(p => {
                const opt = document.createElement('option');
                opt.value       = p.id || p.id_propiedad;
                opt.textContent = p.name || p.nombre;
                frag.appendChild(opt);
            });
            this._propertySelect.appendChild(frag);
        } catch (err) {
            console.warn('DashboardFilters: could not load properties.', err);
        }
    }

    _bindEvents() {
        // Property
        this._propertySelect.addEventListener('change', () => {
            this.state.propertyId = this._propertySelect.value || null;
            this._emit();
        });

        // Year
        this._yearSelect.addEventListener('change', () => {
            const val = this._yearSelect.value;
            this.state.year  = val ? parseInt(val, 10) : null;
            this.state.month = null;
            this.state.day   = null;
            this._syncMonthButtons();
            this._syncDayRow();
            this._emit();
        });

        // Month buttons
        this._monthBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                const val = parseInt(btn.dataset.month, 10);
                // Toggle: clicking the active month deselects it
                if (this.state.month === val) {
                    this.state.month = null;
                    this.state.day   = null;
                } else {
                    this.state.month = val;
                    this.state.day   = null;
                }
                this._syncMonthButtons();
                this._syncDayRow();
                this._emit();
            });
        });

        // Day
        this._daySelect.addEventListener('change', () => {
            const val = this._daySelect.value;
            this.state.day = val ? parseInt(val, 10) : null;
            this._emit();
        });

        // Reset
        if (this._resetBtn) {
            this._resetBtn.addEventListener('click', () => this._reset());
        }
    }

    _syncMonthButtons() {
        this._monthBtns.forEach(btn => {
            const val = parseInt(btn.dataset.month, 10);
            btn.classList.toggle('df-month-active', this.state.month === val);
            // Disable month buttons when no year is selected
            btn.disabled = this.state.year === null;
            btn.classList.toggle('df-month-disabled', this.state.year === null);
        });
    }

    _syncDayRow() {
        const show = this.state.month !== null;
        this._dayRow.classList.toggle('df-hidden', !show);

        if (show) {
            this._populateDays();
        } else {
            // Clear selection
            this._daySelect.value = '';
        }
    }

    _populateDays() {
        // Keep only the placeholder option, then rebuild
        while (this._daySelect.options.length > 1) {
            this._daySelect.remove(1);
        }

        const year  = this.state.year  || new Date().getFullYear();
        const month = this.state.month || 1;
        const days  = new Date(year, month, 0).getDate(); // last day of month

        const frag = document.createDocumentFragment();
        for (let d = 1; d <= days; d++) {
            const opt = document.createElement('option');
            opt.value       = d;
            opt.textContent = d;
            if (this.state.day === d) opt.selected = true;
            frag.appendChild(opt);
        }
        this._daySelect.appendChild(frag);
    }

    _reset() {
        this.state = { propertyId: null, year: null, month: null, day: null, responsable: null };

        this._propertySelect.value = '';
        this._yearSelect.value     = '';
        this._daySelect.value      = '';
        if (this._responsableSelect) this._responsableSelect.value = '';

        this._syncMonthButtons();
        this._syncDayRow();
        this._emit();
    }

    _readFromURL() {
        const params = new URLSearchParams(window.location.search);

        if (params.get('property_id')) {
            this.state.propertyId = params.get('property_id');
            this._propertySelect.value = this.state.propertyId;
        }
        if (params.get('year')) {
            this.state.year = parseInt(params.get('year'), 10);
            this._yearSelect.value = this.state.year;
        }
        if (params.get('month')) {
            this.state.month = parseInt(params.get('month'), 10);
        }
        if (params.get('day')) {
            this.state.day = parseInt(params.get('day'), 10);
        }

        this._syncMonthButtons();
        this._syncDayRow();
    }

    _syncChips() {
        if (!this._chipsRow) return;
        this._chipsRow.innerHTML = '';

        const chips = [];

        if (this.state.propertyId) {
            const label = this._propertySelect.options[this._propertySelect.selectedIndex]?.text || this.state.propertyId;
            chips.push({ key: 'propertyId', label: `📍 ${label}` });
        }
        if (this.state.year) {
            chips.push({ key: 'year', label: `📅 ${this.state.year}` });
        }
        if (this.state.month) {
            chips.push({ key: 'month', label: this._MONTH_NAMES[this.state.month - 1] });
        }
        if (this.state.day) {
            chips.push({ key: 'day', label: `Día ${this.state.day}` });
        }
        if (this.state.responsable) {
            chips.push({ key: 'responsable', label: `👤 ${this.state.responsable}` });
        }

        if (chips.length === 0) {
            this._chipsRow.classList.add('df-hidden');
            return;
        }

        this._chipsRow.classList.remove('df-hidden');
        chips.forEach(({ key, label }) => {
            const chip = document.createElement('span');
            chip.className = 'df-chip';
            chip.innerHTML = `${label}<button class="df-chip-remove" data-key="${key}" title="Quitar filtro">×</button>`;
            chip.querySelector('.df-chip-remove').addEventListener('click', () => {
                this._removeFilter(key);
            });
            this._chipsRow.appendChild(chip);
        });
    }

    _removeFilter(key) {
        if (key === 'propertyId') {
            this.state.propertyId = null;
            this._propertySelect.value = '';
        } else if (key === 'year') {
            this.state.year  = null;
            this.state.month = null;
            this.state.day   = null;
            this._yearSelect.value = '';
            this._syncMonthButtons();
            this._syncDayRow();
        } else if (key === 'month') {
            this.state.month = null;
            this.state.day   = null;
            this._syncMonthButtons();
            this._syncDayRow();
        } else if (key === 'day') {
            this.state.day = null;
            this._daySelect.value = '';
        } else if (key === 'responsable') {
            this.state.responsable = null;
            if (this._responsableSelect) this._responsableSelect.value = '';
        }
        this._emit();
    }

    _emit() {
        this._syncChips();
        document.dispatchEvent(new CustomEvent('filtersChanged', {
            detail: this.getState(),
            bubbles: true,
        }));
    }
}
