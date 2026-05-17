(function () {
    async function initPropertySelector() {
        const propertySelect = document.getElementById('id_propiedad');
        if (!propertySelect) return;

        // Hidden legacy inputs that carry the property name as plain text
        const legacyInput = document.getElementById('cliente_instalacion')
                         || document.getElementById('cliente_visitado');

        let response;
        try {
            response = await fetch('/forms/api/properties');
        } catch (err) {
            console.warn('Properties list unavailable', err);
            return;
        }

        if (!response.ok) return;

        const data = await response.json();
        if (!data.properties || !data.properties.length) return;

        // Replace the loading placeholder with real options
        propertySelect.innerHTML = '';
        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = 'Seleccione una propiedad / instalación...';
        empty.selected = true;
        empty.disabled = true;
        propertySelect.appendChild(empty);

        data.properties.forEach((p) => {
            const option = document.createElement('option');
            option.value = p.id;
            option.textContent = p.cliente ? `${p.name} (${p.cliente})` : p.name;
            propertySelect.appendChild(option);
        });

        propertySelect.addEventListener('change', () => {
            const selected = data.properties.find((p) => String(p.id) === propertySelect.value);
            if (legacyInput) legacyInput.value = selected ? selected.name : '';
        });
    }

    document.addEventListener('DOMContentLoaded', initPropertySelector);
})();
