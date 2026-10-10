# Informe de entrega — Fase 4A-5A

Base aprobada: `9ed5a05f813855ae239331b7829adb9e960ed8ef`.
Feature: `phase-4a-5a-historical-data-validation`.
Entrega de herramientas offline para auditoría; NO MERGE, despliegue o trading.
Runtime **STOPPED**. Datasets reales: **DATASET_NOT_PROVIDED**.

## Resultado arquitectónico

Módulo independiente `replay/historical_validation`, diez archivos, sin dependencias
nuevas o integración automática al replay. Reutiliza normalización Binance aprobada,
invariantes OHLC/brackets, lector FuturesSQLite read-only, auditoría existente,
encoding canónico y contexto Decimal50. No modifica interfaces financieras.

Incluye declaración exacta de contrato/unidades, nueve categorías económicas,
manifiestos, lectores locales acotados, parsers Trade/Mark/funding, reglas temporales
de símbolo/brackets/fees, cobertura mensual y readiness por ocho métricas.
El índice de identidades efímero vive fuera de la fuente y no contiene otra tabla
persistente de velas. Auditoría SQLite no altera esquema ni datos.

Contratos completos: [HISTORICAL_DATA_VALIDATION_SPEC.md](HISTORICAL_DATA_VALIDATION_SPEC.md).
Fuentes/faltantes: [HISTORICAL_SOURCE_CATALOG.md](HISTORICAL_SOURCE_CATALOG.md).

## Procedencia, calidad y disponibilidad

- Trade: normalización exacta12 columnas, ms/closeTime59999, cierre exclusivo,
  OHLC/volumen/taker-buy, orden, gaps y duplicados separados. No corrige datos.
- Mark: objeto OHLC diferente de Trade, disponible al cierre; sin ticks o MarkEvidence.
- Funding: Decimal, instantes/publicación separados; inventario explícito sin supuesto8h,
  sin pagos por rondas ni FundingEvidence. Falta de inventario/referencia: INCOMPLETE.
- Brackets: vigencias, MMR/cum/maxleverage, continuidad y ajuste de usuario declarativo;
  sin consultas firmadas, reglas USDT o extensión arbitraria al pasado.
- Símbolo: filtros de precio/cantidad/notional/status/leverage con quality temporal.
- Fees: vigencia, maker/taker, promoción/nivel/descuento/moneda/rol declarado;
  no asigna tarifas reales a fills ni interpreta LIMIT como maker.
- SQLite: integridad y cobertura read-only, estados de importación, checksums faltantes,
  conflictos, registros fuera de contrato y digest de filas/manifiestos separados.
- Matriz: meses y categorías; PRE_CONTRACT antes del lanzamiento; cobertura no inferida
  desde último registro. Mes completo no se invalida por un gap posterior de otro mes.

SHA/CRC prueban identidad o integridad local, no Binance. SOURCE_AUTHENTICATED sigue
sin verificador independiente; intentos de declararlo manualmente se rechazan.
OBSERVED permanece deshabilitado. Nunca se autoriza económicamente el dataset.
Trade/Mark/funding/reglas reales siguen MISSING por falta de archivos aportados;
liquidación INCOMPLETE por faltantes intraminuto/exchange/evidencia financiera.

## Pruebas causales y seguridad

Prefijos Trade/Mark/funding/brackets/fees iguales al añadir valores futuros,
incluso valores económicos futuros inválidos. Hash global de archivo separado del
event_digest causal. Adquisición no reemplaza tiempo de mercado. Mark Close bloqueado
antes de cierre; reglas CURRENT_ONLY/publicadas tarde no utilizables retrospectivamente.
Datos de tiempo no localizables y errores/límites bloquean, sin afirmar invariancia.

SQLite preserva controles globales del auditor aprobado. Es inventario del artefacto,
no una autorización causal retrospectiva de manifiestos; su estado lo identifica:
BLOCKED_REQUIRES_SEPARATE_PREFIX_MANIFEST_REVIEW. No se relajan esos controles.

