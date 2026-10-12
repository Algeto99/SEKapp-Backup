/**
 * form-draft.js
 *
 * Borrador automático de los formularios de campo.
 *
 * En el teléfono basta una llamada entrante o un cambio a otra app para que el
 * sistema cierre la pestaña o la PWA por memoria. Al volver, el navegador vuelve
 * a cargar la página y todo lo que el Supervisor de Seguridad había escrito
 * desaparece: ningún dato vivía fuera de la página. (El service worker y
 * offline-sync.js sólo guardan envíos ya hechos, no formularios a medio llenar.)
 *
 * Qué hace:
 *  · A cada edición del usuario, y al pasar a segundo plano, guarda en
 *    localStorage el valor de todos los controles del formulario, las firmas
 *    (como trazos de SignaturePad) y qué bloques dinámicos había.
 *  · Guarda las fotos y adjuntos en una base IndexedDB propia
 *    (`secapp-borradores`), separada de la cola offline: su versión la fijan
 *    sw.js y offline-sync.js y subirla rompería la cola.
 *  · Al cargar la página, después de la inicialización propia de cada plantilla,
 *    vuelve a crear los bloques pulsando los mismos botones "Añadir", repone los
 *    valores disparando input/change para que las secciones condicionales se
 *    recalculen, dibuja las firmas, devuelve los archivos a cada widget por su
 *    propio input y lleva al usuario al último campo que estaba editando.
 *  · Los combos que llenan sus opciones desde la red después de cargar
 *    (cliente / instalación, puesto, placa) se reintentan cuando aparecen.
 *  · El borrador se borra al enviar (si la validación dejó salir el envío), al
 *    reiniciar el formulario, al descartarlo desde el aviso, o a las 48 horas.
 *
 * Se carga desde _sidebar.html, que incluyen los once formularios de campo, y
 * sólo actúa sobre formularios POST cuyo action va a /forms/submit_*. No
 * requiere cambios en las plantillas ni en los widgets de foto.
 */
