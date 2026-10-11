# Informe Fase 4A-5D2 — integración histórica con Quotes y ejecución modeladas

Repositorio: `giomen0512-a11y/SHib-alt-b`.
Base exacta y origin/main verificada: `ae1a6aafac4ff09229fd14cdd64e643f9c6a3b34`.
Rama: `phase-4a-5d2-historical-modeled-execution`.
Commit de implementación: `a8bcf1afa5ab1d423b419ec866858c00c8e32c4e`.
El SHA final de entrega documental/publicación se comunica con `git rev-parse HEAD` al entregar.
Runtime **STOPPED**; sólo pruebas OFFLINE. **Sin merge, despliegue, LIVE ni trading**.

## Arquitectura y alcance implementados

HistoricalQuery / SegmentedHistoricalQuery → PreparedTradeHistory verificado → ModeledQuotes
→ ReplayRunner → SimulatedExecution → mismo StrategyCoordinator / AltBEngine.

Sólo cambian dos archivos de producción. CandleHistory conserva exactamente sus tres miembros;
se añade comprobación estructural runtime. ModeledQuotes cambia su anotación y validación de
SQLiteHistory concreto a CandleHistory, con tratamiento explícito de confianza para historia
preparada. No se modifica PreparedTradeHistory, SQLiteHistory, runner, simulador, matching,
estrategia, filtros, riesgo, reservas ni contabilidad. No hay dependencias nuevas.

Dataset_identity preparado debe ser history.evidence.identity, comprobado en construcción y
consumo. Una identidad arbitraria/incompatible bloquea. El token de sesión por identidad sigue
vinculando runner, Quotes y snapshot; el simulador recibe la misma instancia Quotes y política
compatible. Ninguna preparación nueva hereda confianza de otra.

La identidad de cálculo de la nueva fuente usa el descriptor canónico versionado de solicitud,
cutoff, bindings seleccionados y digests de consulta/velas. El alcance de verificación se
conserva distinguible en PreparedTradeEvidence y dataset_identity de cada Quote. Esto permite
UUID y resultados económicos iguales entre modos sin cambiar el algoritmo aprobado de IDs.
Durante las pruebas se detectó que usar directamente una identidad dependiente del modo
cambiaba los UUID y el orden de suma por FillId, alterando últimos dígitos Decimal de VWAP/TP.
La solución evita esa diferencia desde la identidad de inputs económicos; no cambia precisión,
redondeos, estrategia o simulador. Es un contrato nuevo exclusivo de fuentes preparadas.

SQLiteHistory conserva exactamente su identidad anterior y su tratamiento legado de datos
inválidos. La comparación de adaptadores identifica explícitamente el mismo dataset económico
en SQLite; no lo convierte en una historia preparada ni le atribuye verificación segmentada.
La evidencia de Quotes permanece distinta. Detalle completo:
[HISTORICAL_MODELED_EXECUTION_SPEC.md](HISTORICAL_MODELED_EXECUTION_SPEC.md).

## Archivos

Añadidos:

- `docs/HISTORICAL_MODELED_EXECUTION_SPEC.md`
- `docs/PHASE_4A_5D2_REPORT.md`
- `docs/fixtures/historical_modeled_round_trace.json`
- `docs/fixtures/historical_modeled_benchmarks.json`
- `tests/unit/replay/test_historical_modeled_execution.py`
- `tests/unit/replay/historical_modeled_execution_benchmark.py`
- `tests/unit/replay/fixtures/historical_modeled_baselines.json`

Modificados:

- `src/trading_engine/replay/ports.py`: sólo @runtime_checkable.
- `src/trading_engine/replay/execution_model/quotes.py`: puerto, identidad y fail-closed preparados.
- `docs/HISTORICAL_REPLAY_ADAPTER_SPEC.md`: reemplaza limitación de 5D1 por enlace a integración.
- `docs/EXECUTION_MODEL_SPEC.md`: continuación 5D2.
- `docs/SIMULATED_EXECUTION_SPEC.md`: enlace al nuevo origen compatible.

Ningún test previo fue modificado. El informe 5D1 permanece como registro histórico.

## Tiempo, cutoff, gaps y fallos

