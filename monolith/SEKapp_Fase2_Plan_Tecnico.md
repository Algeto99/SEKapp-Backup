# SEKapp — Fase 2: Plan Técnico de Implementación

Traducción del brief de Álvaro a cambios concretos de código, archivo por archivo.
Orden de ejecución estricto: Tarea 1 → 2 → 3 → 4. Cada una se prueba antes de la siguiente.

> **Antes de empezar — 3 supuestos del brief que NO se cumplen tal cual en el código.**
> Léelos primero; cambian el alcance real de las Tareas 1 y 2.
>
> 1. **El modal "Ver Detalles" es COMPARTIDO.** No es exclusivo de Reportes. `static/js/dashboard-record-viewer.js` lo usan ~12 dashboards (gestión, incidentes, supervisión, visitas, etc.) **y** el Morning Briefing. Reorganizarlo afecta a todos. Hay que hacerlo sin romper los demás (ver Tarea 1).
> 2. **NO existe un "sistema de notificaciones internas".** El brief (Tarea 2) asume que "ya existe". En el código solo hay correo (`email_utils.py`) y la mención de push del PWA (`install_prompt.html`). No hay tabla de notificaciones ni campanita. Hay que decidir: correo (rápido, ya existe) o construir notificación in-app real (más trabajo). Ver Tarea 2.
> 3. **El filtro "Cliente" que ya existe ES el filtro por instalación.** En este esquema `propiedades.id_propiedad` = la instalación. El selector `filtCliente` de Operación ya filtra por `id_propiedad`. Lo de "instalación" en Tarea 3 es en gran parte renombrar/duplicar ese filtro. El filtro por **Supervisor** sí es nuevo y es ambiguo entre tablas (ver Tarea 3).

---

## Mapa de archivos relevantes

| Pieza | Archivo | Detalle |
| --- | --- | --- |
| Modal "Ver Detalles" | `static/js/dashboard-record-viewer.js` | `createDashboardRecordViewer()`, `renderRecordDetail()` (línea ~600), `ensureModal()` (~389) |
| Endpoint del registro | `viewer_bp.py` | `GET /viewer/api/report/<id>` (1369) → `fetch_reports_by_ids()` (831). Forma del dato y `FORM_CONFIGS` (149) |
| Morning Briefing | `templates/cgeo_morning_briefing.html` | alertas → `openAlertRecord()` (722), `_getAlertViewer()` (706), `_alertaItemHtml()` (779) |
| Tendencia + filtros | `templates/cgeo_operacion.html` | chart `chartTendencia` (511), `loadData()` (1297), `loadFiltros()` (1322), `filtCliente` (421) |
| Backend tendencia/filtros | `cgeo_bp.py` | `cgeo_api_operacion_data()` (1135), `cgeo_api_filtros()` (230), `_add_cliente()` (113) |
| Acta de Visita | `templates/acta_visita_cliente.html` | `id_propiedad` (179), `motivo_visita` (190), `temas_tratados_0` (208) |
| Ruta del Acta | `forms_bp.py` | `registro_y_acta_de_visita_form()` (993) — hoy se renderiza sin contexto |
| Tabla de usuarios | `users` (PostgreSQL) | columnas: `id, name, email, is_admin, is_super_admin, company_id` (ver `admin_bp.py` 80) |
| Incidentes (estado/asignación) | tabla `reportes_incidentes` | ya tiene columnas `responsable_asignado` y `estado` (ver `FORM_CONFIGS` 178) |

---

## Tarea 1 — Vista de Evento: reorganizar el modal en 3 zonas

**Objetivo.** Que el modal muestre arriba un resumen de 5 preguntas (QUÉ / CUÁNDO / DÓNDE / CÓMO / QUIÉN), debajo el detalle técnico colapsado, y al pie dos botones de acción.

**Dónde se toca.** `static/js/dashboard-record-viewer.js`, función `renderRecordDetail(d, currentRecordId)` (línea ~600). Hoy vuelca todos los campos en una sola grilla plana ("Datos del Formulario").