ZIP corrupto/CRC incorrecto, traversal, symlink, miembros múltiples, ratio/tamaño,
directorio excesivo, JSON contradictorio/NaN/Infinity, UTF-8 inválido, timestamps,
floats económicos y límites de filas/hallazgos son rechazados. Pruebas con socket
bloqueado y AST aseguran ausencia de clientes/red/comandos de trading del módulo.
Contexto Decimal hostil no cambia resultados. Fuentes productivas no modificadas.

## Benchmarks exclusivamente sintéticos

Auditoría de generador streaming, Decimal50, cache SQLite efímera1MiB, tracemalloc
activo. Host Python3.12.14. Mediciones diagnósticas; no garantías de rendimiento.

| Fixture | Registros | Tiempo s | Registros/s | Pico Python bytes | Pico RSS KiB |
|---|---:|---:|---:|---:|---:|
| 1 día | 1440 | 3.7008 | 389.1 | 560236 | 20224 |
| 7 días | 10080 | 19.8214 | 508.5 | 113090 | 20864 |
| 30 días | 43200 | 50.1735 | 861.0 | 32906 | 20864 |

Los tres corrieron secuencialmente, con suites de tests concurrentes. RSS es máximo
acumulado del proceso; el pico
Python del primero incluye inicialización/caches. RSS incluye runtime/caches y no
es memoria incremental por dataset. El índice crece en disco, no en RAM Python.
Journal de auditoría por registro:0. Hallazgos/meses están acotados explícitamente.
JSON monolítico se limita1MiB; el benchmark no acredita escalabilidad de listas JSON.

Medición adicional de la ronda sintética aprobada Base/DCA7/TP con reserva/ADD
modelado, sin cambiar motores ni calcular histórico. Estas medidas pertenecen al
run del fixture financiero anterior a la última revisión del auditor; los motores
y el fixture financiero permanecieron idénticos:

| Medida | Resultado |
|---|---:|
| Minutos leídos, incluido warmup | 1598 |
| Minutos evaluados | 38 |
| Run completo s | 6.5879 |
| Hash/validación de checksum snapshot s | 0.7786 |
| Restauración con validadores completos s | 10.0564 |
| Pico RSS proceso de tests KiB, incluido setup | 90160 |
| Journal ledger / fills | 15 / 14 |
| Journal performance / serie equity | 235 / 91 |
| Journal riesgo / evaluaciones | 184 / 91 |
| Journal reservas / muestras | 313 / 91 |
| Cola transferencias al cierre | 0 |
| Informe canónico bytes | 6100095 |

Snapshot/restauración/reportes continuo y reanudado son exactamente iguales.
El tiempo de restauración incluye reconstrucción causal aprobada, no sólo checksum.
El pico de proceso no aísla memoria del restore. No se extrapola esta ronda corta
a años ni se fuerza un benchmark financiero de30 días: el coste observado y los
journals acumulativos hacen necesaria una revisión de escala primero.

## Diagnóstico de memoria y recuperación

| Componente previo | Crecimiento identificado | Requisito futuro |
|---|---|---|
| Ventana Candle estratégica | Acotada1560; procesamiento incremental | Preservar invariantes. |
| Lectura SQLite | Cursor streaming; índice existente | Batch y segmentos compatibles. |
| Ledger/fills/journal | O(hechos reconocidos), acumulativo | Checkpoints con autenticación/conciliación. |
| Performance journal/equity | O(hechos+valoraciones), repetición de vistas | Series paginadas/segmentadas. |
| Risk journal/evaluations | O(eventos+valoraciones), contexto acumulado | Checkpoints y verificación por segmento. |
| Reserve audits/samples | O(ticks+órdenes+transferencias), acumulativo | Paginación y snapshots parciales revisados. |
| Calendarios/colas configurados | Fuentes finitas pendientes hasta end | Fuentes streaming con cursores autenticados. |
| Snapshot/encoding | Cubre prefijo acumulado; coste proporcional | Serialización revisada, no confiar sólo en hash. |
| Recovery con controller | Reconstruye desde fuentes originales | Recuperación eficiente con prefijos verificados. |
| Manifiestos del pipeline | Persistentes, lectura legacy puede listar todos | Esta auditoría usa cursor y límite explícito. |