(function () {
    'use strict';

    if (window.secappFormDraftLoaded) return;
    window.secappFormDraftLoaded = true;

    var PREFIJO = 'secapp:borrador:';
    var FORMATO = 1;
    var VIGENCIA_MS = 48 * 60 * 60 * 1000;
    var ESPERA_GUARDADO_MS = 400;
    var ESPERA_REINTENTOS_MS = 25000;
    var DB_NOMBRE = 'secapp-borradores';
    var DB_VERSION = 1;
    var ALMACEN = 'archivos';
    var CLAVE_SESION_CAPACITACION = 'capacitacion_session_token';

    // Deben venir frescos en cada carga: el token CSRF del formulario, el token de
    // idempotencia que siembra form-submit-lock.js y la ubicación.
    var EXCLUIDOS = {
        csrf_token: 1, client_submission_id: 1,
        latitude: 1, longitude: 1, location_accuracy: 1, submitter_timezone: 1
    };
    var TIPOS_IGNORADOS = { file: 1, password: 1, submit: 1, button: 1, reset: 1, image: 1 };

    // Un borrador por usuario, página y formulario: la ruta ya distingue el alta
    // de la edición de cada registro.
    var USUARIO = (function () {
        var s = document.currentScript;
        var u = s && s.dataset ? s.dataset.usuario : '';
        return String(u || '').trim().toLowerCase() || 'anon';
    })();

    function claveDe(idForm) {
        return PREFIJO + USUARIO + ':' + location.pathname + '#' + (idForm || 'form');
    }

    // Bloques que el usuario añade con un botón. Se vuelven a crear pulsando ese
    // mismo botón, así cada plantilla sigue armándolos a su manera. `indice` es
    // el atributo (o función) que identifica cada bloque cuando la numeración
    // puede tener huecos; sin él, cuenta la cantidad.
    var REPETIDORES = {
        supervisionForm: [
            { agregar: '#addSupervisionBtn', bloque: '.supervision-block', quitar: '.remove-supervision', indice: 'data-index' }
        ],
        confiabilidadForm: [
            { agregar: '#addInventarioBtn', bloque: '.inventory-block', quitar: '.remove-inv-btn', indice: 'data-index' }
        ],
        visitaForm: [
            { agregar: '#addBlockBtn', bloque: '.block-group', quitar: '.remove-block', indice: 'data-block-index' },
            {
                agregar: '#addAsistenteBtn', bloque: '.asistente-item', quitar: '.remove-asistente',
                indice: function (bloque) {
                    var campo = bloque.querySelector('input[name^="nombre_participante_cliente_"]');
                    return campo ? campo.name.replace('nombre_participante_cliente_', '') : null;
                }
            }
        ],
        checklistForm: [
            { agregar: 'button[onclick*="addChecklistBlock"]', bloque: '.checklist-block', quitar: null, indice: null }
        ],
        capacitacionForm: [
            { agregar: '#add-attendee', bloque: '.attendee-block', quitar: null, indice: null }
        ]
    };

    var formularios = [];

    // ── Firmas: registro de los SignaturePad que crea cada plantilla ─────────
    // Las plantillas guardan sus pads en variables privadas. Envolver el
    // constructor es la única forma de llegar a ellos sin tocar cada plantilla:
    // la librería se carga en el <head> en unas y después de este script en
    // otras, por eso el envoltorio se instala ya y también sobre la asignación
    // futura de window.SignaturePad.
    var padsPorCanvas = typeof WeakMap === 'function' ? new WeakMap() : null;

    function envolverSignaturePad(Original) {
        if (typeof Original !== 'function' || Original.__secappEnvuelto) return Original;
        function SignaturePadRegistrado(canvas, opciones) {
            var pad = new Original(canvas, opciones);
            try {
                if (padsPorCanvas && canvas) padsPorCanvas.set(canvas, pad);
                if (typeof pad.addEventListener === 'function') {
                    pad.addEventListener('endStroke', function () { alTrazar(canvas); });
                }
            } catch (e) { /* el registro es opcional */ }
            return pad;
        }
        SignaturePadRegistrado.prototype = Original.prototype;
        try { Object.setPrototypeOf(SignaturePadRegistrado, Original); } catch (e) { /* ES5 */ }
        SignaturePadRegistrado.__secappEnvuelto = true;
        return SignaturePadRegistrado;
    }

    function instalarRegistroFirmas() {
        if (!padsPorCanvas) return;
        var actual = envolverSignaturePad(window.SignaturePad);
        try {
            Object.defineProperty(window, 'SignaturePad', {
                configurable: true,
                enumerable: true,
                get: function () { return actual; },
                set: function (valor) { actual = envolverSignaturePad(valor); }
            });
        } catch (e) {
            if (actual) window.SignaturePad = actual;
        }
    }

    // ── Formularios rastreados ───────────────────────────────────────────────
    function esFormularioDeCampo(form) {
        var metodo = (form.getAttribute('method') || 'get').toLowerCase();
        var accion = form.getAttribute('action') || '';
        return metodo === 'post' && /\/forms\/submit_/.test(accion) && !/submit_asistencia_qr/.test(accion);
    }

    function estadoDe(form) {
        for (var i = 0; i < formularios.length; i++) {
            if (formularios[i].form === form) return formularios[i];
        }
        return null;
    }

    function estadoDeElemento(el) {
        if (!el || !(el instanceof Element)) return null;
        var form = el.form || (el.closest ? el.closest('form') : null);
        return form ? estadoDe(form) : null;
    }

    // ── Claves de los controles ──────────────────────────────────────────────
    // Por nombre con ordinal (los bloques clonados repiten nombres), por id si
    // no hay nombre, o por data-field (asistentes de capacitación). Lo que no
    // tiene ninguno es un control derivado (la caja visible de un combobox) y
    // se omite: su valor vive en el <select> que lo acompaña.
    function controlesDe(form) {
        var ordinales = {};
        var lista = [];
        Array.prototype.forEach.call(form.querySelectorAll('input, select, textarea'), function (el) {
            var tipo = (el.type || '').toLowerCase();
            var clave;
            if (el.name) {
                if (EXCLUIDOS[el.name]) return;
                var base = (tipo === 'radio' ? 'r:' + el.name + '=' + el.value : 'n:' + el.name);
                var n = ordinales[base] = (ordinales[base] || 0) + 1;
                clave = base + '#' + n;
            } else if (el.id) {
                if (el.readOnly) return; // sólo muestra algo (ej. fecha de la modificación)
                clave = 'i:' + el.id;
            } else if (el.dataset && el.dataset.field) {
                var m = ordinales['d:' + el.dataset.field] = (ordinales['d:' + el.dataset.field] || 0) + 1;
                clave = 'd:' + el.dataset.field + '#' + m;
            } else {
                return;
            }
            lista.push({ el: el, clave: clave, tipo: tipo });
        });
        return lista;
    }

    function entradasArchivo(form) {
        var ordinales = {};
        var lista = [];
        Array.prototype.forEach.call(form.querySelectorAll('input[type="file"]'), function (el) {
            var clave;
            if (el.name) {
                var n = ordinales[el.name] = (ordinales[el.name] || 0) + 1;
                clave = 'f:' + el.name + '#' + n;
            } else if (el.id) {
                clave = 'f:#' + el.id;
            } else {
                return;
            }
            lista.push({ el: el, clave: clave });
        });
        return lista;
    }

    function canvasesDe(form) {
        return Array.prototype.slice.call(form.querySelectorAll('canvas'));
    }

    function claveCanvas(canvas, i) {
        return 'c:' + (canvas.id || ('k' + i));
    }

    // Clave del control donde estaba el usuario, para volver ahí al restaurar.
    function claveDeElemento(estado, el) {
        if (!el) return null;
        var form = estado.form;
        if (el.tagName === 'CANVAS') {
            var canvases = canvasesDe(form);
            var i = canvases.indexOf(el);
            return i === -1 ? null : claveCanvas(el, i);
        }
        if (el.tagName === 'INPUT' && el.type === 'file') {
            var archivos = entradasArchivo(form);
            for (var a = 0; a < archivos.length; a++) if (archivos[a].el === el) return archivos[a].clave;
            // Inputs auxiliares de un widget (galería, cámara nativa): el que
            // cuenta es el input con nombre del mismo widget.
            var widget = el.closest ? el.closest('.secapp-photo-uploader, .foto-card') : null;
            var real = widget && widget.querySelector('input[type="file"][name]');
            for (var r = 0; real && r < archivos.length; r++) if (archivos[r].el === real) return archivos[r].clave;
            return null;
        }
        var objetivo = el;
        if (!el.name && !el.id && el.closest) {
            // La caja visible del combobox, su panel o la caja de texto libre
            // (puesto, placa): el valor vive en el <select> del mismo contenedor.
            var wrap = el.closest('.ss-wrap, .ss-manual, .fleet-manual');
            var host = wrap && wrap.parentElement;
            var select = host && host.querySelector('select');
            if (select) objetivo = select;
        }
        var controles = controlesDe(form);
        for (var c = 0; c < controles.length; c++) if (controles[c].el === objetivo) return controles[c].clave;
        return null;
    }

    function elementoDeClave(estado, clave) {
        if (!clave) return null;
        var form = estado.form;
        if (clave.indexOf('c:') === 0) {
            var canvases = canvasesDe(form);
            for (var i = 0; i < canvases.length; i++) if (claveCanvas(canvases[i], i) === clave) return canvases[i];
            return null;
        }
        if (clave.indexOf('f:') === 0) {
            var archivos = entradasArchivo(form);
            for (var a = 0; a < archivos.length; a++) if (archivos[a].clave === clave) return archivos[a].el;
            return null;
        }
        var controles = controlesDe(form);
        for (var c = 0; c < controles.length; c++) if (controles[c].clave === clave) return controles[c].el;
        return null;
    }

    // ── Valores ──────────────────────────────────────────────────────────────
    function leerValor(el, tipo) {
        if (tipo === 'checkbox' || tipo === 'radio') return !!el.checked;
        if (el.tagName === 'SELECT' && el.multiple) {
            return Array.prototype.filter.call(el.options, function (o) { return o.selected; })
                .map(function (o) { return o.value; });
        }
        return el.value;
    }

    function disparar(el, nombres) {
        nombres.forEach(function (nombre) {
            try { el.dispatchEvent(new Event(nombre, { bubbles: true })); } catch (e) { /* sin eventos */ }
        });
    }

    function tieneOpcion(select, valor) {
        return Array.prototype.some.call(select.options, function (o) { return o.value === valor; });
    }

    // Puesto y placa aceptan texto que no está en la lista; cliente e
    // instalación no. Los dos primeros son los únicos combos con el botón
    // "no está en la lista", que es la señal de que el combo ya terminó de
    // construirse y admite un valor libre.
    function admiteValorLibre(select) {
        if (!select.closest) return false;
        var wrap = select.closest('.ss-wrap');
        var host = wrap && wrap.parentElement;
        return !!(host && host.querySelector('.ss-switch, .fleet-switch'));
    }

    // Misma forma que la opción libre de customer-hierarchy.js, para que el
    // combobox la muestre y la reutilice como propia.
    function agregarOpcionLibre(select, texto) {
        var opcion = select.querySelector('option[data-custom="1"]');
        if (!opcion) {
            opcion = document.createElement('option');
            opcion.dataset.custom = '1';
            select.appendChild(opcion);
        }
        opcion.value = texto;
        opcion.textContent = texto;
        opcion.dataset.label = texto;
        opcion.dataset.search = texto;
        return opcion;
    }

    // Devuelve true si el valor quedó puesto. Un <select> cuyas opciones aún no
    // llegaron devuelve false y se reintenta cuando aparezcan.
    function aplicarValor(el, tipo, valor) {
        if (tipo === 'checkbox' || tipo === 'radio') {
            var marcado = !!valor;
            if (el.checked !== marcado) {
                el.checked = marcado;
                disparar(el, ['input', 'change']);
            }
            return true;
        }
        if (el.tagName === 'SELECT') {
            if (el.multiple && Array.isArray(valor)) {
                Array.prototype.forEach.call(el.options, function (o) { o.selected = valor.indexOf(o.value) !== -1; });
                disparar(el, ['change']);
                return true;
            }
            var v = String(valor == null ? '' : valor);
            if (!tieneOpcion(el, v)) {
                if (v && admiteValorLibre(el)) agregarOpcionLibre(el, v);
                else return false;
            }
            if (el.value !== v) {
                el.value = v;
                // Los selects de la jerarquía de clientes ya emiten change al asignar.
                if (!el.__secappValueHook) disparar(el, ['change']);
            }
            return true;
        }
        var texto = String(valor == null ? '' : valor);
        if (el.value !== texto) {
            el.value = texto;
            disparar(el, ['input', 'change']);
        }
        return true;
    }

    // ── Bloques dinámicos ────────────────────────────────────────────────────
    function bloquesDe(form, rep) {
        return Array.prototype.slice.call(form.querySelectorAll(rep.bloque));
    }

    function indicesDe(form, rep) {
        return bloquesDe(form, rep).map(function (bloque, i) {
            var ix = null;
            if (typeof rep.indice === 'function') ix = rep.indice(bloque);
            else if (rep.indice) ix = bloque.getAttribute(rep.indice);
            return ix == null ? String(i) : String(ix);
        });
    }

    function capturarBloques(form) {
        var reps = REPETIDORES[form.id];
        if (!reps) return null;
        return reps.map(function (rep) { return indicesDe(form, rep); });
    }

    function reconstruirBloques(form, guardados) {
        var reps = REPETIDORES[form.id];
        if (!reps || !guardados) return;
        reps.forEach(function (rep, j) {
            var lista = guardados[j];
            if (!lista || !lista.length) return;
            var boton = form.querySelector(rep.agregar);
            var conIndice = !!rep.indice;

            function falta() {
                var actuales = indicesDe(form, rep);
                if (conIndice) return lista.some(function (ix) { return actuales.indexOf(ix) === -1; });
                return actuales.length < lista.length;
            }

            var intentos = 0;
            while (boton && falta() && intentos < 60) {
                intentos++;
                var antes = bloquesDe(form, rep).length;
                try { boton.click(); } catch (e) { break; }
                if (bloquesDe(form, rep).length === antes) break;
            }

            // La numeración puede tener huecos (se eliminó un bloque intermedio):
            // se crean hasta alcanzar el mayor y se quitan los que no estaban.
            if (conIndice && rep.quitar) {
                bloquesDe(form, rep).forEach(function (bloque) {
                    var ix = typeof rep.indice === 'function' ? rep.indice(bloque) : bloque.getAttribute(rep.indice);
                    if (ix == null || lista.indexOf(String(ix)) !== -1) return;
                    var quitar = bloque.querySelector(rep.quitar);
                    if (quitar) { try { quitar.click(); } catch (e) { /* se queda */ } }
                });
            }
        });
    }

    // ── Firmas ───────────────────────────────────────────────────────────────
    function capturarFirmas(form) {
        var firmas = {};
        if (!padsPorCanvas) return firmas;
        canvasesDe(form).forEach(function (canvas, i) {
            var pad = padsPorCanvas.get(canvas);
            if (!pad || typeof pad.isEmpty !== 'function' || typeof pad.toData !== 'function') return;
            try {
                if (pad.isEmpty()) return;
                var trazos = pad.toData();
                if (trazos && trazos.length) firmas[claveCanvas(canvas, i)] = trazos;
            } catch (e) { /* esa firma no se guarda */ }
        });
        return firmas;
    }

    // Se reponen como trazos y no como imagen: fromData deja al pad como firmado
    // (el envío serializa sólo pads no vacíos) y sobrevive a los redibujados que
    // las plantillas hacen con toData/fromData al cambiar el tamaño.
    function reponerFirmas(form, firmas) {
        if (!firmas || !padsPorCanvas) return;
        canvasesDe(form).forEach(function (canvas, i) {
            var trazos = firmas[claveCanvas(canvas, i)];
            if (!trazos) return;
            var pad = padsPorCanvas.get(canvas);
            if (!pad || typeof pad.fromData !== 'function') return;
            try { pad.fromData(trazos); } catch (e) { /* esa firma no se repone */ }
        });
    }

    function alTrazar(canvas) {
        var estado = estadoDeElemento(canvas);
        if (!estado || estado.restaurando) return;
        estado.sucio = true;
        estado.enviado = false;
        estado.ultimoEl = canvas;
        programar(estado);
    }

    // ── Archivos (IndexedDB) ─────────────────────────────────────────────────
    var dbPromesa = null;

    function abrirDB() {
        if (dbPromesa) return dbPromesa;
        dbPromesa = new Promise(function (resolve, reject) {
            if (!window.indexedDB) { reject(new Error('IndexedDB no disponible')); return; }
            var req;
            try { req = window.indexedDB.open(DB_NOMBRE, DB_VERSION); } catch (e) { reject(e); return; }
            req.onupgradeneeded = function (ev) {
                var db = ev.target.result;
                if (!db.objectStoreNames.contains(ALMACEN)) {
                    var st = db.createObjectStore(ALMACEN, { keyPath: 'id' });
                    st.createIndex('borrador', 'borrador', { unique: false });
                }
            };
            req.onsuccess = function (ev) {
                var db = ev.target.result;
                // Otra pestaña que necesite borrar o actualizar la base no debe
                // quedarse esperando por esta conexión.
                db.onversionchange = function () { db.close(); dbPromesa = null; };
                resolve(db);
            };
            req.onerror = function (ev) { reject(ev.target.error); };
            req.onblocked = function () { reject(new Error('IndexedDB bloqueada')); };
        });
        dbPromesa.catch(function () { dbPromesa = null; });
        return dbPromesa;
    }

    function enAlmacen(modo, trabajo) {
        return abrirDB().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(ALMACEN, modo);
                var resultado = { valor: undefined };
                trabajo(tx.objectStore(ALMACEN), resultado);
                tx.oncomplete = function () { resolve(resultado.valor); };
                tx.onerror = function (ev) { reject(ev.target.error); };
                tx.onabort = function (ev) { reject(ev.target.error || new Error('transacción abortada')); };
            });
        });
    }

    function guardarRegistroArchivos(registro) {
        return enAlmacen('readwrite', function (st) { st.put(registro); });
    }

    function borrarRegistroArchivos(id) {
        return enAlmacen('readwrite', function (st) { st.delete(id); });
    }

    function leerRegistrosArchivos(borrador) {
        return enAlmacen('readonly', function (st, resultado) {
            var req = st.index('borrador').getAll(borrador);
            req.onsuccess = function () { resultado.valor = req.result || []; };
        });
    }

    function borrarArchivosDeBorrador(borrador) {
        return enAlmacen('readwrite', function (st) {
            var req = st.index('borrador').openCursor(IDBKeyRange.only(borrador));
            req.onsuccess = function () {
                var cursor = req.result;
                if (!cursor) return;
                cursor.delete();
                cursor.continue();
            };
        });
    }

    // Sólo las claves, sin cargar las fotos en memoria.
    function listarBorradoresConArchivos() {
        return enAlmacen('readonly', function (st, resultado) {
            var vistos = {};
            resultado.valor = [];
            var req = st.index('borrador').openKeyCursor();
            req.onsuccess = function () {
                var cursor = req.result;
                if (!cursor) return;
                if (!vistos[cursor.key]) { vistos[cursor.key] = true; resultado.valor.push(cursor.key); }
                cursor.continue();
            };
        });
    }

    function huellaDeArchivos(archivos) {
        return Array.prototype.map.call(archivos || [], function (f) {
            return f.name + '|' + f.size + '|' + f.lastModified;
        }).join(';');
    }

    // Escribe en IndexedDB sólo las entradas cuyo contenido cambió desde la última
    // vez; se llama en cada guardado, así también se entera de las fotos que el
    // usuario quita (eso no dispara ningún evento en el input).
    function sincronizarArchivos(estado) {
        var nombres = {};
        entradasArchivo(estado.form).forEach(function (entrada) {
            var archivos = entrada.el.files;
            var huella = huellaDeArchivos(archivos);
            if (huella) nombres[entrada.clave] = Array.prototype.map.call(archivos, function (f) { return f.name; });
            if (estado.huellas[entrada.clave] === huella) return;
            estado.huellas[entrada.clave] = huella;
            var id = estado.clave + '|' + entrada.clave;
            var escritura;
            if (huella) {
                escritura = guardarRegistroArchivos({
                    id: id,
                    borrador: estado.clave,
                    campo: entrada.clave,
                    guardado: Date.now(),
                    archivos: Array.prototype.map.call(archivos, function (f) {
                        return { nombre: f.name, tipo: f.type, modificado: f.lastModified, contenido: f };
                    })
                });
            } else {
                escritura = borrarRegistroArchivos(id);
            }
            escritura.catch(function (e) {
                delete estado.huellas[entrada.clave];
                console.warn('[form-draft] No se pudo guardar el adjunto:', e);
            });
        });
        return nombres;
    }

    function reponerArchivos(estado) {
        return leerRegistrosArchivos(estado.clave).then(function (registros) {
            if (!registros.length) return;
            var porClave = {};
            entradasArchivo(estado.form).forEach(function (e) { porClave[e.clave] = e.el; });
            registros.forEach(function (registro) {
                var el = porClave[registro.campo];
                if (!el || !registro.archivos || !registro.archivos.length) return;
                var archivos = registro.archivos.map(function (a) {
                    return new File([a.contenido], a.nombre, { type: a.tipo, lastModified: a.modificado });
                });
                var dt;
                try {
                    dt = new DataTransfer();
                    archivos.forEach(function (f) { dt.items.add(f); });
                } catch (e) {
                    return; // el navegador no permite reponer archivos
                }
                estado.huellas[registro.campo] = huellaDeArchivos(archivos);
                // El acumulador de fotos múltiples recibe lo nuevo por su input de
                // galería; las tarjetas de las planillas y los adjuntos simples,
                // por el propio input. Su evento change redibuja el widget.
                var destino = el;
                var acumulador = el.classList.contains('input-real-files') && el.closest
                    ? el.closest('.secapp-photo-uploader') : null;
                if (acumulador) destino = acumulador.querySelector('.input-gallery-hidden') || el;
                estado.restaurando = true;
                try {
                    destino.files = dt.files;
                    disparar(destino, ['change']);
                } finally {
                    estado.restaurando = false;
                }
            });
        }).catch(function (e) {
            console.warn('[form-draft] No se pudieron reponer los adjuntos:', e);
        });
    }

    // ── Borrador en localStorage ─────────────────────────────────────────────
    function leerBorrador(clave) {
        try {
            var crudo = localStorage.getItem(clave);
            if (!crudo) return null;
            var b = JSON.parse(crudo);
            if (!b || b.formato !== FORMATO) return null;
            if (Date.now() - (b.guardado || 0) > VIGENCIA_MS) { localStorage.removeItem(clave); return null; }
            return b;
        } catch (e) {
            return null;
        }
    }

    function escribirBorrador(clave, borrador) {
        try {
            localStorage.setItem(clave, JSON.stringify(borrador));
        } catch (e) {
            // Sin cupo: mejor conservar los campos que nada.
            try {
                borrador.firmas = {};
                localStorage.setItem(clave, JSON.stringify(borrador));
            } catch (e2) { /* sin almacenamiento */ }
        }
    }

    function guardarAhora(estado) {
        if (estado.temporizador) { clearTimeout(estado.temporizador); estado.temporizador = null; }
        if (!estado.sucio || estado.enviado || estado.restaurando) return;
        var form = estado.form;
        var campos = {};
        controlesDe(form).forEach(function (c) {
            if (TIPOS_IGNORADOS[c.tipo]) return;
            campos[c.clave] = leerValor(c.el, c.tipo);
        });
        var borrador = {
            formato: FORMATO,
            guardado: Date.now(),
            ultimoCampo: claveDeElemento(estado, estado.ultimoEl),
            campos: campos,
            firmas: capturarFirmas(form),
            bloques: capturarBloques(form),
            archivos: sincronizarArchivos(estado)
        };
        escribirBorrador(estado.clave, borrador);
    }

    function programar(estado) {
        if (estado.temporizador) clearTimeout(estado.temporizador);
        estado.temporizador = setTimeout(function () { guardarAhora(estado); }, ESPERA_GUARDADO_MS);
    }

    function vaciarPendientes() {
        formularios.forEach(function (estado) {
            if (estado.temporizador) guardarAhora(estado);
        });
    }

    function borrarBorrador(estado) {
        try { localStorage.removeItem(estado.clave); } catch (e) { /* nada que borrar */ }
        estado.huellas = {};
        borrarArchivosDeBorrador(estado.clave).catch(function () { /* sin IndexedDB */ });
    }

    function purgarVencidos() {
        var vivos = {};
        var borrar = [];
        try {
            for (var i = 0; i < localStorage.length; i++) {
                var k = localStorage.key(i);
                if (!k || k.indexOf(PREFIJO) !== 0) continue;
                if (leerBorrador(k)) vivos[k] = true; else borrar.push(k);
            }
            borrar.forEach(function (k) { localStorage.removeItem(k); });
        } catch (e) { /* sin localStorage */ }
        listarBorradoresConArchivos().then(function (claves) {
            claves.forEach(function (clave) {
                if (!vivos[clave]) borrarArchivosDeBorrador(clave).catch(function () {});
            });
        }).catch(function () { /* sin IndexedDB */ });
    }

    // ── Restauración ─────────────────────────────────────────────────────────
    function aplicarCampos(estado, campos) {
        var pendientes = {};
        var controles = controlesDe(estado.form);
        var vistos = {};
        controles.forEach(function (c) {
            vistos[c.clave] = true;
            if (!(c.clave in campos) || TIPOS_IGNORADOS[c.tipo]) return;
            var ok = false;
            try { ok = aplicarValor(c.el, c.tipo, campos[c.clave]); } catch (e) { ok = true; }
            if (!ok) pendientes[c.clave] = campos[c.clave];
        });
        // Controles que todavía no existen (los construye un script después de cargar).
        Object.keys(campos).forEach(function (clave) {
            if (!vistos[clave]) pendientes[clave] = campos[clave];
        });
        return pendientes;
    }

    // Los combos de cliente / instalación, puesto y placa rehacen sus opciones
    // cuando llega la lista y pueden pisar lo ya repuesto, y el select de
    // cliente ni siquiera existe al cargar. Se vuelve a aplicar lo pendiente
    // cada vez que cambia el formulario, hasta que todo quede o pase el plazo;
    // un control que el usuario ya tocó deja de vigilarse.
    function vigilarPendientes(estado, pendientes, campos) {
        var vigilados = {};
        Object.keys(pendientes).forEach(function (k) { vigilados[k] = pendientes[k]; });
        Object.keys(campos).forEach(function (k) {
            if (k.indexOf('n:') === 0 && typeof campos[k] === 'string' && campos[k] !== '') {
                var el = elementoDeClave(estado, k);
                if (el && el.tagName === 'SELECT') vigilados[k] = campos[k];
            }
        });
        if (!Object.keys(vigilados).length || typeof MutationObserver !== 'function') return;

        estado.vigilados = vigilados;
        var limite = Date.now() + ESPERA_REINTENTOS_MS;
        var temporizador = null;
        var observador = null;

        function terminar() {
            if (observador) observador.disconnect();
            if (temporizador) clearTimeout(temporizador);
            estado.vigilados = null;
        }

        function reintentar() {
            temporizador = null;
            if (!estado.vigilados || Date.now() > limite) { terminar(); return; }
            var claves = Object.keys(estado.vigilados);
            if (!claves.length) { terminar(); return; }
            estado.restaurando = true;
            try {
                claves.forEach(function (clave) {
                    var el = elementoDeClave(estado, clave);
                    if (!el) return;
                    var valor = estado.vigilados[clave];
                    var tipo = (el.type || '').toLowerCase();
                    if (el.tagName === 'SELECT') {
                        var actual = leerValor(el, tipo);
                        var igual = Array.isArray(valor) ? JSON.stringify(actual) === JSON.stringify(valor) : actual === String(valor);
                        if (igual) return;
                        // Vacío es un rehacer de opciones y se repone; otro valor
                        // después de que el usuario tocó el formulario es una
                        // elección suya y no se pisa.
                        if (actual !== '' && estado.interaccion) { delete estado.vigilados[clave]; return; }
                        try { aplicarValor(el, tipo, valor); } catch (e) { /* se reintenta */ }
                        return;
                    }
                    try { aplicarValor(el, tipo, valor); } catch (e) { /* se descarta */ }
                    delete estado.vigilados[clave];
                });
            } finally {
                estado.restaurando = false;
            }
        }

        function programarReintento(espera) {
            if (temporizador) clearTimeout(temporizador);
            temporizador = setTimeout(reintentar, espera);
        }

        observador = new MutationObserver(function () { programarReintento(60); });
        observador.observe(estado.form, { childList: true, subtree: true });
        programarReintento(1500);
    }

    function dejarDeVigilar(estado, el) {
        if (!estado.vigilados) return;
        var clave = claveDeElemento(estado, el);
        if (clave && estado.vigilados[clave] !== undefined) delete estado.vigilados[clave];
    }

    function anclaVisible(el) {
        for (var n = el; n && n !== document.body; n = n.parentElement) {
            if (n.getClientRects && n.getClientRects().length) return n;
        }
        return el;
    }

    function llevarAlUltimoCampo(estado, clave) {
        var el = elementoDeClave(estado, clave);
        if (!el) return;
        var ancla = anclaVisible(el);
        function ir() {
            try { ancla.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
            catch (e) { try { ancla.scrollIntoView(true); } catch (e2) { /* sin scroll */ } }
            ancla.classList.add('secapp-borrador-foco');
            setTimeout(function () { ancla.classList.remove('secapp-borrador-foco'); }, 2500);
        }
        setTimeout(ir, 150);
        // La página puede seguir acomodándose mientras cargan las fuentes y
        // estilos: si el usuario no ha tocado nada, se vuelve a centrar al final.
        if (document.readyState !== 'complete') {
            window.addEventListener('load', function () {
                if (!estado.interaccion) setTimeout(ir, 100);
            });
        }
    }

    function restaurar(estado) {
        var borrador = leerBorrador(estado.clave);
        if (!borrador) return;
        var pendientes = {};
        estado.restaurando = true;
        try {
            try { reconstruirBloques(estado.form, borrador.bloques); }
            catch (e) { console.warn('[form-draft] No se pudieron rehacer los bloques:', e); }
            try { pendientes = aplicarCampos(estado, borrador.campos || {}); }
            catch (e) { console.warn('[form-draft] No se pudieron reponer los campos:', e); }
            try { reponerFirmas(estado.form, borrador.firmas); }
            catch (e) { console.warn('[form-draft] No se pudieron reponer las firmas:', e); }
        } finally {
            estado.restaurando = false;
        }
        // Lo que sigue (sincronizaciones de los combos, fotos) debe guardarse.
        estado.sucio = true;
        try { vigilarPendientes(estado, pendientes, borrador.campos || {}); }
        catch (e) { console.warn('[form-draft] No se pudieron vigilar los combos:', e); }
        reponerArchivos(estado).then(function () {
            llevarAlUltimoCampo(estado, borrador.ultimoCampo);
        });
        avisar(estado, borrador);
    }

    // ── Aviso ────────────────────────────────────────────────────────────────
    var ESTILOS = [
        '.secapp-borrador-aviso{position:fixed;bottom:1.25rem;left:50%;transform:translateX(-50%);',
        '  z-index:10060;display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;justify-content:center;',
        '  width:max-content;max-width:min(440px,92vw);padding:.65rem 1rem;border-radius:10px;',
        '  background:#1e3a5f;color:#e2e8f0;border:1px solid rgba(96,165,250,.4);',
        '  box-shadow:0 8px 32px rgba(0,0,0,.45);font-family:Roboto,sans-serif;font-size:.85rem;',
        '  transition:opacity .4s ease;}',
        '.secapp-borrador-aviso button{background:none;border:1px solid rgba(255,255,255,.3);color:#fff;',
        '  border-radius:6px;padding:.3rem .7rem;font-size:.8rem;font-weight:700;cursor:pointer;}',
        '.secapp-borrador-foco{outline:2px solid #60a5fa !important;outline-offset:3px;border-radius:6px;}'
    ].join('\n');

    function inyectarEstilos() {
        if (document.getElementById('secapp-form-draft-styles')) return;
        var s = document.createElement('style');
        s.id = 'secapp-form-draft-styles';
        s.textContent = ESTILOS;
        (document.head || document.documentElement).appendChild(s);
    }

    function hace(ts) {
        var min = Math.round((Date.now() - ts) / 60000);
        if (min < 1) return 'hace un momento';
        if (min < 60) return 'hace ' + min + ' min';
        var h = Math.round(min / 60);
        if (h < 24) return 'hace ' + h + ' h';
        var d = Math.round(h / 24);
        return 'hace ' + d + (d === 1 ? ' día' : ' días');
    }

    function avisar(estado, borrador) {
        inyectarEstilos();
        var aviso = document.createElement('div');
        aviso.className = 'secapp-borrador-aviso';
        aviso.setAttribute('role', 'status');
        aviso.setAttribute('aria-live', 'polite');
        var texto = document.createElement('span');
        texto.textContent = '✓ Datos recuperados · ' + hace(borrador.guardado || Date.now());
        var descartar = document.createElement('button');
        descartar.type = 'button';
        descartar.textContent = 'Empezar de cero';
        aviso.appendChild(texto);
        aviso.appendChild(descartar);
        document.body.appendChild(aviso);

        var cerrar = setTimeout(function () {
            aviso.style.opacity = '0';
            setTimeout(function () { aviso.remove(); }, 450);
        }, 10000);

        descartar.addEventListener('click', function () {
            if (!window.confirm('¿Descartar los datos recuperados y empezar este formulario de cero?')) return;
            clearTimeout(cerrar);
            estado.enviado = true; // que ningún guardado tardío lo reviva antes de recargar
            estado.sucio = false;
            borrarBorrador(estado);
            if (estado.form.id === 'capacitacionForm') {
                try { sessionStorage.removeItem(CLAVE_SESION_CAPACITACION); } catch (e) { /* nada */ }
            }
            setTimeout(function () { location.reload(); }, 150);
        });
    }

    // ── Eventos ──────────────────────────────────────────────────────────────
    // Sólo una acción real del usuario arma el borrador: los change que emiten
    // los scripts al inicializar (cliente único, enlaces profundos) no cuentan
    // hasta que el usuario toca algo. Una foto cuenta aunque el evento lo
    // dispare el widget de cámara, y se guarda en el acto porque pesa.
    function alEditar(ev) {
        var el = ev.target;
        var estado = estadoDeElemento(el);
        if (!estado || estado.restaurando) return;
        var esArchivo = el.tagName === 'INPUT' && el.type === 'file';
        if (ev.isTrusted || esArchivo) {
            estado.sucio = true;
            estado.enviado = false;
            estado.interaccion = true;
            estado.ultimoEl = el;
            if (ev.isTrusted) dejarDeVigilar(estado, el);
        }
        if (!estado.sucio) return;
        if (esArchivo && ev.type === 'change') {
            // Un tick después: el acumulador de fotos copia lo elegido a su input
            // real en su propio handler, que corre después de esta escucha.
            setTimeout(function () { guardarAhora(estado); }, 0);
            return;
        }
        programar(estado);
    }

    // Clics y teclas: no cambian valores por sí solos, pero marcan que el usuario
    // ya está actuando (un bloque añadido, una foto quitada, una opción elegida
    // en el panel de un combobox) y sueltan la vigilancia de ese combo.
    function alPulsar(ev) {
        if (!ev.isTrusted) return;
        var estado = estadoDeElemento(ev.target);
        if (!estado) return;
        estado.interaccion = true;
        dejarDeVigilar(estado, ev.target);
        if (estado.sucio && !estado.restaurando) programar(estado);
    }

    function alEnviar(ev) {
        var form = ev.target;
        var estado = form && form.tagName === 'FORM' ? estadoDe(form) : null;
        if (!estado) return;
        var valido = true;
        try { valido = typeof form.checkValidity !== 'function' || form.checkValidity(); } catch (e) { /* se asume válido */ }
        // El handler propio del formulario corre antes (fase target) y, si decidió
        // no enviar, dejó _secappAbort para el candado; aquí se lee antes de que
        // el candado lo consuma porque este script se carga primero.
        if (!valido || form._secappAbort) return;
        if (estado.temporizador) { clearTimeout(estado.temporizador); estado.temporizador = null; }
        estado.enviado = true;
        estado.sucio = false;
        borrarBorrador(estado);
    }

    function alReiniciar(ev) {
        var estado = ev.target && ev.target.tagName === 'FORM' ? estadoDe(ev.target) : null;
        if (!estado) return;
        if (estado.temporizador) { clearTimeout(estado.temporizador); estado.temporizador = null; }
        estado.sucio = false;
        borrarBorrador(estado);
    }

    // ── Inicio ───────────────────────────────────────────────────────────────
    instalarRegistroFirmas();

    // Capacitaciones ata los asistentes por QR a un token que lee de
    // sessionStorage al arrancar. Si el borrador lo trae, se repone antes de que
    // la plantilla lo genere de nuevo, y los asistentes ya registrados siguen ahí.
    (function reponerSesionCapacitacion() {
        if (!/registro_de_capacitaciones/.test(location.pathname)) return;
        var borrador = leerBorrador(claveDe('capacitacionForm'));
        var token = borrador && borrador.campos && borrador.campos['n:session_token#1'];
        if (token) {
            try { sessionStorage.setItem(CLAVE_SESION_CAPACITACION, String(token)); } catch (e) { /* nada */ }
        }
    })();

    document.addEventListener('input', alEditar, true);
    document.addEventListener('change', alEditar, true);
    document.addEventListener('click', alPulsar, true);
    document.addEventListener('keydown', alPulsar, true);
    document.addEventListener('submit', alEnviar, false);
    document.addEventListener('reset', alReiniciar, false);

    function iniciar() {
        purgarVencidos();
        Array.prototype.forEach.call(document.querySelectorAll('form'), function (form) {
            if (!esFormularioDeCampo(form)) return;
            formularios.push({
                form: form,
                clave: claveDe(form.id),
                sucio: false,
                enviado: false,
                restaurando: false,
                interaccion: false,
                huellas: {},
                temporizador: null,
                ultimoEl: null,
                vigilados: null
            });
        });
        if (!formularios.length) return;

        // Después de todos los DOMContentLoaded: las plantillas ya crearon su
        // primer bloque, sus pads y sus valores por defecto.
        setTimeout(function () { formularios.forEach(restaurar); }, 0);

        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'hidden') vaciarPendientes();
        });
        window.addEventListener('pagehide', vaciarPendientes);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', iniciar);
    } else {
        iniciar();
    }

    // Para diagnóstico desde la consola.
    window.SecAppFormDraft = {
        clave: function (form) { var e = estadoDe(form); return e ? e.clave : null; },
        guardar: function (form) { var e = estadoDe(form); if (e) { e.sucio = true; guardarAhora(e); } },
        borrar: function (form) { var e = estadoDe(form); if (e) borrarBorrador(e); }
    };
})();
