(function () {
    function createWrapper(labelText, control) {
        const wrapper = document.createElement('div');
        const label = document.createElement('label');
        label.textContent = labelText;
        label.className = 'block text-sm font-medium mb-1 form-label';
        wrapper.appendChild(label);
        wrapper.appendChild(control);
        return wrapper;
    }

    function createSelect(name, required) {
        const select = document.createElement('select');
        select.name = name;
        select.className = 'form-input form-select';
        if (required) select.required = true;
        return select;
    }

    function fillSelect(select, placeholder, items) {
        select.innerHTML = '';
        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = placeholder;
        empty.selected = true;
        empty.disabled = true;
        select.appendChild(empty);

        items.forEach((item) => {
            const option = document.createElement('option');
            option.value = item.id;
            option.textContent = item.name;
            select.appendChild(option);
        });
    }

    async function initCustomerHierarchy() {
        const legacyInput = document.getElementById('cliente_instalacion') || document.getElementById('cliente_visitado');
        if (!legacyInput) return;

        let response;
        try {
            response = await fetch('/forms/api/customer-hierarchy');
        } catch (err) {
            console.warn('Customer hierarchy unavailable', err);
            return;
        }

        if (!response.ok) return;

        const data = await response.json();
        if (!data.customers || !data.customers.length) return;

        const customerSelect = createSelect('customer_company_id', true);
        const propertySelect = createSelect('id_propiedad', false);
        propertySelect.disabled = true;

        fillSelect(customerSelect, 'Seleccione un cliente...', data.customers);
        fillSelect(propertySelect, 'Seleccione una propiedad...', []);

        const customerWrapper = createWrapper('Cliente', customerSelect);
        const propertyWrapper = createWrapper('Propiedad / Instalación', propertySelect);

        const fieldContainer = legacyInput.closest('div');
        if (!fieldContainer || !fieldContainer.parentNode) return;

        fieldContainer.parentNode.insertBefore(customerWrapper, fieldContainer);
        fieldContainer.parentNode.insertBefore(propertyWrapper, fieldContainer.nextSibling);

        const legacyLabel = document.querySelector(`label[for="${legacyInput.id}"]`);
        if (legacyLabel) legacyLabel.style.display = 'none';
        legacyInput.type = 'hidden';

        customerSelect.addEventListener('change', () => {
            const customer = data.customers.find((item) => String(item.id) === customerSelect.value);
            const properties = (customer && customer.properties) || [];
            fillSelect(propertySelect, properties.length ? 'Seleccione una propiedad...' : 'Sin propiedades registradas', properties);
            propertySelect.disabled = properties.length === 0;
            legacyInput.value = customer ? customer.name : '';
        });

        propertySelect.addEventListener('change', () => {
            const customer = data.customers.find((item) => String(item.id) === customerSelect.value);
            const property = customer && customer.properties.find((item) => String(item.id) === propertySelect.value);
            legacyInput.value = property ? property.name : (customer ? customer.name : '');
        });
    }

    document.addEventListener('DOMContentLoaded', initCustomerHierarchy);
})();
