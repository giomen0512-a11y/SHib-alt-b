# 4A-5D4B — asignación dinámica causal de funding

## Alcance y estado

Base exacta: `cb8c13e21a8ac5f396209d6626279d6c1113b9cf`.
Feature: `phase-4a-5d4b-dynamic-funding`.
Diseño previo: `e115710f5660db5ae1baeb1652bb50bb250c02a1`, creado antes de producción.
**Estado: READY_FOR_INDEPENDENT_AUDIT.** Las validaciones indicadas terminaron
sin fallos. Esta clasificación prepara la revisión independiente; no es PASS propio.
No es un dictamen independiente ni una autorización de merge. Runtime STOPPED.

La extensión es un consumidor opt-in de HistoricalFundingSource. Convierte tasas
fixture autorizadas en FundingEvidence MODELED solo después de demostrar exposición,
ronda, referencia exacta y finalización. No representa pagos reales del usuario.
FinancialPerformance conserva íntegra su fórmula. No cambian estrategia, 4x,
Alternativa B, DCA, TP, matching, ejecución, FinancialLedger, riesgo ni reservas.

## Resultado de arquitectura

El detalle normativo, orden de eventos y diseños rechazados están en
[DYNAMIC_FUNDING_REPLAY_SPEC.md](DYNAMIC_FUNDING_REPLAY_SPEC.md). Las cuatro decisiones
previas FND-5D4-001/002/003/004 quedan implementadas mediante:

- Agenda por cursor y próximas recepciones, sin precargar pagos de años.
- Manifiestos cerrados de ejecución fixture o prueba contractual del simulador.
- Calendario económico inclusivo independiente del inventario semiabierto de tasas.
- Gate de procedencia FIXTURE más política SYNTHETIC_FIXTURE_ONLY. MODELED describe
  el cálculo; no concede autorización a históricos reales.

`ReplayRunner(..., dynamic_funding=None)` mantiene el camino legado. El opt-in
requiere ledger y performance explícitos y rechaza otra autoridad estática de
FundingEvidence/cobertura FUNDING. Fees, transferencias y Mark mantienen sus puertos.

`dynamic_funding/contracts.py` define política, cuotas y declaraciones fixture.
`core.py` propone evidencia y valida confianza/finalidad; `models.py` conserva
estados frozen, journal y reporte. La única ampliación del adaptador es
`next_received_at(cursor)`: metadato de agenda con checks antes/después. `read()` e
`inventory_at()` conservan sus contratos y AUD-5D4-001.

## Exposición, causalidad y rondas

Se reutiliza `FinancialPerformance._exposure` y el contexto Decimal del ledger.
La exposición usa exclusivamente fills confirmados con executed_at < T. Un fill
exactamente en T produce BLOCKED, sin prioridad por UUID o secuencia de recepción.
La suma canónica por FillId no determina prioridad económica.

Una tasa tardía mantiene T. El pago se recibe cuando llegó toda la información
necesaria; no se modifica ninguna valoración anterior. La ronda se obtiene de la
exposición histórica, aunque ya esté cerrada o haya comenzado otra. Órdenes pendientes
y cantidades DCA previstas no intervienen. No se vuelve a multiplicar por 4x.

Sin watermark explícito, la decisión es PENDING_EXPOSURE. El reloj no finaliza
exposición. El manifiesto independiente incluye todos los FillIds ejecutados hasta
su frontera; faltantes esperan, adicionales contradicen. La prueba del simulador
exige cierres procesados, su latencia declarada e igualdad entre fills generados y
entregados. No se acepta un simulador de semántica desconocida ni ejecución externa
mezclada con ese mecanismo.

La protección cubre ASSIGNED y NO_EXPOSURE, incluso si no hay un pago financiero.
Un fill nuevo anterior a una frontera finalizada se rechaza antes de alterar el
ledger. Los duplicados conocidos siguen pasando por su validación original de
contenido. También se validan manifiestos posteriores cuando ya no quedan tasas
pendientes: no pueden omitir fills conocidos ni contradecir inventarios anteriores.
Este último caso se añadió durante revisión y tiene regresión propia.

## Referencia y cobertura

