/**
 * foto-comprimir.js — reduce las fotos en el celular antes de que entren al
 * formulario (Roberto, 2026-10-15, a raíz del Expediente por QR de KANAN).
 *
 * Las fotos salían del celular tal cual (3–8 MB, hasta 12 MP) y así se subían, se
 * guardaban en el borrador y en la cola sin conexión, y se mostraban en Reportes y
 * en el Expediente. Aquí se reducen a ANCHO_MAX px por el lado mayor y a JPEG de
 * calidad CALIDAD antes de que cualquier otro script las vea.
 *
 * Cómo: un listener de `change` en fase de captura sobre todo <input type="file">.
 * Si trae imágenes sin comprimir, detiene el evento, las comprime y vuelve a
 * disparar `change` con los archivos ya reducidos (marcados para no repetir). Los
 * PDF y demás se dejan tal cual; si el navegador no sabe decodificar la imagen
 * (HEIC en Android) o no soporta DataTransfer, se deja el original. El navegador
 * aplica la orientación EXIF al decodificar, así que la foto reducida sale derecha
 * y sin EXIF. Una foto que ya cabe (lado ≤ ANCHO_MAX y ≤ UMBRAL_BYTES) no se
 * recomprime: así un borrador restaurado no pierde calidad una segunda vez.
 *
 * Expone window.FotoComprimir.comprimir(file) → Promise<File> y
 * window.FotoComprimir.desdeCanvas(canvas, nombre) → Promise<File>, que usa
 * photo-capture.js para la foto tomada con la cámara de la app.
 */
(function () {
    'use strict';

    var ANCHO_MAX = 1600;           // lado mayor, en px
    var CALIDAD = 0.82;             // JPEG
    var UMBRAL_BYTES = 600 * 1024;  // dentro del tamaño y por debajo de esto, se deja tal cual
    var marcadas = (typeof WeakSet === 'function') ? new WeakSet() : null;

    function marcar(f) { if (marcadas && f && typeof f === 'object') marcadas.add(f); return f; }
    function yaVista(f) { return !!(marcadas && f && marcadas.has(f)); }

    function esImagen(file) {
        var t = ((file && file.type) || '').toLowerCase();
        return t.indexOf('image/') === 0 && t !== 'image/gif' && t !== 'image/svg+xml';
    }

    function nombreJpg(nombre) {
        return String(nombre || 'foto').replace(/\.[^.]+$/, '') + '.jpg';
    }

    function soporta() {
        try {
            return typeof DataTransfer !== 'undefined' && typeof File !== 'undefined'
                && typeof Promise !== 'undefined'
                && typeof HTMLCanvasElement.prototype.toBlob === 'function';
        } catch (e) { return false; }
    }

    function cargar(file) {
        return new Promise(function (resolve, reject) {
            var url = URL.createObjectURL(file);
            var img = new Image();
            img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
            img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('no decodifica')); };
            img.src = url;
        });
    }

    function aBlob(canvas) {
        return new Promise(function (resolve) { canvas.toBlob(resolve, 'image/jpeg', CALIDAD); });
    }

    // img o canvas → Blob JPEG con el lado mayor en ANCHO_MAX como máximo.
    function reducir(origen, w, h) {
        var escala = Math.min(1, ANCHO_MAX / Math.max(w, h));
        var canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(w * escala));
        canvas.height = Math.max(1, Math.round(h * escala));
        canvas.getContext('2d').drawImage(origen, 0, 0, canvas.width, canvas.height);
        return aBlob(canvas);
    }

    /** File → Promise<File>: el mismo si no hace falta o no se puede comprimir. */
    function comprimir(file) {
        if (!file || yaVista(file) || !esImagen(file) || !soporta()) return Promise.resolve(marcar(file));
        return cargar(file).then(function (img) {
            var w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
            if (!w || !h) return file;
            if (Math.max(w, h) <= ANCHO_MAX && file.size <= UMBRAL_BYTES) return file;
            return reducir(img, w, h).then(function (blob) {
                if (!blob || blob.size >= file.size) return file;
                return new File([blob], nombreJpg(file.name),
                                { type: 'image/jpeg', lastModified: file.lastModified || Date.now() });
            });
        }).catch(function () { return file; }).then(marcar);
    }

    /** Canvas con la foto de la cámara de la app → Promise<File> JPEG reducido. */
    function desdeCanvas(canvas, nombre) {
        var w = canvas.width, h = canvas.height;
        var p = (Math.max(w, h) > ANCHO_MAX) ? reducir(canvas, w, h) : aBlob(canvas);
        return p.then(function (blob) {
            if (!blob) throw new Error('toBlob devolvió null');
            return marcar(new File([blob], nombreJpg(nombre), { type: 'image/jpeg', lastModified: Date.now() }));
        });
    }

    // Gancho global. En fase de captura sobre document corre antes que cualquier
    // listener del input (los del uploader, photo-capture, form-draft y los
    // onchange en línea): ninguno llega a ver los originales.
    document.addEventListener('change', function (ev) {
        var input = ev.target;
        if (!input || input.tagName !== 'INPUT' || input.type !== 'file') return;
        var files = input.files;
        if (!files || !files.length || !soporta()) return;
        var pendientes = false;
        for (var i = 0; i < files.length; i++) {
            if (esImagen(files[i]) && !yaVista(files[i])) { pendientes = true; break; }
        }
        if (!pendientes) return;
        ev.stopImmediatePropagation();
        var lista = Array.prototype.slice.call(files);
        Promise.all(lista.map(comprimir)).then(function (nuevos) {
            try {
                var dt = new DataTransfer();
                nuevos.forEach(function (f) { dt.items.add(f); });
                input.files = dt.files;
            } catch (e) {
                console.warn('[foto-comprimir] DataTransfer no disponible, siguen los originales:', e);
            }
            input.dispatchEvent(new Event('change', { bubbles: true }));
        });
    }, true);

    window.FotoComprimir = { comprimir: comprimir, desdeCanvas: desdeCanvas, ANCHO_MAX: ANCHO_MAX, CALIDAD: CALIDAD };
})();