**El reto real.** El modal es compartido y cada `form_type` trae etiquetas distintas (ver `FORM_CONFIGS` en `viewer_bp.py`). Por eso el mapeo de las 5 preguntas debe ser **por tipo de formulario**, no genérico.

### Paso 1.1 — Mapa de 5 preguntas por form_type

En `dashboard-record-viewer.js`, agregar una constante que diga, para cada `form_type`, qué etiqueta de `data` va a cada pregunta. Las etiquetas salen de `data_mapping` en `FORM_CONFIGS`.

```js
// Las claves son las ETIQUETAS que produce fetch_reports_by_ids (data_mapping de viewer_bp.py)
const FIVE_Q_MAP = {
  reporte_incidente: {
    QUE:    ['Título de Incidencia', 'Categoría'],
    CUANDO: ['Fecha del Incidente'],            // + d.dateSubmitted como respaldo
    DONDE:  ['Propiedad', 'Lugar del Incidente'],
    COMO:   ['Nivel Severidad', 'Descripción del Incidente', 'URLs de Imágenes o PDFs'],
    QUIEN:  ['Nombre del Supervisor', 'Responsable Asignado'],
  },
  supervision_puesto: {
    QUE:    ['Puesto/Área'],
    CUANDO: ['Fecha/Hora'],
    DONDE:  ['Cliente/Instalación'],
    COMO:   ['Observaciones', 'Foto Evidencia'],
    QUIEN:  ['Supervisor', 'Nombre Guardia'],
  },
  checklist_cumplimiento:        { /* … */ },
  medicion_experiencia_cliente:  { /* … */ },
  // fallback genérico abajo
};
```

El `form_type` ya está disponible: `createDashboardRecordViewer({formType})` lo guarda en `cfg.formType`. Hay que pasarlo a `renderRecordDetail` (hoy no lo recibe) o leerlo del closure.

### Paso 1.2 — Reescribir `renderRecordDetail` en 3 zonas

Estructura nueva (reutiliza los estilos `drv-detail-section` que ya existen):

```js
function renderRecordDetail(d, currentRecordId, formType) {
  const raw = d.data || d;
  const qmap = FIVE_Q_MAP[formType] || null;

  // ── ZONA 1: las 5 preguntas (destacada, sin scroll) ──
  const zona1 = qmap ? render5Questions(d, raw, qmap) : '';

  // ── ZONA 2: detalle técnico completo, COLAPSADO ──
  const usadas = qmap ? new Set(Object.values(qmap).flat()) : new Set();
  const rows = Object.entries(raw)
    .filter(([k]) => !usadas.has(k))   // no repetir lo ya mostrado arriba
    .map(([k, v]) => /* … mismo render actual … */).join('');
  const zona2 = `
    <details class="drv-detail-section" ${qmap ? '' : 'open'}>
      <summary><h4 style="display:inline">Detalle técnico completo</h4></summary>
      <div class="drv-detail-grid">${rows}</div>
    </details>`;

  return zona1 + zona2;
}
```

`render5Questions` arma 5 tarjetas con icono/etiqueta fija (QUÉ, CUÁNDO, DÓNDE, CÓMO, QUIÉN) y dentro pinta los valores que correspondan, reutilizando `renderValue()` (que ya maneja imágenes, firmas y geolocalización).

- **CUÁNDO** usa el campo de fecha del formulario y, si falta, `d.dateSubmitted`.
- **DÓNDE** con pin de mapa: `renderValue` ya detecta imágenes; para el pin existe `_map_thumbnail_html` en `viewer_bp.py` (1909) — si quieres mapa real, el backend ya lo sabe generar.
- Si `form_type` no está en `FIVE_Q_MAP`, no se rompe: se omite Zona 1 y Zona 2 sale abierta (comportamiento actual). Así los 12 dashboards que no son de incidentes siguen funcionando igual hasta que les agregues su mapa.

### Paso 1.3 — Zona 3: botones de acción

En `ensureModal()` (~389), el `.drv-action-bar` ya existe con botones PDF/Excel/Correo. Agregar dos botones:

