# Seguridad

- Secretos fuera de Git; ninguna clave real dentro del repositorio ni sus logs.
  `.env.example` contiene solo configuración pública. En Fase 0 no se requieren claves.
- Mínimo privilegio: contenedor con usuario no root; Compose sin red, sin
  capacidades Linux adicionales y con sistema de archivos de solo lectura.
- Live fail-closed: nunca por defecto. Su futura habilitación requerirá
  configuración explícita y controles definidos en la especificación correspondiente.
  Actualmente todo modo distinto de STOPPED se rechaza, incluido LIVE_ARMED.
- Idempotencia, reconciliación, SAFE_MODE y auditoría son requisitos futuros,
  pendientes de especificación e implementación. El enum no implementa protecciones
  operativas ni transiciones.
- No ocultar errores. Usar eventos estructurados sin secretos ni dumps de configuración.
- `.gitignore` excluye credenciales, bases locales, logs y datos históricos;
  revisar siempre los archivos a incluir en cada commit.
- El contexto Docker permite solo código, tests, README y manifiesto de dependencias.
  No incorporar secretos en imágenes, argumentos de build o capas.
