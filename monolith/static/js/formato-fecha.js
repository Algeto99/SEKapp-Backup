/**
 * formato-fecha.js — un solo formato de fecha en pantalla (KANAN, 2026-10-14).
 *
 * DD/MM/AAAA y, con hora, "DD/MM/AAAA hh:mm a. m." / "p. m.": el mismo texto que
 * produce `format_local_datetime` en el servidor para Reportes, el PDF y el Excel.
 * Sustituye los toLocaleDateString('es' | 'es-CO' | 'es-GT') y los '5 oct 2026'
 * que cada pantalla armaba por su cuenta.
 *
 * Acepta ISO 'AAAA-MM-DD', 'AAAA-MM-DDTHH:MM[:SS]' o 'AAAA-MM-DD HH:MM[:SS]', un
 * Date o un epoch en milisegundos. Un ISO SIN zona es reloj de pared (la hora que
 * se escribió en el formulario) y se lee tal cual, sin pasar por Date, que lo
 * movería a la zona del dispositivo; uno CON zona (instantes como `generado_en`)
 * se convierte a la hora local del dispositivo, como hacían los toLocale*.
 * Lo que no se reconoce vuelve tal cual, para no esconder datos.
 *
 * Los <input type="date"> nativos los pinta el navegador en su propio idioma y no
 * pasan por aquí.
 */
(function () {
    'use strict';

    var ISO = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/;
    var CON_ZONA = /(?:Z|[+-]\d{2}:?\d{2})$/i;

    function pad(n) { return (n < 10 ? '0' : '') + n; }

    function desdeDate(d) {
        if (isNaN(d.getTime())) return null;
        return { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate(),
                 h: d.getHours(), mi: d.getMinutes(), conHora: true };
    }

    // {y, m, d, h, mi, conHora} o null si no se reconoce.
    function partes(valor) {
        if (valor === null || valor === undefined || valor === '') return null;
        if (valor instanceof Date) return desdeDate(valor);
        if (typeof valor === 'number') return desdeDate(new Date(valor));
        var s = String(valor).trim();
        var m = ISO.exec(s);
        if (m && !CON_ZONA.test(s)) {
            return { y: +m[1], m: +m[2], d: +m[3],
                     h: m[4] ? +m[4] : 0, mi: m[5] ? +m[5] : 0, conHora: !!m[4] };
        }
        var t = new Date(s);
        return isNaN(t.getTime()) ? null : desdeDate(t);
    }

    function textoOriginal(valor) {
        return (valor === null || valor === undefined) ? '' : String(valor).trim();
    }

    /** '2026-09-25T18:19' → '25/09/2026' */
    function fecha(valor) {
        var p = partes(valor);
        return p ? pad(p.d) + '/' + pad(p.m) + '/' + p.y : textoOriginal(valor);
    }

    /** '2026-09-25T18:19' → '06:19 p. m.'; sin hora → '' */
    function hora(valor) {
        var p = partes(valor);
        if (!p || !p.conHora) return '';
        var h12 = p.h % 12 || 12;
        return pad(h12) + ':' + pad(p.mi) + ' ' + (p.h < 12 ? 'a. m.' : 'p. m.');
    }

    /** '2026-09-25T18:19' → '25/09/2026 06:19 p. m.'; '2026-09-25' → '25/09/2026' */
    function fechaHora(valor) {
        var p = partes(valor);
        if (!p) return textoOriginal(valor);
        var f = fecha(valor);
        var h = hora(valor);
        return h ? f + ' ' + h : f;
    }

    window.FormatoFecha = { fecha: fecha, hora: hora, fechaHora: fechaHora };
})();