No se implementan esas optimizaciones ni persistencia LIVE. El informe financiero
incluye vistas repetidas; su tamaño supera ampliamente un simple total de fills.

## Validación reproducible

80 tests nuevos:78 contratos/seguridad/auditoría y2 benchmarks/regresión financiera.
Se conservan las2125 pruebas previas sin editar ni relajar, incluidos fingerprints.
Resultados finales sobre el código de entrega:

| Validación | Resultado |
|---|---|
| Dominio específico host | 78 passed; incluidos los casos de identidad Decimal y campos auxiliares. |
| Suite completa host | **2205 passed en774.23s**;2125 previos+80 nuevos. |
| Docker offline específico | **80 passed en70.50s**, sin warnings; incluye el fixture financiero y recovery. |
| Ruff format --check . | 168 archivos formateados. |
| Ruff check . | All checks passed. |
| mypy | Sin problemas en168 archivos de fuentes/tests. |
| git diff --check y --cached --check | Sin errores. |

Docker valida los80 tests de la fase, no se presenta como una segunda suite completa.
La regresión completa se acredita con el run host final. Cacheprovider se desactiva
sólo en Docker para no intentar escribir cache en/app read-only; no cambia assertions.
No se incluyen como resultados finales los runs interrumpidos o anteriores a la
última revisión de identidad de duplicados.

```sh
.venv/bin/python -m pytest tests/unit/replay/test_historical_validation.py tests/unit/replay/test_historical_benchmark.py -q -s
.venv/bin/python -m pytest -q
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
git diff --check
```

Benchmarks1/7/30 días reproducibles:

```sh
.venv/bin/python - <<'PY'
from trading_engine.replay.encoding import encoded
from trading_engine.replay.historical_validation.benchmark import benchmark_audit
for days in (1, 7, 30):
    print(encoded(benchmark_audit(days)).decode())
PY
```

Docker usa dependencias ya cacheadas, build --network none --pull=false y test
container --network none --read-only --tmpfs /tmp, ejecutando los dos archivos
específicos con -p no:cacheprovider. Sin servicios persistentes.
La fase no requiere configuración, API keys o instalación de dependencias adicionales.

## Archivos y Git

Cambios limitados al módulo nuevo, dos archivos nuevos de tests y los tres documentos
nuevos de esta fase. No cambia documentación ni tests anteriores.
Un único commit de implementación sobre la base aprobada, publicación normal sólo
feature, sin merge, squash, rebase o force push. El SHA se entrega en el informe Git;
no se inserta el hash de un commit dentro de sí mismo.

## Requisitos pendientes y 4A-5B

1. Aportar datos reales locales y pruebas independientes de procedencia/checksums.
2. Autenticar fuentes sin habilitar OBSERVED globalmente por un manifiesto.
3. Validar cobertura histórica exacta de Trade/Mark y publicación causal por registro.
4. Reconciliar inventarios funding/calendarios y referencias de precio aplicables.
5. Aportar vigencias reales de filtros/brackets1000SHIBUSDC y fees/roles del usuario.
6. Revisar adaptación económica de cada categoría hacia los contratos financieros.
7. Reconciliar datasets fragmentados/solapados antes de sumar cobertura.
8. Revisar checkpoints, paginación, segmentación y recovery con fuentes originales.
9. Mantener intraminuto, reservas/retenciones reales y prioridad de liquidación como
   faltantes. Ausencia de breaches muestreados no certifica supervivencia Binance.

Sin contradicción material con contratos financieros: ninguno se cambia ni relaja.
No hay rentabilidad histórica, fills observados, saldos reales, liquidaciones
fabricadas, descargas, conexiones Binance, transferencias o activación LIVE.
La falta de datasets/autenticación es explícita; no un resultado histórico calculado.
Detenerse para auditoría: **NO MERGE, NO DESPLIEGUE, NO TRADING**.
