# Fase Datos 2 — colector público Futures autónomo

Implementación para Ubuntu 24.04 LTS/Python 3.12, sin dependencias nuevas,
Binance API keys, endpoints privados, trading ni ejecución de órdenes.
El bot continúa en **STOPPED**. Los archivos systemd son **ejemplos sin instalar
ni habilitar**; no se despliega ni descarga histórico real desde Codex.
Los datos sintéticos de tests no representan descargas ni validación del VPS.

Se mantiene el único pipeline de [Fase Datos 1](DATA_PIPELINE.md): el colector
orquesta sus funciones `backfill`, `sync_range`, `audit`, normalizador y
`FuturesSQLite`. No hay un segundo parser, almacén o motor de datos. El símbolo
del colector es exclusivamente `1000SHIBUSDC`, mercado `BINANCE_USDM_FUTURES`,
intervalo 1m e inicio **2024-03-28 07:00 UTC**. No cambia Candle, AltBMarketState,
filtros, vetos, cálculos financieros, DCA, TP, identidad ni ejecución.

## Histórico y reanudación

`history-run` toma un snapshot UTC al iniciar, resta el retraso de publicación
y recorta el fin a una frontera de minuto. Desde el lanzamiento recorre períodos
mensuales uno por uno usando backfill existente: monthly de meses completos,
daily del mes actual y REST para rangos que continúan ausentes. Registra eventos
JSON por período y manifiestos por archivo/página. No carga años en RAM ni guarda
todos los ZIP: un archivo temporal y un stream CSV por vez.

Los archivos requieren CHECKSUM oficial en este comando: su ausencia, formato
incorrecto, SHA-256 diferente o CSV/ZIP inválido impiden confirmar el lote.
Un ZIP 404 queda missing y admite fallback diario/REST, sin inventar datos. El
modo manual Fase Datos 1 conserva su advertencia para CHECKSUM ausente; un lote
sin checksum nunca se omite como verificado. Se reutilizan los manifiestos
complete con hash para el mismo archivo y rango, incluido marzo previamente
importado. Una importación anterior de solo parte de un mes no confirma el resto.

Cada archivo/página conserva su transacción original. Si falla un período, los
anteriores confirmados permanecen; repetir history-run omite archivos verificados
y recupera pendientes. No hay escritura silenciosa sobre una identidad con
mercado distinto: los duplicados numéricos se mantienen y los conflictos fallan
con procedencias explícitas. Al finalizar se hace una auditoría completa de
SQLite y Candle desde el lanzamiento hasta el fin solicitado.

El servicio oneshot de histórico permite cerrar SSH; lo supervisa systemd en el
VPS, sin Codex. El ejemplo reintenta solo fallos transitorios, con 60s entre
arranques y límite de tres arranques en diez minutos. Errores definitivos,
historial pendiente y lock ocupado detienen ese servicio para revisión. Tras
un reboot, el operador puede volver a iniciarlo: no vuelve a importar períodos
verificados. No se habilita el histórico en cada arranque del VPS.

## Actualización finita y readiness

`update-once` termina tras una invocación y exige historia previamente auditada
íntegra hasta el cutoff de una inicialización completa. No adopta una SQLite
vacía ni acepta solo un mes como validación de todos los años. history-run completo
o audit-full completo establecen `ready`. Un `--end` histórico anterior al cutoff
sirve para un trabajo parcial y no habilita una primera actualización periódica.

El fin elegible es `floor_minute(as_of_utc - publication_delay_seconds)`,
por defecto retraso de **5s**. El reloj UTC del VPS debe estar sincronizado; una
regresión anterior al checkpoint hace fallar la actualización sin ingerir nada.
Se utiliza el cierre exclusivo de Fase Datos 1, nunca la vela en formación.

Se consulta la última apertura mediante el índice, pero se reanuda desde el
primer minuto no validado: un gap anterior no queda oculto detrás de una vela
posterior. Se comprueba una vela de frontera y hasta **1500 minutos** por
invocación (configurable 1..10080). Se audita ese tramo, REST consulta únicamente
sus gaps y después se confirma su progreso. Datos ya confirmados antes de un
reinicio no requieren otra petición. Cada página REST tiene como máximo 1500
velas y nunca excede su ventana solicitada.