La referencia procede únicamente de FundingRate.mark_price con contrato fixture
explícito de correspondencia exacta con T. Sin precio o sin ese contrato se conserva
REFERENCE_NOT_AVAILABLE. No hay fallback a Close, bid/ask, aproximación o Mark futuro.

FundingInventory continúa representando [start,end); CoverageEvidence, [start,end].
Un calendario independiente debe declarar el extremo inclusivo. Se exige inventario
publicado compatible, cada ocurrencia recibida, referencia exacta, prueba de
finalización y ASSIGNED o NO_EXPOSURE válido, sin contradicciones. La cobertura lista
exactamente los EventIds de pagos reconocidos; NO_EXPOSURE no genera un pago cero.

Una tasa en end puede asignarse si está preparada y disponible. Si el inventario
que contiene esa tasa se publica después del Replay, no se inventa cobertura. La
métrica puede seguir NOT_AVAILABLE. Un inventario incompleto o una consulta vacía
sin declaración independiente tampoco demuestra funding cero.

COVERAGE_PUBLISHED en el reporte del componente significa al menos una ventana
publicada y detallada en coverage. FinancialPerformance conserva la autoridad para
comprobar cobertura de toda la ventana financiera; el estado del componente no
certifica equity por sí mismo. Fees, capital, transferencias y Mark siguen siendo
requisitos independientes de sus métricas.

## Journal, recuperación y confianza

Cada decisión conserva tasa/EventId, T, recepción, fuente/dataset, política,
inventario, ronda, FillIds, cantidad, referencia, estado, motivo y prueba de finalización.
Los cursores de tasas, manifiestos y calendarios son independientes del cursor
financiero estático. Repetir la misma propuesta no agrega pagos o decisiones.

Snapshot añade identidad, sesión opaca y estado funding, omitidos cuando están OFF.
La sesión no entra en el checksum, pero se exige por identidad de objeto al restaurar.
La restauración ON reconstruye desde las fuentes originales y compara el snapshot
completo. Después siguen ejecutándose los validadores originales de simulador,
ledger, FinancialPerformance y riesgo: no se reemplazan por un checksum coherente.
La separación de agenda estática/dinámica conserva la comprobación de ambas.

Los checks cubren consultas, exposición, construcción de evidencia, observe, VALUE,
confirmación del tick, cierre de run, report y restauración. Los cambios internos
del tick son provisionales hasta terminar riesgo y barreras finales. Una excepción
restaura el snapshot anterior completo y se propaga con su identidad original;
no se inventa un conflicto contable por una fuente inválida. Los checks de reporte
también se realizan después de construir el resultado. No se promete atomicidad
multihilo ni con procesos externos.

Después de un run fallido, el caller debe abortar esa ejecución. El snapshot conserva
el prefijo y su reloj real. No se presenta el end solicitado como alcanzado; una
sesión inválida impide report(). No se permite recuperación entre procesos con
sesiones recreadas o tokens sustituidos.

## Trazabilidad de pruebas

Las 51 pruebas nuevas se encuentran en `test_dynamic_funding.py` y
`test_dynamic_funding_replay.py`. Los niveles se distinguen expresamente: componente
con ledger real y fuentes preparadas, integración del runner o controles previos.

