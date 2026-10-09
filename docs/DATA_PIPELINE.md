# Fase Datos 1 — histórico público USDⓈ-M Futures

La CLI de datos es finita y separada del bot. No requiere API keys,
no llama endpoints privados, StrategyEngine ni ejecución. El runtime principal
continúa en STOPPED y termina; no descarga nada al arrancar. No se implementan
Fase 3B-2B, Replay, Shadow, Live ni órdenes. Fase Datos 2 añade operación autónoma
del mismo pipeline y ejemplos systemd sin instalar: [DATA_COLLECTOR.md](DATA_COLLECTOR.md).

## Mercado, fechas y fuentes

Mercado persistido: `BINANCE_USDM_FUTURES`, intervalo fijo **1m** y símbolo inicial
**1000SHIBUSDC**. Inicio objetivo: **2024-03-28 07:00:00 UTC**, lanzamiento indicado
en la base aprobada. Se rechazan registros anteriores para este contrato. No se
consulta Spot, SHIBUSDC, SHIBUSDT, 1000SHIBUSDT ni COIN-M; no hay conversión de
símbolos, precios o volúmenes por 1000. Otros símbolos configurados deben ser
identificadores USDⓈ-M correctos, con inicio explícito según su disponibilidad.

Fuentes oficiales HTTPS con validación TLS y confianza del sistema:

```text
https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/1m/{SYMBOL}-1m-{YYYY-MM}.zip
https://data.binance.vision/data/futures/um/daily/klines/{SYMBOL}/1m/{SYMBOL}-1m-{YYYY-MM-DD}.zip
https://fapi.binance.com/fapi/v1/klines?symbol=1000SHIBUSDC&interval=1m&startTime=...&endTime=...&limit=1500
```

La fecha objetivo no se presenta como primer minuto **comprobado en Binance**.
`status.first_persisted_open_ms` informa el primer minuto realmente guardado del
intervalo descargado; confirmar el primer minuto global exige consultar el inicio
oficial y auditarlo. Un gap inicial permanece explícito, sin fabricar registros.
Los tests sintéticos no confirman la disponibilidad real de Binance.

## Separación de responsabilidades

- `binance/public_data.py`: transporte público HTTPS, timeout de 20s, máximo
  tres intentos por petición ante 429/5xx, errores de conexión y timeout, con
  pausas de 1s/2s. No reintenta 403/404/451. Solo Futures públicos/archivos UM;
  rechaza credenciales en URL, HTTP y redirecciones fuera del recurso oficial.
  Un error durante lectura del cuerpo aborta y elimina el archivo parcial.
- `binance/klines.py`: normalización/validación de filas; no I/O ni estrategia.
- `persistence/futures_sqlite.py`: esquema, importación atómica, manifiesto y
  lectura cronológica de Candle; sin lógica de entrada o estado de rondas.
- `market_data/history.py`: planificación, ZIP/CSV streaming, checksum, backfill,
  sincronización y recovery REST finitos.
- `market_data/audit.py`: integridad y rangos ausentes, sin interpolación.
- `market_data/cli.py`: opciones del operador y resultados JSON; sin worker.

SQLite conserva todo el histórico. La estrategia pura conserva únicamente los
últimos **1560 minutos** en AltBMarketState; no se modifica ese contrato ni Candle.

## Parsing y causalidad

Exactamente 12 columnas Futures: open_time, open, high, low, close, base_volume,
close_time, quote_volume, trades, taker_buy_base_volume, taker_buy_quote_volume,
ignore. Se admite CSV sin encabezado o el encabezado oficial UM
`open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore`.
JSON REST conserva enteros de tiempo y strings de valores financieros; números
JSON usados como precios se rechazan. Los campos adicionales se validan pero no
se guardan en el esquema mínimo. Taker-buy procede **solo del campo 9**.

Timestamps de origen: enteros ms UTC dentro de 2000–2100; otras unidades, bool,
alineación incorrecta o cierre distinto de `open_ms + 59999` producen error.
El Candle normalizado mantiene la apertura y usa cierre exclusivo
`open_ms + 60000`, intervalo `[open_time, close_time)`. No se desplaza un minuto
ni convierte a horario local. OHLC se valida mediante los modelos aprobados;
volúmenes/taker-buy son Decimal finitos, `0 <= taker_buy <= volume`.
No se corrige OHLC, cuantiza, redondea ni rellena ningún campo.

Cada operación recibe `as_of` UTC explícito. La CLI utiliza `--as-of` o el reloj
UTC del operador y publica la referencia en su resultado JSON; el operador debe
mantener sincronizado ese reloj. `end` es exclusivo y no puede superar la última
frontera de minuto ya terminada. Nunca se persiste un intervalo cuyo cierre sea
posterior a la referencia. Un minuto en formación devuelto por REST se excluye.

## Esquema SQLite