```html
<button class="drv-modal-btn drv-btn-asignar" type="button">Asignar hallazgo</button>
<button class="drv-modal-btn drv-btn-visita"  type="button">Agendar visita</button>
```

Cablearlos en el bloque de `onclick` (~792). `drv-btn-asignar` → Tarea 2; `drv-btn-visita` → Tarea 4. Pásales `currentRecordId` y `cfg.formType` (ya están en el closure).

> **Decisión de alcance:** ¿los botones aparecen en TODOS los form_type o solo en incidentes? Recomiendo mostrarlos solo cuando `cfg.formType === 'reporte_incidente'` (o los tipos que defina Álvaro), para no ensuciar los otros dashboards.

### Paso 1.4 — Que abra sin scroll desde el Briefing

Ya funciona: `openAlertRecord()` (briefing, 722) → `openRecord()` → `renderRecordDetail()`. Con Zona 1 arriba y Zona 2 colapsada, las 5 preguntas quedan visibles sin scroll. No se toca el flujo de apertura.

**Prueba (criterio del brief):** tocar una alerta del Briefing abre el modal con las 5 preguntas arriba sin scroll, Zona 2 colapsada, y los dos botones visibles. Verificar además que abrir un registro desde dashboard_incidentes / dashboard_gestion sigue mostrando todo (Zona 2) sin errores.

---

## Tarea 2 — Botón "Asignar hallazgo"

**Objetivo.** Desde la Vista de Evento, asignar el registro a un Supervisor con fecha límite y nota; marcarlo "Asignado" y notificar.

**Lo que YA existe y ayuda:**
- Tabla `users` con los usuarios (`admin_bp.py` 80) → fuente del selector de responsable.
- `reportes_incidentes` ya tiene columnas `responsable_asignado` y `estado` (no hay que crear columnas para el caso de incidentes).
- Correo funcionando: `send_reports_email()` en `viewer_bp.py` (1045) y `email_utils.py`.

**Lo que NO existe (corrige el supuesto del brief):**
- No hay endpoint para asignar. Hay que crearlo.
- No hay "sistema de notificaciones internas". Solo correo. → **decisión de producto necesaria** (ver abajo).

### Paso 2.1 — Endpoint de usuarios asignables

En `cgeo_bp.py` (o `viewer_bp.py`), nuevo endpoint:

```python
@cgeo_bp.route("/api/usuarios-asignables")
@jwt_required()
def usuarios_asignables():
    # filtrar por company_id del usuario actual (multi-tenant, igual que cgeo_api_filtros)
    # SELECT id, name, email FROM users WHERE company_id = %s AND is_active = TRUE ORDER BY name
```

Reutiliza `_get_user_company_id()` que ya está en `cgeo_bp.py` (38).

### Paso 2.2 — Tabla/columnas de asignación

Para incidentes basta con las columnas existentes. Para trazabilidad completa (fecha límite + nota + historial) recomiendo una tabla nueva, genérica para cualquier form_type:

```sql
CREATE TABLE IF NOT EXISTS asignaciones_hallazgo (
  id            SERIAL PRIMARY KEY,
  form_type     TEXT NOT NULL,
  record_id     INTEGER NOT NULL,
  asignado_a    INTEGER REFERENCES users(id),
  asignado_por  TEXT,
  fecha_limite  DATE,
  nota          TEXT,
  estado        TEXT DEFAULT 'Asignado',
  company_id    INTEGER,
  creado_en     TIMESTAMP DEFAULT NOW()
);
```

(Sigue el patrón de `kpi_thresholds` que ya se crea con `CREATE TABLE IF NOT EXISTS` en `admin_bp.py` 331.)

Si solo es incidentes y Álvaro quiere lo mínimo: `UPDATE reportes_incidentes SET responsable_asignado=%s, estado='Asignado' WHERE id_reporte_incidente=%s` y listo. La tabla extra es la opción trazable.

### Paso 2.3 — Endpoint para confirmar asignación