| Casos | Evidencia principal | Nivel |
| --- | --- | --- |
| A, B | LONG10, tasas ±0.0001 y 0, signos y cargo único; fórmula existente | Componente + FinancialPerformance |
| C–F | Cero, BUY antes/después, empate económico BLOCKED | Componente |
| G | Mark ausente y precio sin contrato no generan pago | Componente |
| H, I | Duplicado de fuente coalescido; contradicción rechazada por preparación | Adaptador + componente |
| J | Sin inventario o sin finalización no se completa cobertura | Componente |
| K–Q | Cierres antes/después, dos rondas, cuatro BUY, DCA no ejecutada y TP parcial | Ledger + componente; fills/pedidos en integración |
| R, S | Fill tardío contradice NO_EXPOSURE o ASSIGNED; prefijo intacto | Componente + guard previo al ledger |
| T | Inventario publicado tarde agrega cobertura sin reescribir decisiones | Componente |
| U | Ocurrencia exactamente en end exige tasa explícita e inventario disponible | Componente |
| V, AC | Continuo, fragmentado y restaurado; 13 fills producidos dentro de run | Runner + simulador |
| W | Cursor, cantidad, journal, prueba y sesión manipulados con checksum recalculado | Runner + reconstrucción |
| X | Cierre durante evidencia/observe/VALUE/commit/después de riesgo; rollback completo | Runner |
| Y | SOURCE_DECLARED no puede publicar aunque el cálculo se etiquete MODELED | Gate real de procedencia |
| Z | Mismos originales, dos verificaciones: IDs económicos y eventos financieros iguales | Preparaciones independientes |
| AA | Sin capital/fees/Mark no se completan métricas dependientes; cero demostrado sigue siendo cero | FinancialPerformance existente |
| AB | Campo opt-in OFF omitido y fingerprints previos de comportamiento preservados | Integración + regresiones congeladas |
| AD | received_at > end conserva cursor sin consumir ni inventar pago | Componente |
| AE | Declaración de calendario contradictoria recibida tras prefijo no cambia estado confirmado | Componente |

Se prueba idempotencia de decisiones y errores originales del constructor. La
continuidad/restauración integral se verifica sobre el escenario de simulación y
sus cortes antes/después de funding; no se afirma un producto cartesiano de cada
fixture A–AE con todos los cortes, fuentes y fallos posibles. La preparación ya
rechaza tasas contradictorias dentro de su cutoff: no se afirma que esos históricos
hayan sido admitidos primero por el runner. El calendario tardío de AE es el
inventario económico; la preparación histórica original permanece inmutable.

## Validación ejecutada

Antes de producción: 101 pruebas en total (99 de funding/AUD-5D4-001 y dos contratos
de equivalencia legada) pasaron en 14.03 s. Se congelaron 208 archivos src/tests antes de editar.
Después: 51 pruebas específicas 5D4B pasaron en 51.91 s. Un grupo previo de funding
pasó 144 pruebas antes de la última ampliación de finalidad; no sustituye la suite
final sobre el código definitivo.

La suite completa host terminó con **2807 passed**, 0 errores, 0 fallos y 0 skips,
en **1770.05 s (29 min 30 s)**. Es una ejecución nueva de esta versión, no el total
reutilizado de la fase anterior. **La suite completa Docker offline terminó con 2807 passed en 2062.18 s
(34 min 22 s)**. Ambos procesos terminaron con exit code 0. Se incluyen las mismas
51 pruebas nuevas y 2756 previas; no se alteró el resultado esperado de esas
regresiones financieras. Evidencia estructurada y hashes de logs:
[dynamic_funding_validation.json](fixtures/dynamic_funding_validation.json).

| Grupo incluido en pytest completo host | Pruebas aprobadas |
| --- | --- |
| 5D4B (componente / runner) | 39 / 12 |
| Funding preparatorio / AUD-5D4-001 | 79 / 20 |
| AUD-5D3-001 | 22 |
| FinancialLedger / FinancialReplay | 49 / 27 |
| FinancialPerformance / PerformanceReplay | 75 / 33 |
| SimulatedExecution / SimulatedReplay | 60 / 24 |
| IsolatedMarginRisk / MarginRiskReplay | 86 / 30 |
| ReserveTransferSimulation / ReserveTransferReplay | 39 / 22 |
| Runner / Snapshots | 52 / 39 |

Ruff format --check y Ruff check aprobaron los 206 archivos Python; mypy --strict
src tests aprobó los 206 archivos. Los mismos controles aprobaron en Docker offline
con filesystem readonly. git diff --check aprobó el diff de trabajo y el index.
 Las ejecuciones generales interrumpidas para añadir
la comprobación de manifiestos posteriores no se contabilizan como aprobadas.

Host: Python 3.12.14. Docker: Python 3.12.15, pytest 8.4.2, Ruff 0.13.3, mypy 1.18.2.
Imagen local `shib-alt-b:test`:
`sha256:654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf`.
Host ejecuta pytest bajo seccomp que deniega sockets IPv4/IPv6. Docker usa
`--pull=never --network=none --read-only`, repositorio montado readonly y tmpfs temporal.
No se descargaron datos, imágenes o dependencias para las pruebas.

