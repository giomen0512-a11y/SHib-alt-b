# Histórico verificado con cotizaciones y ejecución modeladas — Fase 4A-5D2

Base: `ae1a6aafac4ff09229fd14cdd64e643f9c6a3b34`. Sólo OFFLINE; runtime **STOPPED**.
Fuentes y escenarios de esta fase: **SYNTHETIC_FIXTURE**, sin autenticación de Binance.

## Arquitectura y activación

`HistoricalQuery / SegmentedHistoricalQuery → PreparedTradeHistory → ModeledQuotes →
ReplayRunner → SimulatedExecution → StrategyCoordinator / AltBEngine`.

ModeledQuotes recibe el puerto estructural `CandleHistory`: `session`, `conflicts(symbol)`
y `read(symbol, start, end) -> Generator[Candle, None, None]`. El protocolo ahora permite
comprobación estructural en runtime; no añade métodos. SQLiteHistory y PreparedTradeHistory
son compatibles sin herencia ficticia, copia de bases ni motor alternativo de Quotes.
La especialización de confianza de PreparedTradeHistory es explícita; otros proveedores
del puerto conservan el diagnóstico legado de datos inválidos. El import del adaptador
se difiere para evitar el ciclo con ReplayRequest durante la carga de módulos.

Activación explícita, con una única instancia de QuoteSource:

```python
with PreparedTradeHistory(registry_path, dataset_ids, request, ...) as history:
    quotes = ModeledQuotes(history, execution_policy, history.evidence.identity)
    simulator = SimulatedExecution(simulation_policy, quotes)
    runner = ReplayRunner(history, request, quotes=quotes, simulator=simulator)
    report = runner.run()
```

`simulation_policy.pricing` debe coincidir con `execution_policy`. `request.allow_entries`
sigue siendo opt-in explícito de simulación. Sin Quotes, el default continúa NoQuotes.
No se activa automáticamente ningún componente financiero.

## Identidad económica, evidencia y sesión

Con PreparedTradeHistory se exige exactamente `dataset_identity == history.evidence.identity`.
No se admiten un DatasetId aislado ni una etiqueta arbitraria como sustitutos. Esta comparación
se repite al consultar Quotes, consultar identidad y acceder a history_session. La propiedad
session verifica además cierre, TTL e invalidación de la sesión segmentada.

La identidad de cálculo de la nueva fuente utiliza un descriptor canónico:

```text
prepared-trade-inputs-v1:SHA256(encoded((
    evidence.request, evidence.query_start, evidence.cutoff, evidence.bindings,
    evidence.query_digest, evidence.candle_digest
)))
```

El descriptor identifica solicitud, cutoff, datasets/auditorías seleccionados, declaración,
adquisición, hashes de originales y digest exacto de velas. Se calcula una sola vez; no se
recorre la historia por Quote. Se introduce como primer elemento del algoritmo existente
`modeled-from-1m-v1`, junto con símbolo, policy.identity y versión temporal
`last_latency_eligible_close_with_continuity`.

El alcance STRICT/SEGMENTED se conserva en PreparedTradeEvidence y en el dataset_identity
de cada ModeledQuoteEvidence. Se exige conservar esa evidencia junto al reporte. No se incorpora
ese alcance al descriptor económico: las mismas velas, selección, solicitud y políticas deben
producir los mismos UUID de ejecución. El simulador deriva OrderId/FillId de su identidad
completa aprobada; no cambia esa regla. Cambiar los UUID únicamente por el alcance de
verificación también cambiaría el orden canónico de suma por FillId y puede cambiar el último
dígito de un VWAP Decimal de 50 dígitos. La identidad de cálculo estable evita ese efecto sin
modificar estrategia, precisión, matching o redondeos.

Las evidencias de ambos modos siguen siendo distintas; las huellas que incluyen sus Quotes
pueden diferir. Esta identidad compartida no concede confianza ni permite recovery entre
sesiones: runner y Quotes deben compartir el mismo token opaco por identidad (`is`), el
simulador debe recibir la misma instancia Quotes y el snapshot debe pertenecer a esa sesión.
Una preparación nueva, incluso con idéntica identidad económica, rechaza el snapshot anterior.
No existe recovery persistente de Replay entre procesos.