reference_close = floor_minute(t − quote_latency_ms); latest_closed = floor_minute(t).
available_at = reference_close + latencia; expires_at = reference_close + frescura.
Vigencia inclusiva en ambos límites; Quote.timestamp = t. Antes de la latencia del último
cierre sólo puede usarse el anterior si conserva continuidad y frescura. En T+120 nunca se
usa una vela que cierre después de T+120. La adquisición futura del fixture no adelanta su
disponibilidad económica.

Spread sobre Close una sola vez: bid = P(1 − spread/20000), ask = P(1 + spread/20000).
Slippage permanece exclusivamente en el simulador. Con P=100 y política BASE del fixture:
bid=99.95, ask=100.05, MARKET BUY=100.080015; LIMIT ejecutado al precio protegido aprobado.

Preparado exige t <= cutoff y t <= request.end, incluso si el floor_minute de t todavía cae
dentro del prefijo. read conserva warmup/solicitud, sólo minutos cerrados y bloquea fuera del
cutoff. Un cutoff parcial de 90 segundos permite consumir el primer minuto y run(until=cutoff);
una consulta un microsegundo posterior falla. Se permite construir un runner para consumir
deliberadamente el prefijo; run completo interrumpe al primer acceso prohibido. No se declara
futuro no adquirido como gap confirmado ni se anuncia completo un replay truncado.

Gaps de referencia o intermedios devuelven Quote ausente con reason; la historia sigue válida.
No interpolación, forward-fill, vela sintética, fill ficticio o precio constante asumido.
El runner mantiene sus políticas de invalidación/recuperación de warmup y señales.

Bloquean: identidad/selección incompatible, otro contrato, historia cerrada, sesión expirada,
invalidada o cerrada, fuente segmentada cambiada, payload/cursor/índice/cache/raíz SHA-256
adulterados, lectura fuera de solicitud/cutoff y errores inesperados de lectura preparada.
No se convierten en missing_quote. Los fallos habituales registran interrupción original;
otras excepciones propagadas bloquean al caller. Después de expirar/cambiar fuente, el estado
inválido informa VERIFICATION_NOT_AVAILABLE en consultas posteriores.

STRICT_FULL conserva FULL_SOURCE_VERIFIED al preparar el snapshot y no revalida cada original
por Quote. SEALED_SEGMENT_SESSION conserva SEGMENT_VERIFIED / SOURCE_NOT_REVALIDATED y checks
de vigencia/estabilidad de sesión. No se anuncia verificación segmentada como autenticación global.

## Ronda sintética completa y perfiles

Se reutiliza `round_candles` y la política BASE del fixture previo de ronda, incluida su latencia
Quote de 0ms; los fingerprints de ese escenario se congelaron antes de editar. No se optimizan
parámetros ni se modifica estrategia para conseguir fills. Es un mercado local deliberadamente
sintético, con OHLC planos después de la Base y presupuestos compartidos de volumen.

Señal confirmada 2026-01-02 12:00 UTC → Base MARKET emitida exactamente 12:02 UTC (T+120)
→ aceptación 12:02:00.100 → fill Base 12:04 → P0/escalera → DCA1 parcial 12:06–12:09
→ DCA2 12:10 → resto DCA hasta 12:15 → TP reduce-only parcial 12:18/12:19/12:20
→ ronda CLOSED, exposición conciliada exactamente cero.

Resultado: **14 órdenes**, **14 fills**, **47 hechos modelados**, **5 cancelaciones confirmadas**
y **6 TP sucesivos** (cinco cancelados y uno ejecutado). DCA1 tiene cuatro fills; TP terminal
tiene tres fills. No se duplica capacidad de volumen ni IDs. Se registran **60 casos de no
ejecución**, sin inventar fills en minutos inelegibles.

Cada hecho incluye tiempo de ocurrencia/recepción, secuencia, cantidad, precio cuando procede,
OrderId/FillId/IntentId, lado, estado y MODELED_EXECUTION. Intenciones y evidencia de precios
se conservan junto a evidencia preparada y política en la
[traza completa](fixtures/historical_modeled_round_trace.json). Los timestamps/valores exactos
están en JSON; no se redondean para calcular resultados. El digest de esa ejecución de traza es
`e61c343d957d857b726d9e275661e83b1ce8d169bc7b0f5c73511667741c4382`, checksum de snapshot
`9f96e41caf08c0f10c5690629a5dac2ce091c37847a23deb362fff1dca04c479`.