## Fingerprints y archivos afectados

[dynamic_funding_source_manifest.json](fixtures/dynamic_funding_source_manifest.json)
conserva los 208 hashes previos y la comparación exacta: 204 archivos permanecen
idénticos; cambian únicamente los tres módulos productivos existentes mencionados
y la prueba estática de fingerprints. El componente, pruebas y benchmark son nuevos.
FinancialPerformance, FinancialLedger, simulador, estrategia y fórmulas no cambian.

El JSON histórico de 202 fingerprints permanece idéntico. La prueba estática ahora
comprueba 200 archivos actuales contra los hashes originales y, para runner/models,
los bytes históricos archivados más sus hashes actuales separados. El whitelist es
exactamente esos dos módulos; no se regenera el comportamiento financiero esperado.
Los archivos `.py.txt` son datos históricos de verificación y no se ejecutan.
La migración está en `dynamic_funding_source_changes.json` y queda visible en el diff.

## Medidas de recursos

Resultados crudos y método:
[dynamic_funding_benchmarks.json](fixtures/dynamic_funding_benchmarks.json).
Benchmark reproducible: `PYTHONPATH=src python -m tests.unit.replay.bench_dynamic_funding`.
Son fixtures sintéticos con tres ocurrencias/día, recepción dos horas tarde, una
ronda por día y manifiestos independientes. Se miden calendarios completos y una
tasa omitida. No es una distribución oficial de Binance.

Las cuotas máximas explícitas son 512 tasas, 2048 decisiones, 4096 fills, 512
manifiestos por puerto y 44640 minutos por ventana. Superarlas falla antes de confirmar
el tick. La fuente conserva un bloque de hasta 1440 filas, no un historial multianual
en una tupla de pagos.

| Días | Inventario | Tasas / rondas | Consumo s | Fuente Python B | Agenda pico B | Journal pico B | Snapshot funding B | Snapshot JSON B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | completo | 3 / 1 | 0.032931 | 10968 | 5924 | 11657 | 14004 | 20112 |
| 1 | incompleto | 2 / 1 | 0.023638 | 9678 | 4183 | 8321 | 10068 | 13526 |
| 7 | completo | 21 / 7 | 1.181743 | 36404 | 5952 | 77231 | 81738 | 145205 |
| 7 | incompleto | 20 / 7 | 0.792433 | 35114 | 5952 | 73607 | 77370 | 135519 |
| 30 | completo | 90 / 30 | 13.927961 | 133954 | 5952 | 376622 | 394745 | 884932 |
| 30 | incompleto | 89 / 30 | 7.560006 | 132664 | 5952 | 371894 | 388721 | 863378 |

La estimación Python recorre dataclasses, diccionarios y colecciones con deduplicación;
para otros objetos usa su tamaño básico. Excluye buffers nativos SQLite e internos
opacos; no representa toda la RAM. Tracemalloc mide el consumidor, después de preparar fixtures; RSS es el pico
acumulado del proceso. Los tiempos incluyen instrumentación y muestreo de memoria,
no son garantías de latencia. Snapshot del componente significa FundingState,
no ReplaySnapshot completo. Los hashes del benchmark incluyen bindings locales y
se registran como evidencia de esa ejecución, no como golden portátil entre rutas.

El runner completo se midió aparte sobre 38 minutos de evaluación, warmup de 1560
minutos y 13 fills: 16.27 s instrumentados, pico tracemalloc 9,034,087 bytes,
ReplaySnapshot Python 2,018,978 bytes, journal funding 4,462 bytes. Su agenda de
104 bytes es el estado final, no su pico. No se extrapola ese caso a 30 días.
Los journals/valoraciones legados siguen creciendo; no se certifica soporte
financiero multianual 2024–2026 ni un presupuesto RAM universal.

## Resultados financieros sintéticos

