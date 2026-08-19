/**
 * DashboardFilters
 * Reusable filter bar for all dashboard sub-pages.
 *
 * Standard filters supported:
 *   - Cliente / Empresa (Customer Company)
 *   - Propiedad / Instalación (Property / Site) with cascading support
 *   - Puesto (Position / Post) with cascading support
 *   - Año (Year - multi-select)
 *   - Mes (Month - multi-select)
 *   - Día (Day - select)
 *   - Responsable / Rol (optional)
 *
 * Filter state shape:
 *   cliente     : string | null   — null = all clients
 *   propertyId  : string | null   — null = all properties
 *   propiedad   : string | null   — alias of propertyId
 *   puesto      : string | null   — null = all puestos
 *   years       : number[]        — empty = all years (multi-select)
 *   months      : number[]        — 1-12, empty = all months (multi-select)
 *   day         : number | null   — 1-31, null = all days
 *   responsable : string | null   — null = all; activated via activateResponsable()
 */
class DashboardFilters {
    constructor() {
        this.state = {
            cliente:     null,
            propertyId:  null,
            puesto:      null,
            years:       [],   // multi-select
            months:      [],   // multi-select
            day:         null,
            responsable: null,
        };

        // Cache for hierarchy
        this._allProperties = [];
        this._allClients    = [];

        // DOM refs
        this._clienteSelect     = null;
        this._propertySelect    = null;
        this._puestoSelect      = null;
        this._puestoWrap        = null;
        this._yearMS            = null;   // MultiSelect instance
        this._monthBtns         = null;
        this._dayRow            = null;
        this._daySelect         = null;
        this._resetBtn          = null;
        this._chipsRow          = null;
        this._responsableSelect = null;

        this._MONTH_NAMES  = ['Enero','Febrero','Marzo','Abril','Mayo','Junio',
                              'Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
        this._MONTH_SHORT  = ['Ene','Feb','Mar','Abr','May','Jun',
                              'Jul','Ago','Sep','Oct','Nov','Dic'];
    }

    /** Call once after the DOM is ready. */
    async init() {
        this._clienteSelect  = document.getElementById('df-cliente');
        this._propertySelect = document.getElementById('df-property');
        this._puestoSelect   = document.getElementById('df-puesto');
        this._puestoWrap     = document.getElementById('df-puesto-wrap');
        this._monthBtns      = document.querySelectorAll('.df-month-btn');
        this._dayRow         = document.getElementById('df-day-row');
        this._daySelect      = document.getElementById('df-day');
        this._resetBtn       = document.getElementById('df-reset');
        this._chipsRow       = document.getElementById('df-chips');

        if (!this._propertySelect) {
            console.warn('DashboardFilters: required elements not found.');
            return;
        }

        this._populateYears();
        await this._loadHierarchy();
        this._bindEvents();

        // Apply any state already in the URL query string
        this._readFromURL();
    }

    // ─── Public API ──────────────────────────────────────────────────────────

    /** Returns a copy of the current filter state. */
    getState() {
        const { years, months } = this.state;
        return {
            ...this.state,
            propiedad: this.state.propertyId, // alias for consistency
            // Backwards-compat single-value aliases
            year:  years.length  ? years[0]  : null,
            month: months.length ? months[0] : null,
        };
    }

    /**
     * Returns query-string params suitable for appending to a fetch URL.
     * Only includes non-empty values.
     */
    toQueryString() {
        const params = new URLSearchParams();
        if (this.state.cliente) {
            params.set('cliente', this.state.cliente);
        }
        if (this.state.propertyId) {
            params.set('propiedad',   this.state.propertyId);
            params.set('property_id', this.state.propertyId);
        }
        if (this.state.puesto) {
            params.set('puesto', this.state.puesto);
        }
        if (this.state.years.length) {
            params.set('year', this.state.years.join(','));
        }
        if (this.state.months.length) {
            params.set('month', this.state.months.join(','));
        }
        if (this.state.day) {
            params.set('day', this.state.day);
        }
        if (this.state.responsable) {
            params.set('responsable', this.state.responsable);
        }
        return params.toString();
    }

    /**
     * Show the Puesto filter section and sync options.
     * @param {object} opts
     *   label — optional label text (default 'Puesto')
     */
    activatePuesto({ label = 'Puesto' } = {}) {
        const wrap = document.getElementById('df-puesto-wrap');
        const sel  = document.getElementById('df-puesto');
        if (!wrap || !sel) return;
        wrap.style.display = 'contents';
        this._puestoWrap   = wrap;
        this._puestoSelect = sel;

        const labelEl = wrap.querySelector('.df-label');
        if (labelEl) labelEl.textContent = label;

        this._refreshPuestoOptions();

        if (!sel._hasSecappListener) {
            sel.addEventListener('change', () => {
                this.state.puesto = sel.value || null;
                this._emit();
            });
            sel._hasSecappListener = true;
        }
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
        const options     = [];
        for (let y = currentYear; y >= startYear; y--) {
            options.push({ value: String(y), label: String(y) });
        }

        const wrap = document.getElementById('df-year-wrap');
        if (!wrap) return;

        this._yearMS = new MultiSelect({
            anchor:      wrap,
            options:     options,
            placeholder: 'Todos',
            onChange:    (values) => {
                this.state.years  = values.map(v => parseInt(v, 10));
                this.state.months = [];
                this.state.day    = null;
                this._syncMonthButtons();
                this._syncDayRow();
                this._emit();
            },
        });
    }

    async _loadHierarchy() {
        try {
            const res  = await fetch('/dashboard/api/properties');
            if (!res.ok) return;
            const data = await res.json();

            this._allProperties = data.properties || (Array.isArray(data) ? data : []);
            this._allClients    = data.clientes || [];

            // If no explicit clients returned, extract unique clients from properties
            if (!this._allClients.length && this._allProperties.length) {
                const uniqueClientNames = [...new Set(this._allProperties.map(p => p.cliente).filter(Boolean))].sort();
                this._allClients = uniqueClientNames.map(name => ({ id: name, name: name }));
            }

            this._populateClients();
            this._refreshPropertyOptions();
            this._refreshPuestoOptions();
        } catch (err) {
            console.warn('DashboardFilters: could not load property hierarchy.', err);
        }
    }

    _populateClients() {
        if (!this._clienteSelect) return;
        while (this._clienteSelect.options.length > 1) {
            this._clienteSelect.remove(1);
        }

        const frag = document.createDocumentFragment();
        this._allClients.forEach(c => {
            const opt = document.createElement('option');
            opt.value       = String(c.id != null ? c.id : c.name);
            opt.textContent = c.name || c.nombre || String(c.id);
            frag.appendChild(opt);
        });
        this._clienteSelect.appendChild(frag);
    }

    _refreshPropertyOptions() {
        if (!this._propertySelect) return;
        while (this._propertySelect.options.length > 1) {
            this._propertySelect.remove(1);
        }

        const selectedClient = this.state.cliente ? String(this.state.cliente).trim().toLowerCase() : null;

        // Filter properties by selected client if any
        let filteredProps = this._allProperties;
        if (selectedClient) {
            filteredProps = this._allProperties.filter(p => {
                const pCustId = p.customer_company_id != null ? String(p.customer_company_id).trim().toLowerCase() : null;
                const pClient = p.cliente ? String(p.cliente).trim().toLowerCase() : null;
                return pCustId === selectedClient || pClient === selectedClient;
            });
        }

        const frag = document.createDocumentFragment();
        filteredProps.forEach(p => {
            const opt = document.createElement('option');
            const propId = p.id != null ? p.id : p.id_propiedad;
            opt.value = String(propId != null ? propId : (p.name || p.nombre));
            
            let label = p.name || p.nombre || `Instalación ${propId}`;
            // If viewing all clients and property has client info, append client name for clarity
            if (!selectedClient && p.cliente) {
                label = `${label} (${p.cliente})`;
            }
            opt.textContent = label;
            frag.appendChild(opt);
        });
        this._propertySelect.appendChild(frag);

        // Check if selected property is still in filtered options
        if (this.state.propertyId) {
            const optionExists = Array.from(this._propertySelect.options).some(o => o.value === String(this.state.propertyId));
            if (optionExists) {
                this._propertySelect.value = String(this.state.propertyId);
            } else {
                this.state.propertyId = null;
                this._propertySelect.value = '';
            }
        } else {
            this._propertySelect.value = '';
        }

        this._refreshPuestoOptions();
    }

    _refreshPuestoOptions() {
        if (!this._puestoSelect) return;
        while (this._puestoSelect.options.length > 1) {
            this._puestoSelect.remove(1);
        }

        let availablePuestos = [];

        if (this.state.propertyId) {
            const prop = this._allProperties.find(p => String(p.id != null ? p.id : p.id_propiedad) === String(this.state.propertyId));
            if (prop && prop.puestos && Array.isArray(prop.puestos)) {
                availablePuestos = prop.puestos;
            }
        } else if (this.state.cliente) {
            const selectedClient = String(this.state.cliente).trim().toLowerCase();
            const filteredProps = this._allProperties.filter(p => {
                const pCustId = p.customer_company_id != null ? String(p.customer_company_id).trim().toLowerCase() : null;
                const pClient = p.cliente ? String(p.cliente).trim().toLowerCase() : null;
                return pCustId === selectedClient || pClient === selectedClient;
            });
            filteredProps.forEach(p => {
                if (p.puestos && Array.isArray(p.puestos)) {
                    availablePuestos.push(...p.puestos);
                }
            });
        } else {
            this._allProperties.forEach(p => {
                if (p.puestos && Array.isArray(p.puestos)) {
                    availablePuestos.push(...p.puestos);
                }
            });
        }

        // Deduplicate by name/id
        const seen = new Set();
        const uniquePuestos = [];
        availablePuestos.forEach(pu => {
            const val = String(pu.name || pu.nombre || pu.id);
            if (val && !seen.has(val.toLowerCase())) {
                seen.add(val.toLowerCase());
                uniquePuestos.push(pu);
            }
        });

        const frag = document.createDocumentFragment();
        uniquePuestos.forEach(pu => {
            const opt = document.createElement('option');
            const val = pu.name || pu.nombre || pu.id;
            opt.value = String(val);
            opt.textContent = pu.name || pu.nombre || `Puesto ${pu.id}`;
            frag.appendChild(opt);
        });
        this._puestoSelect.appendChild(frag);

        // Check if currently selected puesto is still in options
        if (this.state.puesto) {
            const exists = Array.from(this._puestoSelect.options).some(o => o.value.toLowerCase() === String(this.state.puesto).toLowerCase());
            if (exists) {
                this._puestoSelect.value = String(this.state.puesto);
            } else {
                this.state.puesto = null;
                this._puestoSelect.value = '';
            }
        } else {
            this._puestoSelect.value = '';
        }
    }

    _bindEvents() {
        // Cliente
        if (this._clienteSelect) {
            this._clienteSelect.addEventListener('change', () => {
                this.state.cliente = this._clienteSelect.value || null;
                this._refreshPropertyOptions();
                this._emit();
            });
        }

        // Propiedad / Instalación
        this._propertySelect.addEventListener('change', () => {
            this.state.propertyId = this._propertySelect.value || null;
            
            // If property selected and no client selected, optionally auto-select the client
            if (this.state.propertyId && !this.state.cliente && this._clienteSelect) {
                const matchedProp = this._allProperties.find(p => String(p.id || p.id_propiedad) === String(this.state.propertyId));
                if (matchedProp && (matchedProp.customer_company_id || matchedProp.cliente)) {
                    const clientVal = String(matchedProp.customer_company_id || matchedProp.cliente);
                    const clientOptExists = Array.from(this._clienteSelect.options).some(o => o.value === clientVal);
                    if (clientOptExists) {
                        this.state.cliente = clientVal;
                        this._clienteSelect.value = clientVal;
                        this._refreshPropertyOptions();
                    }
                }
            }
            
            this._refreshPuestoOptions();
            this._emit();
        });

        // Puesto
        if (this._puestoSelect) {
            this._puestoSelect.addEventListener('change', () => {
                this.state.puesto = this._puestoSelect.value || null;
                this._emit();
            });
        }

        // Month buttons — multi-select: each click toggles that month
        this._monthBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                const val = parseInt(btn.dataset.month, 10);
                const idx = this.state.months.indexOf(val);
                if (idx >= 0) {
                    // Deselect
                    this.state.months.splice(idx, 1);
                    if (this.state.months.length === 0) this.state.day = null;
                } else {
                    this.state.months.push(val);
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
        const hasYear = this.state.years.length > 0;
        this._monthBtns.forEach(btn => {
            const val = parseInt(btn.dataset.month, 10);
            btn.classList.toggle('df-month-active', this.state.months.includes(val));
            btn.disabled = !hasYear;
            btn.classList.toggle('df-month-disabled', !hasYear);
        });
    }

    _syncDayRow() {
        const show = this.state.months.length > 0;
        this._dayRow.classList.toggle('df-hidden', !show);

        if (show) {
            this._populateDays();
        } else {
            this._daySelect.value = '';
        }
    }

    _populateDays() {
        // Keep only the placeholder option, then rebuild
        while (this._daySelect.options.length > 1) {
            this._daySelect.remove(1);
        }

        const year  = this.state.years[0]  || new Date().getFullYear();
        const month = this.state.months[0] || 1;
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
        this.state = {
            cliente:     null,
            propertyId:  null,
            puesto:      null,
            years:       [],
            months:      [],
            day:         null,
            responsable: null
        };

        if (this._clienteSelect) this._clienteSelect.value = '';
        this._refreshPropertyOptions();
        this._propertySelect.value = '';
        if (this._puestoSelect) this._puestoSelect.value = '';
        this._refreshPuestoOptions();
        if (this._yearMS) this._yearMS.reset();
        if (this._daySelect) this._daySelect.value = '';
        if (this._responsableSelect) this._responsableSelect.value = '';

        this._syncMonthButtons();
        this._syncDayRow();
        this._emit();
    }

    _readFromURL() {
        const params = new URLSearchParams(window.location.search);

        const clienteParam = params.get('cliente');
        if (clienteParam) {
            this.state.cliente = clienteParam;
            if (this._clienteSelect) {
                // If option exists, select it; if not, add it as fallback
                const optExists = Array.from(this._clienteSelect.options).some(o => o.value === clienteParam);
                if (!optExists && clienteParam) {
                    const opt = document.createElement('option');
                    opt.value = clienteParam;
                    opt.textContent = clienteParam;
                    this._clienteSelect.appendChild(opt);
                }
                this._clienteSelect.value = clienteParam;
            }
            this._refreshPropertyOptions();
        }

        const propId = params.get('propiedad') || params.get('property_id') || params.get('id_propiedad');
        if (propId) {
            this.state.propertyId = propId;
            const optExists = Array.from(this._propertySelect.options).some(o => o.value === String(propId));
            if (!optExists && propId) {
                const opt = document.createElement('option');
                opt.value = propId;
                opt.textContent = `Instalación ${propId}`;
                this._propertySelect.appendChild(opt);
            }
            this._propertySelect.value = String(propId);
            this._refreshPuestoOptions();
        }

        const puestoParam = params.get('puesto');
        if (puestoParam) {
            this.state.puesto = puestoParam;
            if (this._puestoSelect) {
                const optExists = Array.from(this._puestoSelect.options).some(o => o.value === puestoParam);
                if (!optExists && puestoParam) {
                    const opt = document.createElement('option');
                    opt.value = puestoParam;
                    opt.textContent = puestoParam;
                    this._puestoSelect.appendChild(opt);
                }
                this._puestoSelect.value = puestoParam;
            }
        }

        if (params.get('year')) {
            this.state.years = params.get('year').split(',')
                .map(v => parseInt(v.trim(), 10))
                .filter(v => !isNaN(v));
            if (this._yearMS) this._yearMS.setValues(this.state.years.map(String));
        }
        if (params.get('month')) {
            this.state.months = params.get('month').split(',')
                .map(v => parseInt(v.trim(), 10))
                .filter(v => !isNaN(v));
        }
        if (params.get('day')) {
            this.state.day = parseInt(params.get('day'), 10);
        }
        if (params.get('responsable')) {
            this.state.responsable = params.get('responsable');
            if (this._responsableSelect) {
                this._responsableSelect.value = this.state.responsable;
            }
        }

        this._syncMonthButtons();
        this._syncDayRow();
        this._syncChips();
    }

    _syncChips() {
        if (!this._chipsRow) return;
        this._chipsRow.innerHTML = '';

        const chips = [];

        if (this.state.cliente) {
            const clientLabel = (this._clienteSelect && this._clienteSelect.options[this._clienteSelect.selectedIndex]?.text) || this.state.cliente;
            chips.push({ key: 'cliente', label: `Cliente / Empresa: ${clientLabel}` });
        }
        if (this.state.propertyId) {
            const propLabel = (this._propertySelect && this._propertySelect.options[this._propertySelect.selectedIndex]?.text) || this.state.propertyId;
            chips.push({ key: 'propertyId', label: `Propiedad / Instalación: ${propLabel}` });
        }
        if (this.state.puesto) {
            const puestoLabel = (this._puestoSelect && this._puestoSelect.options[this._puestoSelect.selectedIndex]?.text) || this.state.puesto;
            chips.push({ key: 'puesto', label: `Puesto: ${puestoLabel}` });
        }
        if (this.state.years.length) {
            chips.push({ key: 'year', label: `Año: ${this.state.years.join(', ')}` });
        }
        if (this.state.months.length) {
            const mLabels = this.state.months
                .slice().sort((a,b) => a-b)
                .map(m => this._MONTH_SHORT[m - 1]);
            chips.push({ key: 'month', label: `Mes: ${mLabels.join(', ')}` });
        }
        if (this.state.day) {
            chips.push({ key: 'day', label: `Día: ${this.state.day}` });
        }
        if (this.state.responsable) {
            chips.push({ key: 'responsable', label: `Resp: ${this.state.responsable}` });
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
        if (key === 'cliente') {
            this.state.cliente = null;
            if (this._clienteSelect) this._clienteSelect.value = '';
            this._refreshPropertyOptions();
        } else if (key === 'propertyId') {
            this.state.propertyId = null;
            this._propertySelect.value = '';
            this._refreshPuestoOptions();
        } else if (key === 'puesto') {
            this.state.puesto = null;
            if (this._puestoSelect) this._puestoSelect.value = '';
        } else if (key === 'year') {
            this.state.years  = [];
            this.state.months = [];
            this.state.day    = null;
            if (this._yearMS) this._yearMS.reset();
            this._syncMonthButtons();
            this._syncDayRow();
        } else if (key === 'month') {
            this.state.months = [];
            this.state.day    = null;
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