La comparación independiente de FAVORABLE, BASE y ADVERSO usa los perfiles existentes sin
cambiar sus valores para autorizar ejecuciones. Se comparan exactamente selección de referencia,
bid/ask, timestamps, disponibilidad, elegibilidad, intenciones, hechos, cantidades/precios,
posiciones y terminalidad contra SQLite. Los requisitos adicionales de ADVERSO y su política
de parciales pueden abstenerse; ninguna hipótesis demuestra fills de Binance.

| Perfil aprobado | Spread bps | Slippage bps | Latencia Quote ms | Órdenes aceptadas | Fills | Rondas cerradas | Cancelaciones confirmadas |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FAVORABLE | 2 | 1 | 0 | 13 | 11 | 1 | 4 |
| BASE | 10 | 3 | 250 | 14 | 14 | 1 | 5 |
| ADVERSO | 30 | 8 | 1000 | 12 | 6 | 0 | 3 |

ADVERSO conserva una ronda activa al terminar el fixture: se registran 70 abstenciones por
partial_execution_withheld y 3 por additional_quote_does_not_support_limit. No se fuerza el
cierre. Ningún perfil interrumpe por integridad. Los valores y motivos completos están en
profiles_with_approved_values de la traza. Esta tabla usa las latencias originales de cada
perfil; la ronda detallada anterior usa exactamente la política de la ronda legada con latencia
Quote 0ms. Los resultados no se interpretan como rentabilidad ni como un orden de rendimiento.

No se activan Ledger, Performance, IsolatedMarginRisk ni ReserveTransferSimulation.
Resultado financiero: FINANCIAL_RESULTS_NOT_AVAILABLE. No hay Mark observado derivado, capital
presunto, autenticación ni rentabilidad histórica real.

## Equivalencia y reanudación

Las Candle de ambos adaptadores coinciden exactamente, sin factor 1000. Con el mismo descriptor
económico, la identidad del simulador coincide y se comparan todos los campos económicos y
temporales, incluyendo estados y emisiones. Sólo se normalizan metadatos de fuente y IDs
derivados cuando procede; no se normalizan ni toleran diferencias Decimal o timestamps.

STRICT_FULL y SEALED_SEGMENT_SESSION sobre el mismo registro/selección producen exactamente
los mismos Orders, Fills, hechos y estado final. Los tokens de sesión, alcance de verificación,
dataset_identity de evidencia y fingerprints que la incluyen permanecen distintos. Un snapshot
de otro modo/preparación se rechaza aunque la identidad económica coincida.

Dentro de cada misma historia y sesión, trece cortes cubren warmup, activación, señal,
antes/en T+120, aceptación pendiente/confirmada, Base, DCA/parcial, cancelación pendiente,
reemplazo de TP y cierre. ReplayReport, ReplaySnapshot, eventos ordenados, timestamps,
QuoteEvidence, OrderId, FillId, digest y checksum son exactamente iguales a ejecución continua.
Repetir run terminal no duplica nada. No se reconstruye confianza desde el checksum y no se
implementa recovery persistente entre procesos. No hay estados financieros en estos fixtures;
sus opt-in previos se cubren adicionalmente por la suite completa.

## Fingerprints congelados antes de editar

La copia inicial estaba en efcf53e; se obtuvo origin/main mediante Git y se creó exclusivamente
la rama indicada desde el SHA exacto solicitado. Antes de modificar producción se ejecutaron
las cuatro suites específicas (181 aprobadas, 82.10s) y se congelaron nueve escenarios de main.

| Escenario | Digest de eventos pre/post idéntico |
| --- | --- |
| NoQuotes | `1de51441efde90d5059802f62aac9fb21926a16be4dd23b1327109f352d96746` |
| FixtureQuotes | `2ca54f97b544a9f06248d4f5144eaa58dc2f90573ad2f57204c371b1fa074b47` |
| Hechos de fixture | `281e69e645499ff683d854d47844292a026279bacf5902e12d11be986746f484` |
| Modeled SQLite FAVORABLE | `c00688d065ae1127af517e47ae3ec4db14f1275c4597782b1b30d14527afa9e9` |
| Modeled SQLite BASE | `7054d26fe91ec36a1599a1cc1f17e3210f1e90e9b23903847afd49c6ada21d5d` |
| Modeled SQLite ADVERSO | `a67adf59d3ccf9f49281308a16f047de91fe091177ed114c8b2f951731843ec3` |
| Simulated SQLite FAVORABLE | `c9dd2ea8e07a5309d5617c31894a97849e4c00cef9244fd19ba5119e27df0391` |
| Simulated SQLite BASE | `20ab54584420865b6b5f547d427e7809f4ab2f927c95f51c782d05843d3b7757` |
| Simulated SQLite ADVERSO | `8f8c634f6d5cf3ee4e07a7e9b636806abb569c7add1c67b68034df10ffccd0a1` |