```python
@cgeo_bp.route("/api/asignar-hallazgo", methods=['POST'])
@jwt_required()
def asignar_hallazgo():
    # body: { form_type, record_id, asignado_a, fecha_limite, nota }
    # 1. INSERT en asignaciones_hallazgo  (o UPDATE incidente)
    # 2. notificar al responsable  → ver decisión 2.5
    # 3. return {success, estado:'Asignado', responsable: nombre}
```
Usar `X-CSRF-TOKEN` como ya hacen los otros POST del viewer (`getCsrfToken()` en el JS).

### Paso 2.4 — UI: segundo modal

Disparado por `drv-btn-asignar` (Tarea 1.3). Modal pequeño con: select de responsable (carga de 2.1), `input[type=date]` fecha límite, `textarea` nota, botón Confirmar. Reutiliza el patrón de overlay que ya está en el archivo (`.drv-email-overlay`, ~412). Al confirmar, POST a 2.3 y toast de éxito (`showToast()` ya existe).

### Paso 2.5 — DECISIÓN: cómo se "notifica"

Tres opciones, de menor a mayor esfuerzo:

| Opción | Esfuerzo | Qué implica |
| --- | --- | --- |
| **A. Correo** (recomendada para cerrar Fase 2) | Bajo | Reusar `send_reports_email()`. El responsable recibe un mail "Se te asignó un hallazgo". Ya existe toda la infraestructura. |
| **B. Notificación in-app real** | Alto | Tabla `notificaciones`, endpoint de "no leídas", campanita en el header (`_header_nav.html`), polling. No existe nada de esto hoy. |
| **C. Push (PWA)** | Medio-alto | Hay `sw.js` y `install_prompt.html` menciona push, pero no hay backend de Web Push configurado. |

> El brief dice "recibe notificación en la app". Eso es la Opción B, que **no existe** y es un proyecto en sí. Recomiendo cerrar Fase 2 con **A (correo)** + reflejar el estado "Asignado" en el Briefing (que sí es visible), y dejar B como Fase 3. **Confirmar con Álvaro.**

### Paso 2.6 — Reflejar "Asignado" en el Briefing

El estado ya viaja en las alertas (`reportes_incidentes.estado`). Si la asignación setea `estado='Asignado'`, la próxima carga del Briefing lo muestra. Verificar que la query de alertas (`cgeo_api_alertas`, 556) no excluya los "Asignado" si quieres que sigan visibles, o ajustarla para mostrar el badge.

**Prueba:** asignar un hallazgo a un supervisor con fecha límite → el registro muestra "Asignado" + nombre → el Briefing refleja el cambio en la siguiente actualización.

---

## Tarea 3 — Filtro por instalación y Supervisor en la Tendencia

**Objetivo.** Dos selectores sobre `chartTendencia` que filtren la gráfica por instalación y por supervisor.

**Realidad del código (corrige el supuesto del brief):**
- El selector **"Cliente"** que ya existe (`filtCliente`, `cgeo_operacion.html` 421) **ya filtra por `id_propiedad`**, que en este esquema es la instalación (`cgeo_api_filtros` 230 hace `SELECT id_propiedad, nombre FROM propiedades`). O sea, "filtro por instalación" ≈ el filtro que ya tienes. Opciones: (a) renombrar la etiqueta "Cliente" → "Instalación", o (b) agregar un segundo nivel cliente→instalación si un cliente agrupa varias propiedades (ver `customer_companies` en el join). **Aclarar con Álvaro** qué jerarquía quieren.
- El filtro por **Supervisor sí es nuevo** y es ambiguo: en `reportes_incidentes` el responsable es `nombre_responsable`; en `supervision_puesto` es `supervisor`. La tendencia combina varias fuentes, así que hay que definir contra qué columna filtra (recomiendo `nombre_responsable` de incidentes, que es lo que alimenta la línea principal de la tendencia).

### Paso 3.1 — Selectores en el template

En `cgeo_operacion.html`, junto a `filtCliente` (~421) o sobre el `chart-card` de Tendencia (~509):

```html
<div class="filter-group">
  <span class="filter-label">Instalación</span>
  <select id="filtInstalacion" class="filter-select"><option value="">Todas</option></select>
</div>
<div class="filter-group">
  <span class="filter-label">Supervisor</span>
  <select id="filtSupervisor" class="filter-select"><option value="">Todos</option></select>
</div>
```

