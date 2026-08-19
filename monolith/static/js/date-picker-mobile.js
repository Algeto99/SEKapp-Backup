/**
 * date-picker-mobile.js
 * 
 * Previene la apertura automática involuntaria del selector/calendario de fecha
 * en dispositivos móviles (Android Chrome, iOS Safari) al terminar el campo anterior
 * o navegar con la tecla "Siguiente"/Tab del teclado virtual.
 * 
 * Garantiza que el selector SOLO se despliegue cuando el usuario toque o haga clic
 * directamente sobre el campo de fecha o fecha/hora.
 */
(function () {
    'use strict';

    var SELECTOR = 'input[type="date"], input[type="datetime-local"]';

    /**
     * Inicializa un campo de fecha individual.
     */
    function initDateInput(el) {
        if (!el || el._dpMobileInit) return;
        el._dpMobileInit = true;

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
        }
    }

    /**
     * Maneja el clic directo sobre el campo de fecha.
     */
    function handleClick(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            target.readOnly = false;
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
     * Maneja el cambio de valor.
     */
    function handleChange(e) {
        var target = e.target;
        if (target && target.matches && target.matches(SELECTOR)) {
            if (document.activeElement !== target) {
                target.readOnly = true;
            }
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
     * Al enviar el formulario, desactiva temporalmente readOnly para que las
     * validaciones nativas de HTML5 (como 'required') se ejecuten sin restricciones.
     */
    function handleSubmit(e) {
        var form = e.target;
        if (form && form.querySelectorAll) {
            var dateInputs = form.querySelectorAll(SELECTOR);
            for (var i = 0; i < dateInputs.length; i++) {
                dateInputs[i].readOnly = false;
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

    // Exponer función de inicialización global por si se requiere invocar manualmente
    window.initDateInputsMobile = initAllDateInputs;
})();
