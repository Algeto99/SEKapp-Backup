/**
 * date-picker-mobile.js
 * 
 * 1. Previene la apertura automática involuntaria del selector/calendario de fecha
 *    en dispositivos móviles (Android Chrome, iOS Safari) al terminar el campo anterior
 *    o navegar con la tecla "Siguiente"/Tab del teclado virtual.
 * 
 * 2. RESTRICCIÓN GENERAL DE FECHAS FUTURAS:
 *    Configura de manera general para todos los formularios que los campos type="date"
 *    y type="datetime-local" no permitan seleccionar ni registrar fechas posteriores a
 *    la fecha y hora actual del sistema.
 * 
 *    Excepciones soportadas declarativamente mediante data-allow-future="true" o clase .allow-future
 *    (ej. Acta de Visita al Cliente, Asignar Hallazgos, fechas de vencimiento/compromiso y filtros de búsqueda).
 */
(function () {
    'use strict';

    var SELECTOR = 'input[type="date"], input[type="datetime-local"]';

    /**
     * Determina si un campo de fecha tiene permiso para aceptar fechas futuras.
     */
    function isFutureDateAllowed(el) {
        if (!el) return false;

        // Atributo explícito o clase en el input
        if (el.dataset && el.dataset.allowFuture === 'true') return true;
        if (el.getAttribute && el.getAttribute('data-allow-future') === 'true') return true;
        if (el.classList && el.classList.contains('allow-future')) return true;

        // Atributo o clase en formulario o contenedor padre
        if (el.closest) {
            if (el.closest('[data-allow-future="true"]')) return true;
            if (el.closest('.allow-future')) return true;
            // Filtros de búsqueda / dashboards
            if (el.closest('.dashboard-filters') || el.closest('#dashboardFilters') ||
                el.closest('.filters-wrap') || el.closest('.cgeo-filtros') || el.closest('.filter-group')) {
                return true;
            }
        }

        // Filtros por ID o clases conocidas
        var id = (el.id || '').toLowerCase();
        if (id.includes('filter') || id.startsWith('filt') || id.startsWith('filtro')) {
            return true;
        }
        if (el.classList && (el.classList.contains('date-input') || el.classList.contains('filter-date'))) {
            return true;
        }

        return false;
    }

    /**
     * Obtiene la cadena ISO local correspondiente al momento actual para el atributo max.
     */
    function getMaxDateString(type) {
        var now = new Date();
        var pad = function (n) { return String(n).padStart(2, '0'); };
        var yyyy = now.getFullYear();
        var mm = pad(now.getMonth() + 1);
        var dd = pad(now.getDate());

        if (type === 'datetime-local') {
            var hh = pad(now.getHours());
            var min = pad(now.getMinutes());
            return yyyy + '-' + mm + '-' + dd + 'T' + hh + ':' + min;
        }
        return yyyy + '-' + mm + '-' + dd;
    }

    /**
     * Aplica o actualiza el atributo max en el elemento si no permite fechas futuras.
     */
    function applyMaxConstraint(el) {
        if (!el || isFutureDateAllowed(el)) return;
        var maxVal = getMaxDateString(el.type);
        el.setAttribute('max', maxVal);
    }

    /**
     * Inicializa un campo de fecha individual.
     */
    function initDateInput(el) {
        if (!el || el._dpMobileInit) return;
        el._dpMobileInit = true;

        // Restringir fechas futuras si corresponde
        applyMaxConstraint(el);

        // Omite el campo en la navegación secuencial del teclado virtual ("Siguiente" / Tab)
        // para que el foco salte directamente al siguiente campo editable.
        if (!el.hasAttribute('tabindex')) {
            el.setAttribute('tabindex', '-1');
        }

        // Mantiene el estado en readOnly para que el foco no despierte el modal del calendario
        el.readOnly = true;

        // Estilo de cursor para indicar que es interactivo al tacto/clic
        el.style.cursor = 'pointer';
    }

    /**
     * Inicializa todos los campos de fecha presentes en el documento.
     */
    function initAllDateInputs(root) {
        var container = root || document;
        if (!container.querySelectorAll) return;
        var inputs = container.querySelectorAll(SELECTOR);
        for (var i = 0; i < inputs.length; i++) {
            initDateInput(inputs[i]);
        }
    }

    /**
     * Maneja eventos de toque o puntero directo del usuario.
     */
    function handlePointerDown(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            target._userDirectTouch = true;
            target.readOnly = false;
            applyMaxConstraint(target);
        }
    }

    /**
     * Maneja el clic directo sobre el campo de fecha.
     */
    function handleClick(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            target.readOnly = false;
            applyMaxConstraint(target);
            // Para navegadores modernos con soporte showPicker() (Chrome 99+, Safari 16+)
            try {
                if (typeof target.showPicker === 'function') {
                    target.showPicker();
                }
            } catch (err) {
                // Ignorar si ya está abierto o si el navegador maneja el clic nativamente
            }
        }
    }

    /**
     * Maneja el evento focusin (delegado).
     */
    function handleFocusIn(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            applyMaxConstraint(target);
            if (!target._userDirectTouch) {
                // Foco recibido sin toque directo (navegación por teclado o script) -> mantener readOnly
                target.readOnly = true;
            }
            // Limpiar la bandera tras establecer el foco
            setTimeout(function () {
                if (target) target._userDirectTouch = false;
            }, 150);
        }
    }

    /**
     * Maneja el evento focusout (delegado).
     */
    function handleFocusOut(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            target.readOnly = true;
            target._userDirectTouch = false;
        }
    }

    /**
     * Valida el valor del campo frente a la restricción de fecha máxima.
     */
    function validateInputDate(el) {
        if (!el || isFutureDateAllowed(el)) return true;
        var maxVal = getMaxDateString(el.type);
        if (el.value && el.value > maxVal) {
            el.setCustomValidity('No se permiten fechas u horas posteriores a la actual.');
            if (typeof el.reportValidity === 'function') {
                el.reportValidity();
            }
            return false;
        } else {
            el.setCustomValidity('');
            return true;
        }
    }

    /**
     * Maneja el cambio de valor.
     */
    function handleChange(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            validateInputDate(target);
            if (document.activeElement !== target) {
                target.readOnly = true;
            }
        }
    }

    /**
     * Maneja el evento input en tiempo real.
     */
    function handleInput(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            validateInputDate(target);
        }
    }

    /**
     * Soporte de accesibilidad por teclado (Enter o Barra espaciadora para abrir el selector).
     */
    function handleKeyDown(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
                target.readOnly = false;
                applyMaxConstraint(target);
                try {
                    if (typeof target.showPicker === 'function') {
                        e.preventDefault();
                        target.showPicker();
                    }
                } catch (err) {}
            }
        }
    }

    /**
     * Al enviar el formulario:
     * 1. Valida que ningún campo de fecha tenga valores futuros no autorizados.
     * 2. Desactiva temporalmente readOnly para que las validaciones nativas de HTML5 se ejecuten.
     */
    function handleSubmit(e) {
        var form = e.target;
        if (form && form.querySelectorAll) {
            var dateInputs = form.querySelectorAll(SELECTOR);
            for (var i = 0; i < dateInputs.length; i++) {
                var input = dateInputs[i];
                if (!validateInputDate(input)) {
                    e.preventDefault();
                    e.stopPropagation();
                    input.focus();
                    return false;
                }
                input.readOnly = false;
            }
        }
    }

    // Delegación global de eventos
    document.addEventListener('pointerdown', handlePointerDown, true);
    document.addEventListener('touchstart', handlePointerDown, { capture: true, passive: true });
    document.addEventListener('mousedown', handlePointerDown, true);
    document.addEventListener('click', handleClick, true);
    document.addEventListener('focusin', handleFocusIn, true);
    document.addEventListener('focusout', handleFocusOut, true);
    document.addEventListener('change', handleChange, true);
    document.addEventListener('input', handleInput, true);
    document.addEventListener('keydown', handleKeyDown, true);
    document.addEventListener('submit', handleSubmit, true);

    // Observador de mutaciones para campos insertados dinámicamente en tiempo de ejecución
    if (typeof MutationObserver !== 'undefined') {
        var observer = new MutationObserver(function (mutations) {
            for (var i = 0; i < mutations.length; i++) {
                var mutation = mutations[i];
                if (mutation.addedNodes && mutation.addedNodes.length > 0) {
                    for (var j = 0; j < mutation.addedNodes.length; j++) {
                        var node = mutation.addedNodes[j];
                        if (node.nodeType === 1) { // Elemento HTML
                            if (node.matches && node.matches(SELECTOR)) {
                                initDateInput(node);
                            } else if (node.querySelectorAll) {
                                initAllDateInputs(node);
                            }
                        }
                    }
                }
            }
        });

        if (document.body) {
            observer.observe(document.body, { childList: true, subtree: true });
        } else {
            document.addEventListener('DOMContentLoaded', function () {
                if (document.body) {
                    observer.observe(document.body, { childList: true, subtree: true });
                }
            });
        }
    }

    // Inicialización al cargar el DOM
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            initAllDateInputs();
        });
    } else {
        initAllDateInputs();
    }

    // Exponer funciones globales
    window.initDateInputsMobile = initAllDateInputs;
    window.applyMaxDateConstraint = applyMaxConstraint;
})();