### Paso 3.2 — Poblar los selectores

Ampliar `cgeo_api_filtros()` (`cgeo_bp.py` 230) para devolver también `instalaciones` y `supervisores`:

```python
# instalaciones: ya las tienes (son las propiedades) — puedes reutilizar 'clientes'
# supervisores: SELECT DISTINCT TRIM(nombre_responsable) FROM reportes_incidentes
#               WHERE nombre_responsable IS NOT NULL [AND company_id=%s] ORDER BY 1
return jsonify({"clientes": clientes, "supervisores": supervisores})
```

Y en `loadFiltros()` (`cgeo_operacion.html` 1322) llenar los dos nuevos `<select>` igual que se llena `filtCliente`.

### Paso 3.3 — Pasar los filtros al backend

En `loadData()` (1297) agregar:

```js
const inst = document.getElementById('filtInstalacion').value;
const sup  = document.getElementById('filtSupervisor').value;
if (inst) params.set('instalacion', inst);
if (sup)  params.set('supervisor', sup);
```

### Paso 3.4 — Aplicar en la query de tendencia

En `cgeo_api_operacion_data()` (`cgeo_bp.py` 1135), leer los params y añadir condiciones **solo al bloque de tendencia de incidentes** (línea ~1223, `inc_trend`) — o a todos los bloques que alimenten la gráfica:

```python
instalacion = request.args.get("instalacion") or None
supervisor  = request.args.get("supervisor") or None
# instalacion reutiliza _add_cliente (filtra id_propiedad)
if supervisor:
    inc_conds.append("TRIM(nombre_responsable) = %s"); inc_params.append(supervisor)
```

Importante: hoy `inc_where`/`inc_params` se arman una vez y se reutilizan. Si solo quieres filtrar la tendencia (no los KPIs de arriba), construye un set de condiciones separado para la query de `inc_trend`. **Definir con Álvaro** si el filtro afecta solo la gráfica o toda la pantalla. El brief dice "el resto de la pantalla no cambia" → usa condiciones separadas solo para la tendencia.

### Paso 3.5 — Limpiar filtro

Al poner el select en "" (Todas/Todos) y dar Aplicar (o `onchange`), vuelve al agregado. Ya funciona con la lógica de `if (inst)`. El brief pide que limpiar **no recargue la página**: `loadData()` ya es fetch async sin reload, ok.

**Prueba:** seleccionar una instalación muestra solo esa tendencia; limpiar restaura el agregado sin recargar. Igual para supervisor.

---

## Tarea 4 — Acta de Visita pre-llenada desde el contexto

**Objetivo.** Botón "Agendar visita" abre `acta_visita_cliente.html` con instalación, motivo y temas pre-cargados.

**Lo que existe:** el formulario completo con firma digital (`acta_visita_cliente.html`) y su ruta `registro_y_acta_de_visita_form()` (`forms_bp.py` 993), hoy renderizada **sin contexto**. Campos clave: `id_propiedad` (select, 179), `motivo_visita` (select, 190), `temas_tratados_0` (textarea, 208).

### Paso 4.1 — La ruta acepta contexto por query params

En `forms_bp.py` `registro_y_acta_de_visita_form()` (993):

```python
@forms_bp.route('/registro_y_acta_de_visita')
@jwt_required()
def registro_y_acta_de_visita_form():
    user_name, is_admin = get_user_info_from_jwt()
    ctx = {
        'pre_propiedad': request.args.get('id_propiedad', ''),
        'pre_motivo':    request.args.get('motivo', 'Seguimiento a hallazgos del período'),
        'pre_temas':     request.args.get('temas', ''),
    }
    return render_template('acta_visita_cliente.html', name=user_name,
                           is_admin=is_admin, **ctx, **get_service_urls())
```

### Paso 4.2 — El template usa el contexto