No se hace COUNT de todos los años, integrity_check global ni auditoría histórica
en cada minuto. Una desconexión extensa se recupera en varias invocaciones
acotadas, informando backlog y pendientes con salida 2. Los gaps/colas vacías
persisten para reintento real; no se interpolan, rellenan o consideran íntegros.
Los errores transitorios de transporte/429/5xx tienen reintentos limitados y
backoff. La separación por defecto entre peticiones, incluidos reintentos, es
al menos **1 segundo**; a limit 1500 limita el uso a 600 unidades de peso/minuto
si cada petición consume peso 10. Deben respetarse además las condiciones y
límites efectivos del proveedor y el tráfico restante de la misma IP.
403/451, denegación del túnel proxy, errores de confianza TLS y corrupción son
definitivos; no se eluden restricciones ni se cambia la validación TLS.

## Persistencia, bloqueo y recuperación

El esquema añade tres tablas pequeñas STRICT sin reemplazar ni borrar las
tablas de Fase Datos 1:

- `collector_state`, PK (market, symbol): inicio, fin del prefijo validado,
  ready, último intento/sincronización exitosa/acción/estado/error y auditoría.
- `collector_gaps`, PK (market, symbol, start_ms): rangos semiabiertos ausentes.
- `collector_errors`, PK (market, symbol, action): último error pendiente por
  operación; un update exitoso no oculta un backup fallido. Se limpia cuando
  esa operación termina correctamente (gaps pendientes se informan aparte).

Los checkpoints y gaps se confirman juntos en una transacción. Los datos de
mercado se confirman primero; el progreso después. Si el proceso muere entre
ambos, el checkpoint conservador vuelve a revisar ese tramo y reutiliza las
velas ya persistidas. Una muerte durante una transacción deja rollback SQLite,
no un lote exitoso. `last_state=running` tras la desaparición del servicio señala
un intento interrumpido; la siguiente invocación retoma el estado persistido.

Todas las aperturas de escritura de FuturesSQLite, incluidos los comandos
manuales anteriores, mantienen un flock exclusivo sobre la ruta canónica
`<db>.lock` durante la invocación. No se borra el lockfile: el kernel libera
flock tras salida, kill o reinicio. El contendiente falla con salida 73 antes
de escribir; status puede leer un snapshot WAL sin bloquear la ingesta.
Debe usarse una única ruta canónica local; no crear hard links ni escritores
SQLite externos que eludan este bloqueo cooperativo.

Triggers invalidan readiness si se actualizan/borran velas o cambia un prefijo
ya validado. El colector no ejecuta esos cambios financieros; son una defensa
frente a modificaciones externas. La auditoría completa separada detecta
corrupción antigua, gaps y taker-buy ausente. Si un audit periódico descubre una
cola pendiente después de un prefijo previamente válido, el recovery incremental
sigue permitido. Si existe corrupción o un borrado del prefijo, se exige reparación
explícita y nueva auditoría completa antes de continuar.

## Espacio y backups

Se comprueba espacio antes del trabajo y de peticiones/lotes; los ZIP temporales
también comprueban su filesystem. Reserva predeterminada: **512 MiB**. Un fallo
de disco durante una escritura produce rollback/error visible, sin marcar éxito.
La reserva no sustituye monitorizar el disco frente a otros procesos.

`backup` usa la API backup de SQLite, incluye las páginas de WAL, copia por
256 páginas y verifica integrity_check en la copia. Requiere espacio para el
tamaño estimado de la base más reserva. Publica mediante un archivo temporal
en el mismo directorio y enlace atómico a un nombre **nuevo**; rechaza destinos
existentes y la propia base. Ante fallo elimina el temporal y registra error.
No se copia a mano solo el .sqlite mientras hay WAL, no se reemplaza producción
y no existe restauración automática. La copia es consistente, no una garantía
de recuperación frente a la pérdida del disco del VPS: conservar también una
copia verificada fuera de ese disco, mediante el procedimiento del operador.

Ejecutar un backup antes de cada actualización de versión y de forma periódica
bajo una política explícita de retención/espacio. La eliminación de copias viejas
no se automatiza en esta fase. Para restaurar: parar jobs, conservar la base
original, validar una copia recuperada y auditarla antes de reactivar el timer.

## Comandos y resultados JSON

