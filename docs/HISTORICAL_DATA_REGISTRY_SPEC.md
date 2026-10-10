# Registro histórico offline — Fase 4A-5B

Base `0a3c153df02527cca7ffbc021d0f11c2a73d779a`. Runtime **STOPPED**.
Biblioteca explícita, sin startup, CLI operativa, red, descargas ni integración
automática con ReplayRunner, FinancialPerformance, IsolatedMarginRisk o reservas.
No hay datasets reales aportados: **DATASET_NOT_PROVIDED**. Las pruebas utilizan
**SYNTHETIC_FIXTURE**, sin certificar rendimiento histórico o supervivencia.

## Arquitectura y contrato

```text
Original local explícito + Registration + reloj UTC explícito
    ↓ local_files / parsers / audit_file de 4A-5A
HistoricalDatasetRegistry → SQLite de registro separada
    ├─ declaración + SHA original + manifiesto + identidad de auditoría
    ├─ índices Trade sobre el original; ninguna segunda tabla de velas productivas
    ├─ registros independientes Mark / funding / reglas
    └─ segmentos sellados y estados de importación
          ↓ selección explícita de DatasetIds
HistoricalQuery → HistoricalReconciliation → matriz / HistoricalRegistryReadiness
          ↓ candidatos read-only, sin autorización financiera
```

Archivos nuevos dentro de `replay/historical_validation/`: `segments.py`,
`storage.py`, `registry.py`, `query.py`, `reconciliation.py`, `readiness.py` y
`registry_benchmark.py`. Los diez archivos aprobados de 4A-5A permanecen intactos.
No cambia ningún motor, normalizador, interfaz financiera, dependencia o modelo
estratégico. `parse_record` sigue usando `normalize_kline` para Trade; el registro
no realiza ingesta Futures ni crea otra fuente autoritativa de Trade.

Contrato exclusivo: BINANCE, BINANCE_USDM_FUTURES, 1000SHIBUSDC, PERPETUAL, USDC,
inicio declarado **2024-03-28 07:00 UTC**. No hay sustituciones Spot/USDT, factor
1000 adicional o multiplicación por leverage. LONG/4x/Base–DCA7/TP0.80% permanecen
en sus módulos aprobados; este registro no utiliza sus fórmulas.

## Declaración, identidad y procedencia

`Registration` frozen exige `DatasetDeclaration`, original local, formato
CSV/JSON/JSONL, validation_time UTC, procedencia y su evidencia textual, segmentos
explícitos, y permite member ZIP, checksum local y FundingInventory externos.
Límites reutilizados de 4A-5A. Parser `historical-local-v1/4a5b-1`; schema1.
No utiliza datetime.now(), UUID aleatorios ni fechas implícitas.

Las fuentes se declaran SYNTHETIC_FIXTURE, SOURCE_DECLARED o UNKNOWN. Ninguna
etiqueta autentica Binance. SOURCE_AUTHENTICATED continúa rechazado por 4A-5A;
OBSERVED sigue inhabilitado por Reference, sin editar ese contrato.

DatasetId = SHA-256 del encoding canónico de Registration y hash original.
Incluye contrato, período, unidades, parser, schema, partición, localización,
adquisición/validación y evidencia de fuente. Los mismos inputs explícitos
producen el mismo ID; mover el original o cambiar su declaración crea otro ID.
AuditId = SHA-256 del manifiesto/auditoría original. SegmentId vincula DatasetId
con start/end explícitos; el hash de origen y parser anclan su interpretación.
Cada segmento guarda también digest de las filas indexadas, conteo y cobertura.

La declaración conserva período solicitado; el manifiesto conserva período
auditado, first/last, conteos únicos, duplicados, contradicciones, gaps, checksum,
causalidad, integridad y autorización. El índice mantiene filas originales y
locators, incluso duplicados; no se confunde ese conteo con cobertura única.
La finalización de importación **COMPLETE_IMPORT** no significa cobertura completa.

Adquisición, validación, tiempo efectivo y publicación son independientes. El hash
global del archivo/dataset no forma parte del digest económico del prefijo.
Procedencias diferentes se mantienen en DatasetIds/sources, aunque su economía
sea equivalente. La identidad económica usa Decimal exacto sin normalize() global;
las métricas de porcentajes usan ledger_context() precisión50/HALF_EVEN.

## SQLite separada y transacciones

El operador elige una ruta nueva, fuera de Git. El registro rechaza adoptar un
esquema ajeno o una fuente como su propia base. No crea ni modifica
`futures_klines_1m`, `import_manifest`, tablas del colector o archivos originales.

| Tabla STRICT | Contenido y claves |
|---|---|
| registry_meta | Schema soportado, versión1. |
| datasets | PK DatasetId, declaración, original SHA, audit completo/AuditId y estado. |
| records | PK(DatasetId,sequence), FK a fuente, tiempos, event_id, hash crudo, payload opcional. |
| segments | PK SegmentId, FK fuente, período, conteo, digest, cobertura y conflictos. |
| imports | PK intento determinista, COMPLETE_IMPORT/FAILED, error y fecha explícita. |
| import_events | Historial inmutable de estados y errores; recovery no borra el fallo anterior. |
| reconciliations | PK SHA del informe causal verificable, contenido canónico. |
| conflicts | FK informe, PK(informe,effective,reason), DatasetIds incompatibles. |
| external_trade_inventories | Inventarios SQLite Trade existentes, archivo/hash/auditoría read-only. |