En `acta_visita_cliente.html`:
- `id_propiedad` (179): marcar `selected` la opción que coincida con `pre_propiedad`, o setearlo por JS tras poblar el select (probablemente las opciones se cargan por fetch — en ese caso, en el `.then()` hacer `select.value = "{{ pre_propiedad }}"`).
- `motivo_visita` (190): `{% if pre_motivo %}` preseleccionar; si es texto libre editable, considerar opción "Otro" + input.
- `temas_tratados_0` (208): `<textarea>{{ pre_temas }}</textarea>`.

Los campos de fecha/hora y firmas se dejan vacíos (los llena el Administrador). El brief lo pide así.

### Paso 4.3 — Construir la URL desde los dos puntos de entrada

**(1) Desde la Vista de Evento** (botón `drv-btn-visita`, Tarea 1.3). En `dashboard-record-viewer.js`, el handler arma los temas con las alertas/hallazgos del registro y navega:

```js
const temas = encodeURIComponent('Hallazgos del período:\n- ' + hallazgos.join('\n- '));
const prop  = encodeURIComponent(idPropiedad); // viene del registro (data['ID Propiedad'])
window.location = `/registro_y_acta_de_visita?id_propiedad=${prop}&temas=${temas}`;
```

`id_propiedad` ya viaja en el registro de incidentes (`FORM_CONFIGS`: "ID Propiedad" → `id_propiedad`). Los hallazgos pueden venir de la alerta activa o de un fetch de incidentes abiertos de esa instalación.

**(2) Desde la tendencia filtrada** (`cgeo_operacion.html`). Cuando hay `filtInstalacion` activo, mostrar al pie de la gráfica un botón "Agendar visita" que arme la misma URL con `id_propiedad` = instalación seleccionada y `temas` = novedades de esa instalación.

### Paso 4.4 — Pre-cargar la lista de hallazgos

Para "Agenda y Temas" = lista de alertas activas de esa instalación: reutilizar `cgeo_api_alertas` (`cgeo_bp.py` 556) filtrando por `id_propiedad`, o pasar los textos de alerta que ya están en memoria en el Briefing/Operación. Lo más simple: en la Vista de Evento usar la descripción del propio incidente; en la tendencia, las novedades ya cargadas en `renderData`.

**Prueba:** desde la Vista de Evento de una instalación, "Agendar visita" abre el Acta con instalación y temas cargados; el Administrador solo agrega fecha y firma. Igual desde la tendencia con instalación filtrada.

---

## Resumen de archivos a modificar

| Tarea | Archivos | Tipo de cambio |
| --- | --- | --- |
| 1 | `static/js/dashboard-record-viewer.js` | Reescribir `renderRecordDetail`, mapa 5Q, 2 botones en `ensureModal` |
| 2 | `cgeo_bp.py` (2–3 endpoints nuevos), `dashboard-record-viewer.js` (modal asignar), SQL (tabla opcional) | Backend + UI + correo |
| 3 | `cgeo_operacion.html` (2 selects + JS), `cgeo_bp.py` (`cgeo_api_filtros`, `cgeo_api_operacion_data`) | Filtros front + back |
| 4 | `forms_bp.py` (ruta), `acta_visita_cliente.html` (pre-fill), `dashboard-record-viewer.js` + `cgeo_operacion.html` (puntos de entrada) | Pre-carga por query params |

## Decisiones pendientes con Álvaro (antes de codear)

1. **Tarea 2 — ¿cómo se notifica?** Correo (existe) vs. notificación in-app (no existe, es Fase 3). → recomiendo correo.
2. **Tarea 2 — ¿asignación solo para incidentes** o genérica para cualquier form_type? Define si crear la tabla `asignaciones_hallazgo`.
3. **Tarea 3 — "instalación" vs "cliente":** el filtro actual ya es por instalación (`id_propiedad`). ¿Renombrar, o agregar jerarquía cliente→instalación?
4. **Tarea 3 — filtro por supervisor:** ¿contra `nombre_responsable` (incidentes)? ¿Afecta solo la gráfica o toda la pantalla?
5. **Tarea 1 — ¿los botones de acción** aparecen en todos los dashboards o solo en incidentes?