Los ejemplos verificados LONG10 × precio100 × tasas ±0.0001 producen respectivamente
-0.10/+0.10 USDC mediante FinancialPerformance, sin factor adicional 4x. NO_EXPOSURE
no crea FundingEvidence. Referencia, finalización o cobertura faltantes conservan
NOT_AVAILABLE según el contrato de la métrica. No se presentan datos reales, pagos
privados ni rentabilidad de usuario.

En el runner integrado, la exposición histórica en T fue
2.1602809697920099032583364773558151768587806112275 contratos, referencia 100 y tasa
0.0001; FinancialPerformance produjo
-0.021602809697920099032583364773558151768587806112275 USDC MODELED.
T = 2026-01-02T12:06:30Z; pago recibido a 12:08:30Z. Fees/equity permanecieron
NOT_AVAILABLE. El unrealized final fue cero porque la posición ya estaba cerrada,
no porque se hubiera inventado un Mark. Evidencia:
[dynamic_funding_synthetic_financials.json](fixtures/dynamic_funding_synthetic_financials.json).

## Riesgos, observaciones y límites de la revisión

- Los manifiestos y calendarios son declaraciones de un fixture; no autentican Binance.
- Una contradicción posterior aborta la extensión del prefijo, sin revisar dinero pasado.
- La restauración reconstruye causalmente y puede ser costosa dentro de sus cuotas.
- Los límites son de objetos y ventana; no certifican el peor caso RAM del runner completo.
- No existe persistencia portable de sesiones ni garantía multihilo.
- El cálculo usa únicamente la referencia exacta declarada del fixture; otra fuente
  Mark requeriría contrato e implementación independientes.
- La matriz no agota combinaciones de cortes, errores y escenarios. Sigue siendo
  necesaria la auditoría independiente sobre el diff final.
- La API REST GitHub mediante gh devolvió Forbidden. Los SHA se consultan por el
  protocolo Git del origin en github.com; no se afirma haber inspeccionado checks
  remotos de CI o protecciones de rama mediante REST.

No se autorizan ni se implementan LIVE, SHADOW, Binance privado, órdenes o transferencias reales,
Add Margin real, Telegram operativo, servicios, despliegue VPS o fase posterior.
Main no se modifica. La publicación se limita a un push normal de la feature. No se hace merge ni se
inicia una fase posterior. La autorización y autenticación de datos reales continúan
pendientes; no se confunden con hallazgos bloqueantes de este alcance fixture.


## Reproducción y control Git

La suite completa se invoca con `python -m pytest -q -p no:cacheprovider`; incluye
los módulos funding, AUD-5D4-001, AUD-5D3-001, FinancialLedger, FinancialPerformance,
SimulatedExecution, ReplayRunner/snapshots, riesgo y reservas. Los controles son
`ruff format --check src tests`, `ruff check src tests`,
`mypy --strict src tests` y `git diff --check`.

En Docker se monta esta feature sobre `/app`, se fija `PYTHONPATH=/app/src`, se
utiliza la imagen local anterior y se ejecuta con red deshabilitada y filesystem
readonly. Ruff usa `RUFF_NO_CACHE=true`; mypy escribe su caché únicamente en tmpfs.
No se reconstruye una imagen con descargas. Las pruebas tienen sus temporales en
ese tmpfs y no utilizan artefactos de una ejecución anterior.

La revisión Git confirmó origen `github.com/giomen0512-a11y/SHib-alt-b.git`, base y
main/origin-main iguales al SHA aprobado, rama nueva desde esa base y árbol limpio
antes de comenzar. `git fsck --connectivity-only --no-dangling` terminó sin errores.
Se publican diseño e implementación como commits descriptivos de feature, sin
merge, squash, rebase o force push. El SHA final se obtiene de la referencia remota
verificada y se entrega junto al diff exacto en el informe de publicación.


## Dictamen de entrega técnica

READY_FOR_INDEPENDENT_AUDIT para la extensión fixture opt-in descrita. No se han
detectado fallos bloqueantes en las comprobaciones ejecutadas. La cobertura y las
limitaciones anteriores forman parte del alcance revisable; no se anuncia aprobación
independiente, FF_ONLY_APPROVED ni autorización operativa. Main permanece en la base
aprobada. Tras publicar se detiene el trabajo hasta nueva instrucción.
