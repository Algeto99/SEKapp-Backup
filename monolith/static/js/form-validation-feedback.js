/**
 * form-validation-feedback.js
 *
 * Dice qué falta cuando el navegador detiene el envío de un formulario.
 *
 * La validación sigue siendo la nativa; esto solo la hace visible, porque por sí
 * sola es muda en los dos casos que más aparecen en campo:
 *
 *   · iOS Safari no dibuja la burbuja de validación. El campo queda enfocado y
 *     ya está: el botón "Enviar" parece no hacer nada.
 *   · Un control obligatorio oculto no lo puede señalar ningún navegador. Las
 *     cuatro vistas de las planillas pre-operacionales son
 *     `<input type="file" style="display:none" required>` detrás de una tarjeta,
 *     así que sin foto el envío se cancela sin mensaje en cualquier dispositivo.
 *
 * Se carga desde _sidebar.html, que incluyen todos los formularios de campo, y
 * escucha en `document` para cubrirlos a todos sin tocar cada plantilla.
 */
(function () {
    'use strict';

    if (window.secappValidationFeedbackLoaded) return;
    window.secappValidationFeedbackLoaded = true;

    var ESTILOS = [
        '.secapp-vf-panel{margin:14px 0 4px;padding:12px 14px;border:1px solid #dc2626;',
        '  border-radius:8px;background:#fee2e2;color:#7f1d1d;font-size:.85rem;line-height:1.45;}',
        '.secapp-vf-panel[hidden]{display:none;}',
        '.secapp-vf-title{font-weight:700;margin:0 0 6px;}',
        '.secapp-vf-list{margin:0;padding:0;list-style:none;}',
        '.secapp-vf-item{padding:3px 0;cursor:pointer;text-decoration:underline;',
        '  text-underline-offset:2px;}',
        '.secapp-vf-item:hover{color:#dc2626;}',
        '.secapp-vf-where{opacity:.75;font-weight:400;}',
        /* El realce va en el ancla visible, que puede ser el propio control o la
           tarjeta que lo envuelve cuando el control está oculto. */
        '.secapp-vf-target{outline:2px solid #dc2626 !important;outline-offset:2px;',
        '  border-radius:6px;}'
    ].join('\n');

    function inyectarEstilos() {
        if (document.getElementById('secapp-vf-styles')) return;
        var s = document.createElement('style');
        s.id = 'secapp-vf-styles';
        s.textContent = ESTILOS;
        (document.head || document.documentElement).appendChild(s);
    }

    function texto(valor) {
        return String(valor == null ? '' : valor)
            .replace(/\s+/g, ' ')
            .replace(/[*:·]+$/, '')
            .trim()
            .slice(0, 90);
    }

    function esVisible(el) {
        return !!(el && el.getClientRects && el.getClientRects().length);
    }

    /* Adónde llevar al usuario. Un control oculto no se puede enfocar ni
     * desplazar, pero el bloque que lo representa —la tarjeta de la foto— sí. */
    function anclaDe(el) {
        for (var n = el; n && n !== document.body; n = n.parentElement) {
            if (esVisible(n)) return n;
        }
        return el;
    }

    /* El nombre del campo con las mismas palabras que ve el usuario. Se prueba
     * de lo más específico a lo más genérico. */
    function etiquetaDe(el) {
        var conDato = el.closest('[data-label]');
        if (conDato && conDato.dataset.label) return texto(conDato.dataset.label);

        if (el.id) {
            try {
                var puesta = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]');
                if (puesta) return texto(puesta.textContent);
            } catch (err) { /* un id que no se puede consultar no es motivo de nada */ }
        }

        // Los combobox de customer-hierarchy.js meten un .ss-wrap entre la
        // etiqueta y el control, así que se sube desde ahí.
        var host = el.closest('.ss-wrap') || el.closest('.ss-manual') || el;
        for (var n = host.parentElement; n && n.tagName !== 'FORM'; n = n.parentElement) {
            var lab = n.querySelector('label');
            if (lab && texto(lab.textContent)) return texto(lab.textContent);
            if (n.classList && n.classList.contains('form-section')) break;
        }

        return texto(el.getAttribute('aria-label') || el.placeholder || el.name) || 'un campo obligatorio';
    }

    /* En qué parte del formulario está. `.form-section` con un <h2> es la
     * convención de todas las plantillas; los bloques de supervisión traen
     * además su propio encabezado, ya renumerado cuando se elimina uno. */
    function seccionDe(el) {
        var bloque = el.closest('.supervision-block');
        if (bloque) {
            var h = bloque.querySelector('h2');
            if (h) return texto(h.textContent);
        }
        var seccion = el.closest('.form-section');
        var titulo = seccion && seccion.querySelector('h2, h3');
        return titulo ? texto(titulo.textContent) : '';
    }

    function iconoDe(el, etiqueta) {
        var t = (etiqueta + ' ' + (el.name || '')).toLowerCase();
        if (el.type === 'file' || /foto|imagen|evidencia|anexo/.test(t)) return '📷';
        if (/firma/.test(t)) return '✍️';
        return '•';
    }

    function panelDe(form) {
        var panel = form.querySelector('.secapp-vf-panel');
        if (!panel) {
            panel = document.createElement('div');
            panel.className = 'secapp-vf-panel';
            panel.setAttribute('role', 'alert');
            panel.setAttribute('aria-live', 'assertive');
            panel.hidden = true;
            form.appendChild(panel);
        }
        return panel;
    }

    function irA(el) {
        var ancla = anclaDe(el);
        if (esVisible(el) && typeof el.focus === 'function') {
            try { el.focus({ preventScroll: true }); } catch (err) { /* el foco es opcional */ }
        }
        try { ancla.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
        catch (err) { ancla.scrollIntoView(true); }
    }

    function limpiar(form) {
        form.querySelectorAll('.secapp-vf-target').forEach(function (el) {
            el.classList.remove('secapp-vf-target');
        });
        var panel = form.querySelector('.secapp-vf-panel');
        if (panel) {
            panel.textContent = '';
            panel.hidden = true;
        }
    }

    function pintar(form, campos, mover) {
        limpiar(form);

        var panel = panelDe(form);
        var titulo = document.createElement('p');
        titulo.className = 'secapp-vf-title';
        titulo.textContent = campos.length === 1
            ? 'No se puede enviar. Falta completar:'
            : 'No se puede enviar. Faltan ' + campos.length + ' campos por completar:';
        panel.appendChild(titulo);

        var lista = document.createElement('ul');
        lista.className = 'secapp-vf-list';
        var vistos = {};

        campos.forEach(function (el) {
            anclaDe(el).classList.add('secapp-vf-target');

            var etiqueta = etiquetaDe(el);
            var seccion = seccionDe(el);
            var clave = seccion + '|' + etiqueta;
            if (vistos[clave]) return;
            vistos[clave] = true;

            var item = document.createElement('li');
            item.className = 'secapp-vf-item';
            item.textContent = iconoDe(el, etiqueta) + ' ' + etiqueta;
            if (seccion) {
                var donde = document.createElement('span');
                donde.className = 'secapp-vf-where';
                donde.textContent = ' — ' + seccion;
                item.appendChild(donde);
            }
            // Con varios pendientes, tocar el renglón lleva hasta el campo.
            item.addEventListener('click', function () { irA(el); });
            lista.appendChild(item);
        });

        panel.appendChild(lista);
        panel.hidden = false;
        if (mover) irA(campos[0]);
    }

    // ── Recolección ─────────────────────────────────────────────────────────
    // 'invalid' no burbujea: se escucha en captura. El navegador lo dispara una
    // vez por control antes de cancelar el envío, así que se acumulan y se
    // procesan todos juntos al terminar la tanda.
    var pendientes = [];
    var temporizador = null;

    function procesar() {
        var campos = pendientes;
        pendientes = [];
        if (!campos.length) return;
        inyectarEstilos();
        var porFormulario = new Map();
        campos.forEach(function (el) {
            var form = el.form || el.closest('form');
            if (!form) return;
            if (!porFormulario.has(form)) porFormulario.set(form, []);
            porFormulario.get(form).push(el);
        });
        porFormulario.forEach(function (lista, form) { pintar(form, lista, true); });
    }

    document.addEventListener('invalid', function (event) {
        var el = event.target;
        if (!el || !el.form) return;
        pendientes.push(el);
        clearTimeout(temporizador);
        temporizador = setTimeout(procesar, 0);
    }, true);

    /* El realce se retira en cuanto el campo queda correcto, sin esperar a otro
     * intento. Se consulta .validity y no checkValidity(), que volvería a
     * disparar 'invalid' y a arrastrar la página hasta el campo. Se aplaza un
     * turno porque al elegir en un combobox el 'change' sale antes de que el
     * propio combobox sincronice el control visible. */
    function revisar(form) {
        if (!form.querySelector('.secapp-vf-target')) return;
        var invalidos = [];
        form.querySelectorAll('input, select, textarea').forEach(function (el) {
            if (el.willValidate && !el.validity.valid) invalidos.push(el);
        });
        // La lista se vuelve a pintar con lo que aún falta, para que el aviso no
        // siga nombrando campos ya resueltos.
        if (invalidos.length) pintar(form, invalidos, false);
        else limpiar(form);
    }

    ['input', 'change'].forEach(function (tipo) {
        document.addEventListener(tipo, function (event) {
            var form = event.target && event.target.form;
            if (!form || !form.querySelector('.secapp-vf-target')) return;
            setTimeout(function () { revisar(form); }, 0);
        }, true);
    });

    // Un envío que sí sale deja el aviso anterior sin sentido.
    document.addEventListener('submit', function (event) {
        if (event.target && event.target.tagName === 'FORM') limpiar(event.target);
    }, true);
})();