También coinciden exactamente SHA-256 de ReplayReport, emisiones, checksum de snapshot,
identidad Quote e identidad Simulation: valores completos en
[historical_modeled_baselines.json](../tests/unit/replay/fixtures/historical_modeled_baselines.json).
Las identidades de fixtures preparados incluyen rutas de procedencia temporales y pueden
cambiar entre ejecuciones independientes. Los fingerprints de la traza/benchmarks documentan
esas ejecuciones concretas; no se anuncian como constantes legadas ni prueba de autenticación.

## Cobertura obligatoria y validación

| Casos solicitados | Pruebas nuevas y regresión pertinentes |
| --- | --- |
| A–D: latencia/frescura | latency_freshness_boundaries en ambos modos; model_closed_candle previo verifica reason de latencia directo. |
| E–G: gaps/futuro | economic_gaps, future_and_contract_reads, exact_closed_prefix; fixture adquirido después de su disponibilidad económica. |
| H–L: confianza | identity_and_same_session, trust_errors, tampered_prepared_blocks; pruebas previas de conflicto/cambio antes y durante preparación. |
| M–N: cutoff/warmup | partial_cutoff, warmup_and_allow_entries; regresión de warmup incompleto en 5D1. |
| O–R: MARKET/LIMIT | market_requires_valid_prepared_quote, limit_eligibility; ronda con Base/DCA reales del mismo motor. |
| S–V: parciales/TP/cancelación/reemplazo | complete_round, continuous_fragmented en cancelación pendiente; regresión de shortfill y carrera TP previo. |
| W–Z: políticas/fuente/snapshot/reanudación | policy_and_quote_instance_conflicts, identity_and_same_session, trece cortes por modo. |
| AA–AD: modos/Decimal/red/SQLite | strict_and_segmented_economic_identity, hostile_decimal_and_network, preparación sin sockets, nueve congelaciones. |
| AE–AH: NoQuotes/Fixture/contrato/unidades | entries_off_and_noquotes_default, frozen_main_legacy_ports, structural_port, comparación Candle exacta y rechazo SHIBUSDC. |
| AI–AJ: slippage/gaps sin fills | MARKET=100.080015 exacto; LIMIT protegido; gap sin liquidez, órdenes ni fills ficticios. |

- Python host 3.12.14; Docker 3.12.15, dependencias locales existentes.
- Nuevas 5D2: **98 aprobadas**, 101.85s.
- Específicas existentes combinadas post-cambio: **181 aprobadas**, 83.76s.
- Ejecución individual: ModeledQuotes **38**, 25.06s; adaptador histórico **59**, 48.62s;
  simulador **60**, 0.05s; replay simulado **24**, 11.13s.
- Docker específico nuevo: **98 aprobadas**, 129.91s.
- Suite completa host: **2545 aprobadas**, 729.19s (12 min 09 s).
- Suite completa Docker: **2545 aprobadas**, 915.64s (15 min 15 s), exit code 0.
- Ruff format --check: **189 archivos conformes**; Ruff check aprobado.
- mypy estricto: **189 archivos aprobados**; git diff --check aprobado.

Se recogieron 2545 tests en la suite actual. Los 2447 declarados por 5D1 son referencia histórica,
no una validación propia. Los resultados finales de suite completa se consignan arriba cuando
terminan; no se confunde collection con aprobación.

Docker usa imagen local `sha256:654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf`,
`--pull=never --network=none --read-only`, repo montado de sólo lectura, `/tmp` temporal,
PYTHONPATH=/app/src y UID/GID 1000:1000 para leer el workspace sin cambiar sus permisos.
Se comprobaron NetworkMode=none y ReadonlyRootfs=true. El primer intento con UID de imagen
10001 no pudo leer el mount privado; se corrigió el selector de usuario y se repitió la prueba.
No se descargaron paquetes ni imágenes, no hubo build de red ni cambios de credenciales/proxy.
Contenedores exclusivamente de pruebas, no servicios del bot.

## Rendimiento sintético