`futures_klines_1m` (STRICT):

| Campo | Tipo / significado |
| --- | --- |
| market | TEXT; `BINANCE_USDM_FUTURES` |
| symbol | TEXT; símbolo exacto |
| open_time_ms | INTEGER; apertura alineada a minuto UTC |
| close_time_ms_exclusive | INTEGER; apertura + 60000 |
| open, high, low, close | TEXT Decimal exacto |
| base_volume | TEXT Decimal exacto |
| taker_buy_base_volume | TEXT Decimal exacto; NULL permitido para reconstruir Candle genérico |
| source | TEXT; `monthly`, `daily` o `rest` |
| ingested_at_utc | TEXT ISO-8601; hora UTC real de ingesta, independiente de as_of |

Clave primaria/índice compuesto: **(market, symbol, open_time_ms)**. Las consultas
por símbolo y rango aprovechan ese índice; todos los precios/volúmenes evitan REAL.
La normalización Binance exige taker-buy real, aunque la lectura del modelo
genérico puede representar NULL como None. Los tipos, intervalos y valores de las
filas se vuelven a validar al reconstruir Candle.

`import_manifest` (STRICT) guarda `file` (clave), `period`, SHA-256 opcional
`checksum`, `source`, `state` (`complete`, `failed`, `missing`), `processed_rows`,
`processed_at_utc` (hora UTC real de procesamiento) y `error`. El identificador incluye archivo y rango solicitado
para que una importación parcial no haga omitir después un rango mayor. REST usa
símbolo/rango como identidad. Es un registro del último intento por identidad.
`complete` significa que el lote validado se confirmó; **la integridad temporal
se determina exclusivamente mediante audit.complete**, que también expone
conflictos pendientes. Un CSV puede contener gaps y nunca se declara íntegro por
el solo hecho de finalizar su importación.

Importaciones con `BEGIN IMMEDIATE`/COMMIT explícitos, WAL y synchronous=FULL.
Cada archivo es una transacción y cada página REST es otra; filas se procesan
una a una sin cargar años en RAM. Un fallo revierte el lote completo y registra
`failed` fuera del rollback. Páginas ya confirmadas permanecen recuperables.
Tras una interrupción del proceso SQLite revierte la transacción pendiente; la
invocación siguiente puede repetirla. Conexiones y cursores se cierran.

Si una identidad ya existe, Decimal compara todos sus datos de mercado
numéricamente: `103` y `103.000` son iguales. No se inserta otra vela ni cambia
source, timestamp de ingesta o representación original. Datos diferentes producen
DataConflict con símbolo, minuto y procedencias: rollback, log JSON, manifiesto
failed y revisión del operador; jamás UPDATE silencioso. La PK impide duplicados;
los duplicados y el desorden dentro de un lote se rechazan.

## Archivos, checksum y períodos faltantes

`plan` elige archivos mensuales de meses terminados y diarios de días terminados
del mes todavía abierto. Un mensual 404 queda `missing` y habilita el fallback
diario de ese período. No se consideran meses/días anteriores al lanzamiento.
404 no confirma una importación ni inventa datos; períodos pendientes conservan
su manifiesto aunque una fuente alternativa complete después las velas.

Se descarga un ZIP por vez a un directorio temporal, con chunks de 64 KiB y
limpieza al terminar. Se lee únicamente el miembro CSV exacto esperado; no se
extrae al filesystem. Si existe `.CHECKSUM`, se verifica SHA-256, nombre y formato
antes de importar. Un hash incorrecto detiene la importación. Un CHECKSUM 404
permite procesar con advertencia `archive_unverified` y checksum NULL: **no se
marca como verificado ni se omite automáticamente en una repetición**. Un archivo
confirmado con checksum se omite para el mismo rango; no se vuelve a bajar como
diarios. No se emplean mirrors ni se deshabilita TLS.

## Sincronización, auditoría y recovery

`sync-once` lee el último open persistido y calcula el siguiente minuto; primero
audita el rango para recuperar también su primer gap anterior. REST pagina con
limit <= 1500 y ventanas de petición de como máximo limit minutos, avanza tras el último minuto cerrado realmente recibido y valida
símbolo de petición, orden, rango y contenido. No ordena respuestas defectuosas.
Una respuesta vacía deja `missing` y termina. Una respuesta parcial con huecos
registra `rest_gap`; el resultado audit indica los rangos pendientes y permite
reintento. Una sincronización repetida sobre un rango ya íntegro no llama REST ni
altera la base. No existe bucle permanente.

`audit` usa SQLite integrity_check y recorre el rango ordenado comprobando
intervalos normalizados y el siguiente `open + 60000`. Incluye gaps iniciales,
internos y finales, errores de datos y conflictos entre fuentes. Un rango vacío
de velas nunca es íntegro. `recover-range` consulta exclusivamente un rango UTC
explícito y persiste datos reales mediante las mismas reglas. No borra,
interpola ni fabrica velas de volumen cero. El consumidor futuro debe leer
cronológicamente y respetar gaps; el estado aprobado vuelve a exigir warmup.