Con SQLiteHistory se conserva exactamente el primer elemento dataset_identity proporcionado
por el caller y el algoritmo anterior de identidad. Sus nueve fingerprints congelados no
cambian. Una comparación de adaptadores puede asignar al SQLite sintético el descriptor
económico verificado de las mismas velas: así también coincide la identidad del simulador y
se comparan resultados exactos, manteniendo distinta la evidencia de procedencia de las Quotes.
No se exige igualdad de reportes completos cuando difieren los metadatos de evidencia.

## Causalidad, latencia y spread

Se conserva sin cambios `model_closed_candle` y el algoritmo de selección:

```text
reference_close = floor_minute(t - quote_latency_ms)
latest_closed = floor_minute(t)
available_at = reference_close + quote_latency_ms
expires_at = reference_close + freshness_limit_ms
available_at <= t <= expires_at
Quote.timestamp = t
```

La ventana streaming va de reference_close menos un minuto hasta latest_closed. Exige
continuidad completa. Un minuto posterior a reference_close sólo verifica continuidad;
su cierre no sustituye la referencia mientras siga bajo latencia. Antes del límite de
latencia se puede seleccionar la referencia anterior si sigue vigente; no se fuerza una
espera ni se adelanta el cierre más reciente. Vencimiento exacto es inclusivo.

Para `P = reference_candle.close`, `s = spread_bps / 10000`:
`bid = P * (1 - s/2)`, `ask = P * (1 + s/2)`. Spread se aplica una vez. No se usan high/low
como bid/ask observado. Quote es **MODELED_FROM_1M**; Fill es **MODELED_EXECUTION**.
La adquisición del dataset es procedencia, sin autorización de lookahead. Las comprobaciones
SHA-256 del adaptador pueden verificar un bloque completo autorizado; sólo las velas del rango
cerrado solicitado se entregan al modelo de precios. Verificar un bloque no vuelve disponible
económicamente el resto de sus velas.

Se conservan Decimal exacto con contexto local 50, UTC aware y contrato
**BINANCE_USDM_FUTURES / 1000SHIBUSDC PERPETUAL USDC / 1m**. No hay conversión por 1000.

## Ausencia económica y pérdida de confianza

| Categoría | Comportamiento |
| --- | --- |
| Referencia ausente o gap conocido en la ventana | QuoteEstimate sin evidencia, reason explícito de gap; historia sigue válida. |
| Referencia vencida o latencia no transcurrida | Quote ausente según algoritmo/fórmula aprobados; sin interpolación. |
| Identidad de dataset o contrato incompatible | ValueError verificable; constructor/consulta bloqueados. |
| Historia cerrada, TTL vencido, sesión invalidada/cerrada, fuente cambiada | Excepción de confianza conservada; ReplayRunner interrumpe. |
| Cache preparado, índice, cursor, payload o raíz SHA-256 inválidos | Fallo del bloque y cierre del adaptador; ninguna Quote ni fill ficticio. |
| Lectura fuera de solicitud/warmup o cutoff | Excepción conservada; nunca se declara gap histórico del futuro. |
| Otros ValueError/TypeError/SQLiteError/errores Decimal durante lectura preparada | Se propagan; no se capturan como ausencia de liquidez. |

El runner conserva su tratamiento aprobado: ValueError, TypeError y SQLiteError registran
interrupción con clase y mensaje; otras excepciones propagadas bloquean al caller con su
error original. No se continúa simulando. Tras SOURCE_CHANGED o SESSION_EXPIRED, la sesión queda inválida y
consultas posteriores informan VERIFICATION_NOT_AVAILABLE. No se convierte ninguno de esos
fallos en missing_quote. Gaps legítimos conservan la invalidación/recuperación estratégica
vigente y nunca hacen forward-fill ni inventan operaciones.

## Cutoff y truncamiento