```sh
.venv/bin/python -m trading_engine.market_data.cli history-run --db-path /ruta/absoluta/sqlite-existente.sqlite3 --dry-run
.venv/bin/python -m trading_engine.market_data.cli history-run --db-path /ruta/absoluta/sqlite-existente.sqlite3
.venv/bin/python -m trading_engine.market_data.cli update-once --db-path /ruta/absoluta/sqlite-existente.sqlite3 --publication-delay-seconds 5 --max-update-minutes 1500
.venv/bin/python -m trading_engine.market_data.cli audit-full --db-path /ruta/absoluta/sqlite-existente.sqlite3
.venv/bin/python -m trading_engine.market_data.cli collector-status --db-path /ruta/absoluta/sqlite-existente.sqlite3
.venv/bin/python -m trading_engine.market_data.cli backup --db-path /ruta/absoluta/sqlite-existente.sqlite3 --backup-path /ruta/backup-nuevo.sqlite3
```

Config adicional: `--min-free-mib` (512), `--request-interval-seconds` (1),
`--as-of` UTC explícito para referencias verificables. La ingesta automática usa
el reloj UTC sincronizado; no fijar una fecha antigua en los servicios. Los
comandos operativos default usan `/var/lib/shib-alt-b/futures.sqlite3`, pero el
despliegue debe **elegir y verificar la ruta de la SQLite existente**. Dry-run no
hace red, no abre SQLite y no habilita jobs.

`collector-status` es un informe JSON de un snapshot de lectura, sin HTTP ni
auditoría completa. Campos principales:

| Campo | Significado |
| --- | --- |
| market, symbol, db_path | Identidad del único histórico |
| first_open_ms/utc, last_open_ms/utc, rows | Límites y total persistidos |
| eligible_end_utc, lag_minutes | Cutoff exclusivo y atraso de la última vela; negativo advierte reloj/delay |
| last_attempt_utc | Fecha real del último intento |
| last_successful_sync_utc | Última ingesta íntegra al cutoff; audit/backup no la inventan |
| minutes_missing_verified, gaps | Minutos/rangos ausentes comprobados |
| unverified_minutes | Tiempo posterior al último tramo auditado; pendiente de comprobar |
| progress | Gate, prefijo, acción, estado y fechas de auditoría |
| last_import | Último manifiesto, estado, procedencia, checksum/error |
| pending_errors | Errores pendientes por operación y conflictos de datos |
| free_bytes, required_reserve_bytes | Espacio y reserva del filesystem |
| complete | Prefijo validado hasta el cutoff actual, sin gaps/errores y ready |

`audit_at_utc` es fecha de auditoría completa; actualizaciones incrementales no
la rejuvenecen artificialmente. Los logs JSON por operación/período van a stderr
o journal; el informe final va a stdout. No se registran secretos ni el entorno.
Salidas: 0 completo, 2 pendientes, 1 definitivo, 73 lock ocupado, 75 transitorio
tras reintentos. Una invocación parcial/no íntegra nunca informa complete=true.

## Ejemplos systemd y VPS

Ver [procedimiento de despliegue](DEPLOYMENT.md). `deploy/systemd` prepara:
servicio manual de histórico, servicio de actualización finita y su timer,
servicio de auditoría completa y timer diario, y configuración sin secretos.
El timer de update se activa al segundo 10 de cada minuto; el de audit a las
**22:15 de El Salvador**, equivalentes a **04:15 UTC del día siguiente**.
Los timers persistent retoman tras reboot. La auditoría puede encontrar el lock
ocupado y fallar visiblemente; repetirla al liberar el histórico/update.

Ejemplos: usuario ubuntu, WorkingDirectory `/home/ubuntu/SHib-alt-b`, memoria
máxima 768 MiB, un núcleo como máximo y filesystem del sistema de solo lectura
con escritura permitida únicamente al directorio revisado de datos. Adaptar
ReadWritePaths a la ruta **existente** antes de instalar. Los ExecStartPre
rechazan una base ausente/vacía: no iniciar una base nueva por error.

No se ha instalado, habilitado ni ejecutado systemd desde Codex. No se ha probado
el histórico real completo, su duración o tamaño efectivo en el VPS; los límites
son protecciones y el diseño es streaming. Se requiere aprobación de esta versión,
validación en el VPS y auditoría histórica completa antes de activar el timer.