| Minutos | Modo | Registro s | Sesión inicial s | Preparación s | Replay s | Quotes s | RSS bytes | Bloques Quotes / Replay | SQLite preparado bytes |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | STRICT_FULL | 0.355 | — | 1.052 | 1.189 | 0.0033 | 43393024 | 1 / 2 | 962560 |
| 20 | SEALED_SEGMENT_SESSION | 0.377 | 0.324 | 0.688 | 1.449 | 0.0037 | 44421120 | 1 / 2 | 962560 |
| 1440 | STRICT_FULL | 0.669 | — | 1.961 | 3.999 | 0.0723 | 49487872 | 2 / 3 | 1818624 |
| 1440 | SEALED_SEGMENT_SESSION | 0.672 | 0.672 | 1.168 | 3.951 | 0.0933 | 50147328 | 2 / 3 | 1818624 |
| 10080 | STRICT_FULL | 2.577 | — | 7.538 | 20.632 | 0.5071 | 94728192 | 8 / 9 | 7024640 |
| 10080 | SEALED_SEGMENT_SESSION | 2.628 | 2.452 | 4.450 | 22.821 | 0.6823 | 96862208 | 8 / 9 | 7024640 |

Mediciones pequeñas reproducibles, un escenario por proceso, 1560 minutos de warmup además
del intervalo indicado. Registro y validación inicial de sesión se muestran aparte; preparación
incluye conciliación y validación. Quotes se miden en un bucle separado antes de Replay;
por tanto Replay empieza con un bloque validado cacheado. Las cargas cuentan verificaciones
completas de bloques no cacheados, no accesos lógicos/minutos ni lecturas físicas de disco.
RSS máximo incluye fixture/registro/preparación/Quotes/Replay y imports de helpers de tests;
no es memoria exclusiva del adaptador ni un pico de temporales agregado.

Las seis mediciones comparan exactamente ejecución continua/fragmentada. Sus digests pueden
diferir entre procesos/modos por rutas temporales y evidencia; la equivalencia entre modos
del mismo dataset se comprueba aparte en tests. No se afirma que un modo sea más rápido:
hubo otras validaciones concurrentes. Datos completos, restauración y tamaños de sesión:
[historical_modeled_benchmarks.json](fixtures/historical_modeled_benchmarks.json).

## Limitaciones y cierre para auditoría

- STRICT_FULL verifica originales integralmente al preparar; conserva su snapshot aceptado.
- SEGMENT_VERIFIED / SOURCE_NOT_REVALIDATED no equivalen a autenticación global del proveedor.
- Las raíces de proceso protegen bloques persistidos bajo confianza en proceso/memoria;
  no son firma independiente ni defienden contra control del proceso.
- Trade no autentica Mark, Funding, Fees, Brackets, Capital, Balance o ExecutionObserved.
  Las autorizaciones financieras pendientes de 5D1 permanecen pendientes.
- No hay rentabilidad histórica financiera completa, supervivencia/liquidación real o Mark observado.
- No hay recovery persistente entre procesos ni activación automática de entradas/finanzas.
- No se descargaron históricos, no se ejecutó replay 2024–2026, no hubo Binance/API privada,
  fondos/transferencias/Add Margin real, Telegram, VPS, servicios, Shadow o LIVE.
- Git externo se limita a obtener/verificar base y publicar la feature autorizada; pruebas y
  benchmarks son offline. main no cambia, sin force push, rebase o merge.

Comparación con main: sólo el consumidor de Quotes y la comprobación estructural del puerto
cambian en producción. model_closed_candle, AltBEngine, filtros, simulador, runner y todos los
contratos financieros mantienen su contenido. Se verificó AST idéntico de model_closed_candle
y contenido exacto de los módulos protegidos mediante Git. SHA-256 sin cambios:

| Archivo protegido | SHA-256 en base y feature |
| --- | --- |
| history.py | `1004537c9086b1184884bc9a213634f0edf6cf93ccceb0bc4b293fc7067856d2` |
| runner.py | `117ef2f86801f52350758280e87db46fa03652b4f8fbb2ad59bb1bc7b2946793` |
| replay_adapter.py | `41b1a400329da14704530c3bcf956b1e266823703056cc5526829096f2031442` |

La configuración del runtime se comprobó
directamente como **STOPPED**. Se detiene la fase aquí para auditoría independiente, sin 4A-5D3.