Índice `temporal_records(dataset_id,effective,available)` y PKs por fuente/secuencia.
Cache SQLite1MiB; precios nunca REAL. Mark/funding/reglas se conservan como payload
JSON textual exacto, identificado por categoría de la declaración y tiempos.
Trade almacena sólo hashes, disponibilidad y locators; payload es NULL. Su lectura
recupera la fila del original usando el lector aprobado, no otra tabla Candle.

Escritura: BEGIN IMMEDIATE, synchronous FULL, foreign_keys ON. Dataset, filas y
segmentos se confirman en una sola transacción. Cualquier fallo, incluido COMMIT,
revierte todo el lote; FAILED se registra después del rollback. Una muerte del
proceso antes del commit deja la transacción sin confirmar: SQLite revierte al
reabrir y el mismo intento se puede repetir. No se publica un segmento parcial.
Un fallo de disco que impida incluso escribir FAILED sigue propagándose como error;
no se declara recovery exitoso ni se oculta una indisponibilidad física.

Una repetición verifica la fuente y el índice existente antes de devolver el mismo
ID. No duplica hechos. Las conexiones de consulta usan mode=ro, query_only y BEGIN;
no crean bases ausentes ni permiten escrituras. integrity_check, foreign_key_check
y schema soportado se comprueban al abrir. Conexiones se cierran incluso ante error.

## Integridad y sellado

Reutiliza límites, checksum antes/después, CRC/ZIP, traversal, symlink, miembros,
ratios de compresión, tamaños, JSON contradictorio/no finito, UTF-8, timestamps y
validación económica de 4A-5A. Se vuelven a validar todas las filas para el índice:
fuera de período, desorden, contrato incorrecto o campos inválidos abortan.
Contradicciones válidas económicamente se conservan para bloquear conciliación;
gaps y datos económicos insuficientes se registran sin inventar valores.

Antes de commit se recalculan SHA y auditoría, incluidos checksums aportados.
La consulta reconstruye identidad y auditoría desde el original, compara cada
fila/locator y cada sello contra sus datos originales. Se rechaza modificar un
segmento aunque se recalcule su digest o el checksum del archivo SQLite.
También verifica cada fila entregada y el SHA al completar el stream. Un stream
interrumpido no constituye un informe sellado exitoso.

Cambiar contenido/declaración exige una nueva versión con DatasetId distinto.
Una HistoricalQuery fija una tupla inmutable de IDs: registrar otro archivo no
cambia silenciosamente consultas anteriormente selladas. No hay UPDATE de fuentes,
resolución silenciosa de conflictos, reparación arbitraria o firma Binance implícita.
Sin originales no se puede reconstruir la identidad: se bloquea la lectura.

## Segmentos e inventarios Trade externos

Segmentos UTC start/end particionan exactamente el período declarado, sin gaps
de configuración ni solapamientos; Trade/Mark exigen fronteras1m. Hasta1000
segmentos explícitos por registro, sin objeto de millones de velas. Un segmento
Trade/Mark cuenta aperturas únicas contra cada minuto esperado; funding/reglas
no reciben calendarios inventados por contar su primera/última observación.

`register_trade_sqlite_inventory` reutiliza `audit_sqlite` aprobado, sin copiar
Candle. Exige artefacto SQLite sellado, sin WAL/SHM y con journal rollback de un
solo archivo. Una fuente operativa WAL necesita una copia consistente preparada
por el operador; el registro no altera el journal ni realiza backups productivos.
Los inventarios externos conservan `BLOCKED_REQUIRES_SEPARATE_PREFIX_MANIFEST_REVIEW`:
los manifiestos globales existentes carecen de publicación causal por registro.
Su ID no es aceptado por HistoricalQuery como dataset causal. Esta restricción
preserva 4A-5A y es una limitación explícita de adaptación posterior.

## Lecturas, readiness y conexión futura

Uso mediante HistoricalQuery(path, tuple_dataset_ids): `read_trade`, `read_mark`,
`read_funding`, `read_rules(kind,...)`; start/end UTC, intervalo efectivo
semiabierto, cutoff de disponibilidad inclusivo. Devuelven iteradores acotados
de HistoricalEvent con registro auditado, IDs económicos y procedencia separada.
Detalles en [HISTORICAL_RECONCILIATION_SPEC.md](HISTORICAL_RECONCILIATION_SPEC.md).

HistoricalRegistryReadiness separa Trade, Mark, funding, filtros, brackets, fees,
autenticidad, causalidad, adaptación financiera y validación de liquidación.
Sin fuente: MISSING/DATASET_NOT_PROVIDED. Cobertura suficiente:
AVAILABLE_FOR_REVIEW, nunca autorización financiera. Categorías desconocidas no
impiden auditar las independientes. No se produce READY global financiero.

Los candidatos conceptuales HistoricalTradeSource/HistoricalMarkSource/
HistoricalFundingSource/HistoricalRulesSource se materializan como consultas de
registros auditados, **sin implementar los puertos financieros**. Conectarlos exige
revisión separada: autenticación independiente, publicación/vigencia por evento,
revisión causal de manifiestos SQLite, cobertura/roles por fill y ronda, inventario
funding auténtico, brackets/fees históricos de este contrato y aprobación de la
extensión de Reference/OBSERVED. Mark OHLC no implica ticks, funding rate no es pago,
bracket/fee declarados no son evidencia autorizada, LIMIT no implica maker.

Los motores previos siguen con journals/series/snapshots acumulativos. Futura
reanudación eficiente requiere checkpoints por segmento, anclas de fuente verificables,
versionado de correcciones, cursores auditados y validación económica revisada;
no se optimizan ni reescriben esos motores aquí. Tampoco hay recovery LIVE.

Comandos, benchmarks, casos y límites medidos en [PHASE_4A_5B_REPORT.md](PHASE_4A_5B_REPORT.md).