Además del rango acotado de read, toda consulta preparada exige
`t <= history.cutoff` y `t <= history.request.end`. Esto bloquea incluso una consulta un
microsegundo después del cutoff cuyo floor_minute todavía estaría dentro de él. Se mantienen
los límites inferiores de read y sólo se entregan minutos completos ya cerrados.

Un cutoff parcial conserva cobertura INCOMPLETE y permite consumir su prefijo con
`runner.run(until=cutoff)` —until acepta UTC subminuto—. No se rechaza automáticamente el
constructor sólo porque request.end exceda cutoff: eso impediría ese consumo deliberadamente
truncado. Solicitar ejecución más allá del contrato interrumpe al primer read/Quote prohibido;
no ofrece un replay completo de futuro desconocido. La consulta directa de Quote fuera del
cutoff falla inmediatamente. La ejecución no se anuncia como completa al ejecutar sólo el prefijo.

## Matching, estrategia y finanzas

SimulatedExecution y ReplayRunner permanecen sin modificaciones. Aceptación, latencias,
Base MARKET, DCA LIMIT, TP SELL reduce-only, parciales, shortfills, cancelaciones/reemplazos,
volumen compartido, cola, ambigüedad, idempotencia y orden de entrega conservan sus contratos.
MARKET aplica slippage una vez sobre ask; LIMIT conserva su buffer de elegibilidad y precio
al límite. No se cambia elegibilidad ni se prueba ejecución real con un toque OHLC.

AltBEngine conserva warmup 1560, checkpoints 15m, filtros, vetos, Base T+120, una ronda activa,
LONG ONE-WAY ISOLATED 4x, 1200.00 USDC, Base + DCA1–DCA7, TP 0.80%, SL/trailing OFF.
FAVORABLE/BASE/ADVERSO son hipótesis; ningún perfil prueba fills de Binance.

FinancialLedger, FinancialPerformance, IsolatedMarginRisk y ReserveTransferSimulation
siguen opt-in independiente. Trade no autentica MARK_PRICE, FUNDING, FEES,
MAINTENANCE_BRACKETS, CAPITAL, BALANCE o EXECUTION_OBSERVED. No se deriva Mark observado del
Close. La evidencia preparada mantiene NOT_AUTHENTICATED y
NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES; resultados financieros completos NOT_AVAILABLE.

## Validación y medición

`test_historical_modeled_execution.py` añade congelaciones pre/post, puerto estructural,
ambos modos, límites temporales, gaps, cutoff parcial, identidad/sesión, fallos de confianza,
ronda conciliada, MARKET/LIMIT, ausencia de Quote, slippage único, Decimal hostil, red
prohibida, opt-in y equivalencia exacta continuo/fragmentado en trece cortes por modo.
La ronda reutiliza el fixture aprobado existente, incluidos sus valores de política; no se
ajustan para producir nuevos resultados. La comparación de los tres perfiles usa sus valores
aprobados sin cambiar latencias ni requisitos para conseguir fills.

`python -m tests.unit.replay.historical_modeled_execution_benchmark --minutes 20|1440|10080
--mode STRICT_FULL|SEALED_SEGMENT_SESSION` mide un proceso/escenario. Reporta registro,
validación inicial de sesión, preparación inclusiva, Quotes separadas, Replay con fuente y
simulador explícitos, restauración y consumo restante, RSS de proceso, tamaños de archivos
retenidos y cargas de bloques verificadas por fase. Genera CSV incremental; no carga años
en RAM. El mercado plano de medición no emite señales ni activa entradas; la ejecución con
órdenes se valida en la ronda sintética aparte. RSS incluye todo el proceso, no memoria
exclusiva de Quotes; no se afirma ventaja de velocidad entre modos a partir de estas muestras.

Resultados, trazas, fingerprints y limitaciones: [PHASE_4A_5D2_REPORT.md](PHASE_4A_5D2_REPORT.md).
La entrega se detiene para auditoría: sin merge, despliegue, LIVE, trading ni siguiente fase.