## Uso manual

Opciones después del subcomando; todos aceptan `--db-path`, `--symbol`, `--start`,
`--end`, `--as-of` y `--dry-run`. Fechas ISO-8601 UTC con Z/+00:00 y límites
alineados a minuto. `plan` y dry-run no hacen red ni crean una base. `status`/`audit`
abren una base existente en lectura; no crean ni borran datos. Códigos de salida:
0 operación íntegra (o status/plan), 2 auditoría incompleta, 1 error definitivo,
73 bloqueo de escritura ocupado y 75 fallo transitorio de red tras reintentos.

```sh
python -m trading_engine.market_data.cli plan --start 2024-03-28T07:00:00Z --end 2024-03-29T00:00:00Z
python -m trading_engine.market_data.cli backfill --db-path data/futures.sqlite3 --start 2024-03-28T07:00:00Z --end 2024-03-29T00:00:00Z --dry-run
python -m trading_engine.market_data.cli import-archive --archive-file /ruta/1000SHIBUSDC-1m-2024-03.zip --source monthly --period 2024-03 --checksum-file /ruta/1000SHIBUSDC-1m-2024-03.zip.CHECKSUM
python -m trading_engine.market_data.cli sync-once --db-path data/futures.sqlite3 --start 2024-03-28T07:00:00Z
python -m trading_engine.market_data.cli audit --db-path data/futures.sqlite3 --start 2024-03-28T07:00:00Z --end 2024-03-29T00:00:00Z
python -m trading_engine.market_data.cli recover-range --db-path data/futures.sqlite3 --start 2024-03-28T08:00:00Z --end 2024-03-28T08:05:00Z
python -m trading_engine.market_data.cli status --db-path data/futures.sqlite3
```

Quitar `--dry-run` de backfill inicia la descarga del rango elegido por el operador;
no se ejecuta automáticamente. Backfill/recover exigen inicio y fin explícitos;
sync-once exige inicio y usa fin por defecto en el último minuto cerrado. Antes de
descargar años, el operador debe elegir y aprobar ese rango. Base predeterminada
`data/futures.sqlite3`, fuera de archivos versionados. `.gitignore` protege SQLite,
WAL/SHM, directorios de datos, ZIP/CHECKSUM, caches y temporales. Los comandos
manuales no añaden dependencias/API keys ni instalan o activan servicios.

## Validación y estado operativo

Tests automáticos exclusivamente offline con CSV mensual/diario, JSON REST,
ZIP/checksums y transportes simulados. Cubren validación estricta, transacciones,
conflictos, reintento, paginación, velas abiertas, gaps y lectura exacta. Una base
temporal recibe 27h sintéticas CSV + REST; sus Candle reconstruidas se procesan
mediante ingest_closed_1m y coinciden con evaluate_entry_checkpoint en los **108
cierres 15m**, usando únicamente el prefijo causal. La suite Docker corre sin red.

Las fixtures no son datos reales ni una descarga en VPS. La prueba pública
controlada debe efectuarse solo después de pasar los tests offline, con una
ventana pequeña y base temporal. 403/451, timeout o bloqueo de red se reportan;
no se sustituyen por Spot ni fixtures. La disponibilidad real y primer minuto
comprobado se registran en el informe de entrega, sin afirmar acceso no realizado.

Validación de entrega, **2026-10-08 19:38 UTC**, después de los tests offline:
una petición pública Futures para `[2024-03-28 07:00, 07:03)` UTC, limit 1500,
un intento y timeout 8s, con SQLite temporal. El proxy del entorno rechazó el
túnel HTTPS con `403 Forbidden` (URLError); la API Binance no fue alcanzada.
Resultado: **0 velas guardadas**, audit.complete=False y gap de tres minutos.
La base temporal se eliminó al terminar. No se descargó el histórico ni se
confirmó el primer minuto real. La política de salida actual no incluye
`fapi.binance.com` ni `data.binance.vision`; hace falta habilitar HTTPS a esos
dominios para repetir la comprobación/importar fuentes oficiales. No se eludió
el proxy, sustituyeron datos ni inició descarga completa o despliegue en VPS.

Fase Datos 2 añade bloqueo a todas las conexiones de escritura, progreso
operativo persistente, checksums obligatorios para history-run, actualización
acotada, backup consistente y observabilidad. Los comandos anteriores conservan
su semántica manual; para ejecución periódica usar update-once, que no recorre
todo el histórico. Consultar [DATA_COLLECTOR.md](DATA_COLLECTOR.md). El runtime
principal sigue STOPPED; los servicios preparados requieren aprobación e
instalación explícita en el VPS.
