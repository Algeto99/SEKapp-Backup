/**
 * MultiSelect — a reusable multi-select dropdown widget.
 *
 * Usage:
 *   const ms = new MultiSelect({
 *     anchor: document.getElementById('myWrap'),  // replaces this element
 *     options: [{value:'1', label:'Enero'}, ...],
 *     placeholder: 'Todos',
 *     onChange: (values) => { ... }
 *   });
 *   ms.setValues(['1','3']);
 *   ms.getValues();       // returns string[]
 *   ms.reset();
 *   ms.setOptions(newOptions);
 */
class MultiSelect {
    constructor({ anchor, options = [], placeholder = 'Todos', onChange = null } = {}) {
        this._anchor      = anchor;
        this._options     = options; // [{value, label}]
        this._placeholder = placeholder;
        this._onChange    = onChange;
        this._selected    = new Set();
        this._open        = false;

        this._container   = null;
        this._btn         = null;
        this._panel       = null;

        this._render();
        this._bindOutside();
    }

    // ─── Public API ──────────────────────────────────────────────────────────

    getValues() {
        return [...this._selected];
    }

    setValues(values = []) {
        this._selected = new Set(values.map(String));
        this._updateButton();
        this._updateCheckboxes();
    }

    reset() {
        this._selected.clear();
        this._updateButton();
        this._updateCheckboxes();
    }

    setOptions(newOptions = []) {
        this._options = newOptions;
        // Preserve only selected values that still exist in new options
        const valid = new Set(newOptions.map(o => String(o.value)));
        for (const v of this._selected) {
            if (!valid.has(v)) this._selected.delete(v);
        }
        this._rebuildPanel();
        this._updateButton();
    }

    // ─── Rendering ───────────────────────────────────────────────────────────

