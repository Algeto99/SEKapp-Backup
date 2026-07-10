/**
 * SEKapp — Generador del formulario de onboarding en Google Forms
 *
 * Cómo usarlo (una sola vez, ~2 minutos):
 *   1. Abra https://script.google.com y cree un "Nuevo proyecto".
 *   2. Borre el contenido de Code.gs y pegue este archivo completo.
 *   3. Con la función `crearFormularioOnboarding` seleccionada, presione ▶ Ejecutar.
 *   4. Autorice los permisos cuando Google lo solicite (solo la primera vez).
 *   5. En el "Registro de ejecución" aparecerán dos enlaces:
 *        - URL de edición  → para usted (revisar/ajustar el formulario)
 *        - URL de respuesta → para enviar al cliente
 *
 * El formulario usa secciones con navegación condicional para registrar
 * hasta MAX_CLIENTES clientes y MAX_INSTALACIONES instalaciones
 * ("¿Desea registrar otro/a?" → Sí continúa, No salta a la siguiente parte).
 * Ajuste las constantes de abajo si necesita más.
 */

const TITULO_FORMULARIO = 'SEKapp — Onboarding de Nueva Empresa';
const MAX_CLIENTES = 5;
const MAX_INSTALACIONES = 10;

function crearFormularioOnboarding() {
  const form = FormApp.create(TITULO_FORMULARIO);

  form.setDescription(
    'Bienvenido a SEKapp. Este formulario recopila la información necesaria para ' +
    'configurar su empresa en la plataforma: sus clientes, sus instalaciones, los ' +
    'usuarios que tendrán acceso y la configuración operativa inicial.\n\n' +
    'Tiempo estimado: 10–15 minutos.\n\n' +
    'Antes de comenzar, le recomendamos tener a la mano:\n' +
    ' • La lista de sus clientes y de las instalaciones donde presta servicio.\n' +
    ' • Direcciones (y coordenadas GPS, si las conoce) de cada instalación.\n' +
    ' • Nombres, correos y teléfonos del Administrador y de los Supervisores.\n\n' +
    'Si no tiene algún dato a la mano, puede dejarlo en blanco y lo confirmaremos después.'
  );
  form.setProgressBar(true);
  form.setShowLinkToRespondAgain(false);
  form.setConfirmationMessage(
    '¡Gracias! Hemos recibido la información de su empresa. ' +
    'Nuestro equipo configurará su entorno en SEKapp y le contactará con las credenciales de acceso.'
  );

  // Solicitar el correo del respondiente (API nueva con respaldo a la anterior).
  try {
    form.setEmailCollectionType(FormApp.EmailCollectionType.RESPONDER_INPUT);
  } catch (e) {
    try { form.setCollectEmail(true); } catch (e2) {}
  }

  const emailValido = FormApp.createTextValidation()
    .setHelpText('Ingrese un correo electrónico válido.')
    .requireTextIsEmail()
    .build();

  // ==========================================================
  // Sección 1 — Datos de la empresa (primera página)
  // ==========================================================
  form.addSectionHeaderItem()
    .setTitle('1. Datos de su empresa')
    .setHelpText('Información general de la empresa de seguridad que se incorpora a SEKapp.');

  form.addTextItem()
    .setTitle('Nombre completo de la empresa')
    .setRequired(true);

  form.addTextItem()
    .setTitle('Nombre corto o siglas (ej.: ABC)')
    .setHelpText('Se usará como identificador de su empresa dentro de la plataforma.')
    .setRequired(true);

  form.addTextItem()
    .setTitle('País y ciudad de la sede principal');

  form.addTextItem()
    .setTitle('Nombre de quien completa este formulario')
    .setRequired(true);

  form.addTextItem()
    .setTitle('Cargo de quien completa este formulario');

  form.addTextItem()
    .setTitle('Teléfono de contacto')
    .setRequired(true);

  form.addMultipleChoiceItem()
    .setTitle('¿Su contrato incluye el módulo opcional "Log de Patrullas"?')
    .setChoiceValues(['Sí', 'No', 'No estoy seguro'])
    .setRequired(true);

  // ==========================================================
  // Secciones 2..N — Clientes (bloques repetibles)
  // ==========================================================
  const pbClientes = [];
  const navClientes = []; // preguntas de navegación; las opciones se conectan al final

  for (let i = 1; i <= MAX_CLIENTES; i++) {
    const pb = form.addPageBreakItem()
      .setTitle('Cliente ' + i)
      .setHelpText('Empresa cliente a la que su compañía presta servicios de seguridad.');
    pbClientes.push(pb);

    form.addTextItem()
      .setTitle('Nombre completo del cliente')
      .setHelpText('Así aparecerá en formularios y reportes.')
      .setRequired(true);

    form.addTextItem()
      .setTitle('Código corto del cliente (ej.: DEF)')
      .setRequired(true);

    if (i < MAX_CLIENTES) {
      const nav = form.addMultipleChoiceItem()
        .setTitle('¿Desea registrar otro cliente?')
        .setRequired(true);
      navClientes.push(nav);
    } else {
      form.addSectionHeaderItem()
        .setTitle('Ha alcanzado el máximo de clientes de este formulario')
        .setHelpText('Si tiene más clientes, indíquelo en la sección final de comentarios y los registraremos por usted.');
    }
  }

  // ==========================================================
  // Secciones — Instalaciones (bloques repetibles)
  // ==========================================================
  const pbInstalaciones = [];
  const navInstalaciones = [];

  for (let j = 1; j <= MAX_INSTALACIONES; j++) {
    const pb = form.addPageBreakItem()
      .setTitle('Instalación ' + j)
      .setHelpText('Propiedad o sitio donde se presta el servicio de seguridad.');
    pbInstalaciones.push(pb);

    form.addTextItem()
      .setTitle('¿A qué cliente pertenece esta instalación? (use el código, ej.: DEF)')
      .setRequired(true);

    form.addTextItem()
      .setTitle('Nombre de la instalación')
      .setHelpText('Como se le conoce operativamente (ej.: Planta Central, Torre Norte).')
      .setRequired(true);

    form.addTextItem()
      .setTitle('Tipo o descripción')
      .setHelpText('Ej.: planta industrial, oficina, bodega, residencial.');

    form.addParagraphTextItem()
      .setTitle('Dirección completa')
      .setRequired(true);

    form.addTextItem()
      .setTitle('Coordenadas GPS (latitud, longitud)')
      .setHelpText('Ej.: 14.634915, -90.506882. Puede obtenerlas en Google Maps (clic derecho sobre el sitio). Si no las conoce, déjelo en blanco.');

    if (j < MAX_INSTALACIONES) {
      const nav = form.addMultipleChoiceItem()
        .setTitle('¿Desea registrar otra instalación?')
        .setRequired(true);
      navInstalaciones.push(nav);
    } else {
      form.addSectionHeaderItem()
        .setTitle('Ha alcanzado el máximo de instalaciones de este formulario')
        .setHelpText('Si tiene más instalaciones, indíquelo en la sección final de comentarios y las registraremos por usted.');
    }
  }

  // ==========================================================
  // Sección — Usuarios y accesos
  // ==========================================================
  const pbUsuarios = form.addPageBreakItem()
    .setTitle('Usuarios y accesos')
    .setHelpText(
      'El registro en SEKapp es cerrado: solo los correos que usted indique aquí ' +
      'podrán crear una cuenta. Cada usuario recibirá una contraseña temporal que ' +
      'deberá cambiar en su primer ingreso.'
    );

  form.addSectionHeaderItem()
    .setTitle('Administrador principal')
    .setHelpText('Ve el Morning Briefing, los dashboards y genera los informes.');

  form.addTextItem()
    .setTitle('Nombre completo del Administrador')
    .setRequired(true);

  form.addTextItem()
    .setTitle('Correo electrónico del Administrador')
    .setRequired(true)
    .setValidation(emailValido);

  form.addTextItem()
    .setTitle('Teléfono del Administrador');

  form.addParagraphTextItem()
    .setTitle('Otros administradores (opcional)')
    .setHelpText('Uno por línea: Nombre completo, correo, teléfono.');

  form.addSectionHeaderItem()
    .setTitle('Supervisores de Seguridad')
    .setHelpText('Realizan las supervisiones en campo y llenan los formularios desde el celular.');

  form.addParagraphTextItem()
    .setTitle('Lista de Supervisores')
    .setHelpText('Uno por línea: Nombre completo, correo, teléfono.\nEj.: Juan Pérez, jperez@abc.com, +502 5555 1234')
    .setRequired(true);

  form.addMultipleChoiceItem()
    .setTitle('¿Cómo prefieren recibir las credenciales temporales?')
    .setChoiceValues(['Correo electrónico', 'WhatsApp'])
    .showOtherOption(true)
    .setRequired(true);

  // ==========================================================
  // Sección — Configuración operativa (Umbrales y KPIs)
  // ==========================================================
  form.addPageBreakItem()
    .setTitle('Configuración operativa')
    .setHelpText('Parámetros que controlan las alertas, metas y semáforos de KPIs en la plataforma.');

  form.addDateItem()
    .setTitle('Fecha de inicio de operación en SEKapp')
    .setHelpText('Muy importante: los datos anteriores a esta fecha no se tomarán en cuenta para alertas ni indicadores.')
    .setRequired(true);

  form.addMultipleChoiceItem()
    .setTitle('¿Desea usar los umbrales estándar de KPIs?')
    .setHelpText(
      'Estándar: verde ≥ 90 %, amarillo 70–89 %, rojo < 70 %. ' +
      'Meta de 25 supervisiones y 20 visitas por período. ' +
      'Alerta tras 2 días sin supervisión. Escalamiento de incidentes a las 24 h. ' +
      'Aviso de certificaciones por vencer a 30 días y de compromisos a 5 días.'
    )
    .setChoiceValues(['Sí, usar los valores estándar', 'No, tenemos indicadores propios (detallar abajo)'])
    .setRequired(true);

  form.addParagraphTextItem()
    .setTitle('Si tienen indicadores o SLAs propios, detállelos aquí');

  form.addListItem()
    .setTitle('Periodicidad de la meta de supervisiones')
    .setChoiceValues(['Diario', 'Semanal', 'Mensual'])
    .setRequired(true);

  form.addListItem()
    .setTitle('Periodicidad de la meta de visitas')
    .setChoiceValues(['Diario', 'Semanal', 'Mensual'])
    .setRequired(true);

  // ==========================================================
  // Sección — Operación en campo
  // ==========================================================
  form.addPageBreakItem()
    .setTitle('Operación en campo')
    .setHelpText('Nos ayuda a preparar los formularios y la capacitación de sus Supervisores.');

  form.addCheckboxItem()
    .setTitle('Turnos que operan')
    .setChoiceValues(['Diurno', 'Nocturno', '24 horas'])
    .showOtherOption(true);

  form.addCheckboxItem()
    .setTitle('Recursos utilizados en la operación')
    .setChoiceValues(['Vehículos', 'Motocicletas', 'Guardias armados', 'Radios de comunicación']);

  form.addParagraphTextItem()
    .setTitle('Nombres estándar de puestos o áreas por instalación (opcional)')
    .setHelpText('Ayuda a mantener los reportes ordenados. Ej.: Garita principal, Recepción, Perímetro norte.');

  form.addParagraphTextItem()
    .setTitle('Comentarios finales')
    .setHelpText('Clientes o instalaciones adicionales, aclaraciones o cualquier otro dato que debamos conocer.');

  // ==========================================================
  // Conectar la navegación condicional (requiere que todas las
  // secciones ya existan, por eso se hace al final)
  // ==========================================================
  navClientes.forEach(function (nav, idx) {
    nav.setChoices([
      nav.createChoice('Sí, registrar otro cliente', pbClientes[idx + 1]),
      nav.createChoice('No, continuar con las instalaciones', pbInstalaciones[0]),
    ]);
  });

  navInstalaciones.forEach(function (nav, idx) {
    nav.setChoices([
      nav.createChoice('Sí, registrar otra instalación', pbInstalaciones[idx + 1]),
      nav.createChoice('No, continuar con los usuarios', pbUsuarios),
    ]);
  });

  Logger.log('Formulario creado con éxito.');
  Logger.log('URL de edición (para usted):   ' + form.getEditUrl());
  Logger.log('URL de respuesta (para el cliente): ' + form.getPublishedUrl());
  return form;
}
