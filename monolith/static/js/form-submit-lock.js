/**
 * form-submit-lock.js
 *
 * Control global de envío duplicado para todos los formularios de SEKapp.
 *
 * Previene que el usuario genere envíos múltiples al presionar varias veces
 * el botón "Enviar" o "Guardar" mientras la aplicación procesa la solicitud
 * (subida de fotos, firmas o latencia de red).
 *
 * Características:
 *  - Intercepta el evento `submit` a nivel de `document`.
 *  - Respeta la validación nativa (no bloquea si faltan campos obligatorios).
 *  - Bloquea temporalmente los botones de envío con indicador visual (spinner + "Enviando...").
 *  - Inyecta un token de idempotencia del cliente (`client_submission_id`).
 *  - Restaura el estado en caso de error de validación (`invalid`), navegación atrás (`pageshow`)
 *    o expiración de tiempo de seguridad (30 s).
 */
(function () {
    'use strict';

    if (window.secappSubmitLockLoaded) return;
    window.secappSubmitLockLoaded = true;

    // Inyectar estilos para el estado de envío y el spinner
    var ESTILOS = [
        '.secapp-btn-submitting {',
        '  pointer-events: none !important;',
        '  cursor: not-allowed !important;',
        '  opacity: 0.75 !important;',
        '  position: relative !important;',
        '  display: inline-flex !important;',
        '  align-items: center !important;',
        '  justify-content: center !important;',
        '  gap: 8px !important;',
        '}',
        '@keyframes secapp-spin {',
        '  from { transform: rotate(0deg); }',
        '  to { transform: rotate(360deg); }',
        '}',
        '.secapp-spinner-icon {',
        '  animation: secapp-spin 0.8s linear infinite;',
        '  display: inline-block;',
        '  width: 18px;',
        '  height: 18px;',
        '  vertical-align: middle;',
        '  flex-shrink: 0;',
        '}'
    ].join('\n');

    function inyectarEstilos() {
        if (document.getElementById('secapp-submit-lock-styles')) return;
        var s = document.createElement('style');
        s.id = 'secapp-submit-lock-styles';
        s.textContent = ESTILOS;
        (document.head || document.documentElement).appendChild(s);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', inyectarEstilos);
    } else {
        inyectarEstilos();
    }

    function generarTokenEnvio() {
        return 'sub_' + Date.now().toString(36) + '_' + Math.random().toString(36).substring(2, 9);
    }

    function asegurarTokenEnForm(form) {
        var inputToken = form.querySelector('input[name="client_submission_id"]');
        if (!inputToken) {
            inputToken = document.createElement('input');
            inputToken.type = 'hidden';
            inputToken.name = 'client_submission_id';
            form.appendChild(inputToken);
        }
        if (!inputToken.value) {
            inputToken.value = generarTokenEnvio();
        }
        return inputToken.value;
    }

    function obtenerBotonesEnvio(form) {
        var selector = 'button[type="submit"], input[type="submit"], button#submit-btn, button#submitBtn, button.btn-submit';
        var botones = Array.from(form.querySelectorAll(selector));
        // Si no se encontró ninguno explícito, buscar el último botón del form que no sea reset/cancel
        if (botones.length === 0) {
            var allBtns = Array.from(form.querySelectorAll('button:not([type="button"]):not([type="reset"])'));
            if (allBtns.length > 0) {
                botones = [allBtns[allBtns.length - 1]];
            }
        }
        return botones;
    }

    var SPINNER_SVG = '<svg class="secapp-spinner-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
        '<circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle>' +
        '<path d="M12 2a10 10 0 0 1 10 10"></path>' +
        '</svg>';

    function bloquearBoton(btn) {
        if (!btn || btn.classList.contains('secapp-btn-submitting')) return;

        // Conservar ancho original para evitar saltos de layout
        var rect = btn.getBoundingClientRect();
        if (rect && rect.width > 0 && !btn.dataset.origWidth) {
            btn.dataset.origWidth = btn.style.width || '';
            btn.style.width = Math.max(rect.width, 140) + 'px';
        }

        if (!btn.dataset.origHtml) {
            btn.dataset.origHtml = btn.innerHTML;
        }

        btn.classList.add('secapp-btn-submitting');
        btn.setAttribute('aria-busy', 'true');
        btn.disabled = true;

        // Texto visual según el contexto
        var texto = 'Enviando...';
        var origLower = (btn.textContent || '').toLowerCase();
        if (origLower.indexOf('guardar') !== -1) {
            texto = 'Guardando...';
        } else if (origLower.indexOf('actualizar') !== -1) {
            texto = 'Actualizando...';
        }

        btn.innerHTML = SPINNER_SVG + '<span>' + texto + '</span>';
    }

    function desbloquearBoton(btn) {
        if (!btn) return;
        btn.classList.remove('secapp-btn-submitting');
        btn.removeAttribute('aria-busy');
        btn.disabled = false;
        if (btn.dataset.origHtml) {
            btn.innerHTML = btn.dataset.origHtml;
            delete btn.dataset.origHtml;
        }
        if (btn.dataset.origWidth !== undefined) {
            btn.style.width = btn.dataset.origWidth;
            delete btn.dataset.origWidth;
        }
    }

    function desbloquearFormulario(form) {
        if (!form) return;
        form.dataset.submitting = 'false';
        if (form._secappTimeout) {
            clearTimeout(form._secappTimeout);
            form._secappTimeout = null;
        }
        var botones = obtenerBotonesEnvio(form);
        botones.forEach(desbloquearBoton);
    }

    // Interceptar evento submit
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.tagName !== 'FORM') return;

        // Si ya se está enviando, frenar cualquier envío subsecuente
        if (form.dataset.submitting === 'true') {
            e.preventDefault();
            e.stopImmediatePropagation();
            return false;
        }

        // Si la validación nativa del formulario falla, NO bloquear
        if (form.checkValidity && !form.checkValidity()) {
            return;
        }

        // Marcar formulario en proceso de envío
        form.dataset.submitting = 'true';
        asegurarTokenEnForm(form);

        var botones = obtenerBotonesEnvio(form);
        // Usar setTimeout para permitir que el valor del botón que disparó el submit se incluya
        // en los datos del formulario antes de deshabilitarlo
        setTimeout(function () {
            botones.forEach(bloquearBoton);
        }, 0);

        // Temporizador de seguridad: si pasados 30 segundos no ha respondido la red ni navegado,
        // restaurar los botones para que el usuario no quede atrapado.
        if (form._secappTimeout) clearTimeout(form._secappTimeout);
        form._secappTimeout = setTimeout(function () {
            if (form.dataset.submitting === 'true') {
                desbloquearFormulario(form);
                // Regenerar token para que el próximo clic sea una solicitud fresca
                var inputToken = form.querySelector('input[name="client_submission_id"]');
                if (inputToken) inputToken.value = generarTokenEnvio();
            }
        }, 30000);

    }, false);

    // Si algún elemento dispara 'invalid' (validación fallida), desbloquear el formulario
    document.addEventListener('invalid', function (e) {
        var el = e.target;
        if (el && el.form) {
            desbloquearFormulario(el.form);
        }
    }, true);

    // Soporte para bfcache (navegar atrás o recargar desde caché)
    window.addEventListener('pageshow', function (e) {
        var forms = document.querySelectorAll('form[data-submitting="true"]');
        forms.forEach(desbloquearFormulario);
    });

    // Exponer API global de conveniencia
    window.SecAppSubmitLock = {
        lock: function (form) {
            if (form) {
                form.dataset.submitting = 'true';
                obtenerBotonesEnvio(form).forEach(bloquearBoton);
            }
        },
        unlock: desbloquearFormulario,
        resetToken: function (form) {
            if (form) {
                var input = form.querySelector('input[name="client_submission_id"]');
                if (input) input.value = generarTokenEnvio();
            }
        }
    };
})();