    _render() {
        // Create wrapper
        const wrap = document.createElement('div');
        wrap.className = 'ms-wrap';
        wrap.style.position = 'relative';
        wrap.style.display  = 'inline-block';

        // Button
        const btn = document.createElement('button');
        btn.type      = 'button';
        btn.className = 'ms-btn';
        btn.setAttribute('aria-haspopup', 'listbox');
        btn.setAttribute('aria-expanded', 'false');
        this._btn = btn;

        // Panel
        const panel = document.createElement('div');
        panel.className = 'ms-panel';
        panel.setAttribute('role', 'listbox');
        panel.setAttribute('aria-multiselectable', 'true');
        panel.style.display = 'none';
        this._panel = panel;

        wrap.appendChild(btn);
        wrap.appendChild(panel);
        this._container = wrap;

        // Insert into DOM, replacing anchor
        this._anchor.replaceWith(wrap);

        this._buildPanel();
        this._updateButton();

        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            this._open ? this._closePanel() : this._openPanel();
        });

        btn.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') this._closePanel();
        });

        this._injectStyles();
    }

    _buildPanel() {
        this._panel.innerHTML = '';
        if (this._options.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'ms-empty';
            empty.textContent = 'Sin opciones';
            this._panel.appendChild(empty);
            return;
        }
        this._options.forEach(opt => {
            const item = this._makeItem(opt);
            this._panel.appendChild(item);
        });
    }

    _rebuildPanel() {
        this._buildPanel();
    }

    _makeItem(opt) {
        const val   = String(opt.value);
        const label = opt.label;

        const item = document.createElement('label');
        item.className = 'ms-item';
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', String(this._selected.has(val)));

        const cb = document.createElement('input');
        cb.type    = 'checkbox';
        cb.value   = val;
        cb.checked = this._selected.has(val);

        cb.addEventListener('change', () => {
            if (cb.checked) {
                this._selected.add(val);
                item.setAttribute('aria-selected', 'true');
                item.classList.add('ms-item-checked');
            } else {
                this._selected.delete(val);
                item.setAttribute('aria-selected', 'false');
                item.classList.remove('ms-item-checked');
            }
            this._updateButton();
            if (this._onChange) this._onChange(this.getValues());
        });

        if (this._selected.has(val)) item.classList.add('ms-item-checked');

        const span = document.createElement('span');
        span.textContent = label;

        item.appendChild(cb);
        item.appendChild(span);
        return item;
    }

    _updateButton() {
        if (!this._btn) return;
        const count = this._selected.size;
        if (count === 0) {
            this._btn.textContent = this._placeholder;
            this._btn.classList.remove('ms-btn-active');
        } else if (count <= 2) {
            // Show abbreviated labels
            const labels = this._options
                .filter(o => this._selected.has(String(o.value)))
                .map(o => o.shortLabel || o.label);
            this._btn.textContent = labels.join(', ');
            this._btn.classList.add('ms-btn-active');
        } else {
            this._btn.textContent = `${count} seleccionados`;
            this._btn.classList.add('ms-btn-active');
        }
        // Add chevron via CSS — just ensure we add a trailing arrow character
        // (done via ::after in CSS)
    }

    _updateCheckboxes() {
        if (!this._panel) return;
        this._panel.querySelectorAll('input[type=checkbox]').forEach(cb => {
            const checked = this._selected.has(cb.value);
            cb.checked = checked;
            const item = cb.closest('.ms-item');
            if (item) {
                item.classList.toggle('ms-item-checked', checked);
                item.setAttribute('aria-selected', String(checked));
            }
        });
    }

    _openPanel() {
        this._panel.style.display = 'block';
        this._btn.setAttribute('aria-expanded', 'true');
        this._btn.classList.add('ms-btn-open');
        this._open = true;
    }

    _closePanel() {
        this._panel.style.display = 'none';
        this._btn.setAttribute('aria-expanded', 'false');
        this._btn.classList.remove('ms-btn-open');
        this._open = false;
    }

    _bindOutside() {
        document.addEventListener('click', (e) => {
            if (this._open && this._container && !this._container.contains(e.target)) {
                this._closePanel();
            }
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && this._open) this._closePanel();
        });
    }

    _injectStyles() {
        if (document.getElementById('multi-select-styles')) return;
        const style = document.createElement('style');
        style.id = 'multi-select-styles';
        style.textContent = `
/* ── MultiSelect widget ─────────────────────────────────────────────── */
.ms-wrap {
    position: relative;
    display: inline-block;
}

.ms-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    appearance: none;
    background-color: #374151;
    color: #e2e8f0;
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 8px;
    padding: 0.5rem 2rem 0.5rem 0.875rem;
    font-size: 0.875rem;
    font-family: 'Roboto', sans-serif;
    cursor: pointer;
    transition: border-color 0.2s, box-shadow 0.2s;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke='%236b7280'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M19 9l-7 7-7-7'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 0.5rem center;
    background-size: 1rem;
    white-space: nowrap;
    min-width: 110px;
    text-align: left;
}

.ms-btn:focus {
    outline: none;
    border-color: #60a5fa;
    box-shadow: 0 0 0 3px rgba(96,165,250,0.15);
}

.ms-btn.ms-btn-open {
    border-color: #60a5fa;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke='%2360a5fa'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M5 15l7-7 7 7'/%3E%3C/svg%3E");
}

.ms-btn.ms-btn-active {
    color: #93c5fd;
    border-color: rgba(59,130,246,0.4);
    background-color: rgba(59,130,246,0.1);
}

.ms-panel {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    z-index: 200;
    background-color: #1f2937;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
    min-width: 140px;
    max-height: 260px;
    overflow-y: auto;
    padding: 0.375rem 0;
}

.ms-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.45rem 0.875rem;
    cursor: pointer;
    font-size: 0.875rem;
    font-family: 'Roboto', sans-serif;
    color: #cbd5e1;
    transition: background 0.12s;
    user-select: none;
}

.ms-item:hover {
    background-color: rgba(255,255,255,0.07);
    color: #e2e8f0;
}

.ms-item.ms-item-checked {
    color: #93c5fd;
}

.ms-item input[type=checkbox] {
    accent-color: #3b82f6;
    width: 14px;
    height: 14px;
    flex-shrink: 0;
    cursor: pointer;
}

.ms-empty {
    padding: 0.6rem 0.875rem;
    font-size: 0.8rem;
    color: #6b7280;
    font-family: 'Roboto', sans-serif;
}

/* ── Light mode ──────────────────────────────────────────────────────── */
body.light-mode .ms-btn {
    background-color: #f8fafc;
    color: #1e293b;
    border-color: rgba(0,0,0,0.12);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 24 24' stroke='%239ca3af'%3E%3Cpath stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='M19 9l-7 7-7-7'/%3E%3C/svg%3E");
}

body.light-mode .ms-btn.ms-btn-active {
    color: #1d4ed8;
    border-color: rgba(59,130,246,0.4);
    background-color: rgba(59,130,246,0.06);
}

body.light-mode .ms-panel {
    background-color: #ffffff;
    border-color: rgba(0,0,0,0.1);
    box-shadow: 0 8px 24px rgba(0,0,0,0.12);
}

body.light-mode .ms-item {
    color: #374151;
}

body.light-mode .ms-item:hover {
    background-color: rgba(0,0,0,0.04);
    color: #111827;
}

body.light-mode .ms-item.ms-item-checked {
    color: #1d4ed8;
}
`;
        document.head.appendChild(style);
    }
}
