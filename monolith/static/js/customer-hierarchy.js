(function () {
    const PROPERTIES_URL = '/forms/api/properties';
    const PROPERTIES_STORAGE_KEY = 'secapp:properties:v1';
    const PROPERTIES_FETCH_TIMEOUT_MS = 4000;

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
        try {
            return validPropertiesPayload(JSON.parse(localStorage.getItem(PROPERTIES_STORAGE_KEY) || 'null'));
        } catch {
            return null;
        }
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

    async function loadProperties() {
        const cached = await readPropertiesFromCacheStorage() || readPropertiesFromLocalStorage();
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

    async function initPropertySelector() {
        const propertySelect = document.getElementById('id_propiedad');
        if (!propertySelect) return;

        const legacyInput = document.getElementById('cliente_instalacion')
                         || document.getElementById('cliente_visitado');

        let data;
        try {
            data = await loadProperties();
        } catch {
            showOfflineTextFallback(propertySelect, legacyInput);
            return;
        }

        if (!data.properties.length) {
            renderEmptyState(propertySelect, 'No hay propiedades disponibles');
            return;
        }

        propertySelect.innerHTML = '';
        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = 'Seleccione una propiedad / instalación...';
        empty.selected = true;
        empty.disabled = true;
        propertySelect.appendChild(empty);

        data.properties.forEach((p) => {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.cliente ? `${p.name} (${p.cliente})` : p.name;
            propertySelect.appendChild(opt);
        });

        propertySelect.addEventListener('change', () => {
            const selected = data.properties.find((p) => String(p.id) === propertySelect.value);
            if (legacyInput) legacyInput.value = selected ? selected.name : '';
        });
    }

    // Exposed for the "Preparar modo offline" button — fetches and lets the SW cache the response
    window.secappPrefetchProperties = async function () {
        return fetchPropertiesFromNetwork();
    };

    document.addEventListener('DOMContentLoaded', initPropertySelector);
})();
