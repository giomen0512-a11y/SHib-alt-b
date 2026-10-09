# Entrega técnica — Fase 4A-1

Base aprobada: `2b2446902e56ab61e9004be0e64aa2f1aeb22647`.
Rama de entrega: `phase-4a-1-replay-causal`.
Alcance: infraestructura causal offline, sin modelo financiero histórico.

## 1. Arquitectura implementada

SQLiteHistory → FuturesSQLite read-only → ReplayRunner → el mismo
StrategyCoordinator(AltBEngine). SimulatedClock aporta únicamente tiempo lógico.
QuoteSource y FactSource aportan evidencia explícita. ReplaySnapshot conserva
estado/resultados y ReplayReport expone el informe JSON mediante as_dict().

No se modifica ninguna fórmula, parámetro, estado o interfaz de estrategia,
coordinación, ejecución o persistencia existentes. No se añade dependencia.
El runner no implementa ExecutionAdapter, no acepta ni envía órdenes.

## 2. Secuencia exacta de eventos

En start, termina primero el warmup y se aplica la autorización offline explícita.
En cada instante: EntryControl → Order → FillEvidence → cierre Candle o gap →
advance_time → Quote explícita → on_quote → on_scheduled_action → emisiones.
Los empates de hechos usan prioridad y sequence explícita; las acciones usan el
orden canónico del coordinador. PAUSE/gap en T+120 cancelan antes de Base.

## 3. Reglas del reloj

Tiempo UTC aware inyectado, avance monotónico, sin reloj del sistema ni espera.
Cada minuto solo se consulta/decodifica al cerrar. La señal procede del checkpoint
15m aprobado; su acción es elegible exactamente en T+120. Los límites de fragmento
no son eventos de dominio y no cambian la huella.

## 4. Lectura y validación SQLite

Mercado BINANCE_USDM_FUTURES, símbolo inicial 1000SHIBUSDC, intervalo 1m.
Transacción estable de lectura; query_only y mode=ro. Consulta indexada por minuto,
ventana canónica máxima de 1560 velas, Decimal/taker-buy sin transformación.
Warmup y período evaluado separados. Duplicados, conflictos, OHLC corruptos y
velas anteriores al contrato se rechazan. Gaps/pre_contract se registran;
ningún dato se rellena y el gap exige un nuevo warmup completo.

## 5. Quotes ausentes

NoQuotes es el default. FixtureQuotes usa únicamente inputs explícitos etiquetados
SYNTHETIC_FIXTURE. Quote debe pertenecer al símbolo y al instante exacto T+120.
Una Quote ausente/antigua/futura/ajena produce missing_quote e invalidación de la
reserva; no se emite Base ni se espera a una Quote arbitrariamente tardía.
Ninguna vela se convierte en bid/ask, Order o Fill.

## 6. Checkpoints y reanudación

Snapshot frozen con reloj, cursor de minutos/hechos, CoordinatorState completo,
reservas, órdenes/fills, ronda/historial, cancelaciones, contadores, emisiones y
huella. El checksum integral rechaza la pérdida accidental de evidencia/cursor.
Se restaura dentro del proceso y en la misma sesión SQLite con las mismas fuentes.
No hay serialización validada ni recovery persistente entre reinicios.
Las pruebas incluyen reanudación durante la cancelación TP y cierre conciliado.

## 7. Pruebas y controles

52 pruebas nuevas específicas verifican los requisitos del encargo:

| Requisitos | Evidencia |
| --- | --- |
| 1, 2, 22 | mode=ro/query_only, lectura ascendente, Decimal exacto, SQLite sin mutaciones y vista estable ante escritor externo de fixture |
| 3, 21 | Candle solo tras cierre, corrupción futura no afecta Base anterior, rechazo de timestamps futuros de Order/Fill/Quote |
| 4, 5 | 1560 minutos exactos, 1559 fail-closed, warmup separado y señal en checkpoint UTC 15m |
| 6, 7, 8 | Frontera T+120 con microsegundos; antes no emite, sin Quote invalida definitivamente |
| 9, 10, 11 | Puertos explícitos, ninguna ejecución desde OHLC; vela que toca DCA y TP conserva secuencia desconocida |
| 12, 13 | SQLite malformada con duplicados/conflictos, gap en el vencimiento, nuevo warmup completo |
| 14, 15, 16 | PAUSE previo/simultáneo, retries sin reemisión, FillId idempotente y contenido conflictivo interrumpe |
| 17, 18 | Huella determinista y empates por prioridad/sequence independientes del orden de inserción |
| 19, 20 | Continuo/fragmentado y snapshot integral con Base/fills, cancelación TP, reemplazo y cierre de ronda |
| 23 | Red prohibida en tests de integración, inspección de dependencias y Docker con --network none |
| 24 | Suite completa incluye intactos los 1500 tests anteriores |

Validación local completada:

- pytest específico: **52 passed**.
- pytest completo: **1552 passed**, incluidos los **1500 anteriores intactos**.
- Ruff format --check: 110 archivos con formato correcto.
- Ruff check: sin incidencias.
- mypy: sin incidencias en 110 archivos.
- Docker offline, red deshabilitada: **1552 passed**.
- Arranque offline desde esta versión montada: bootstrap_ready, mode=STOPPED.

Docker utiliza la imagen de tests existente con Python 3.12 y las mismas versiones
fijadas en pyproject.toml. Monta esta versión de src/tests en solo lectura y
PYTHONPATH=/app/src; ejecuta con el UID del workspace para leer el bind mount,
sin red, sin capacidades y con raíz read-only.
No descarga históricos ni conecta al exchange. El arranque principal se comprueba
en STOPPED, sin habilitar otro modo.

## Informe reproducible con datos sintéticos

Ambos escenarios usan una SQLite temporal de 1580 minutos **SYNTHETIC_FIXTURE**.
Período evaluado UTC: 2026-01-02 11:45 a 12:05; primer minuto leído:
2026-01-01 09:45. Inicio efectivo evaluado: 2026-01-02 11:45.
Warmup leído/aplicado: 1560; evaluados: 20; checkpoints evaluados: 1;
gaps/períodos inválidos: 0; interrupción: ninguna.

| Resultado | Sin Quote | Quote + hecho Base explícitos |
| --- | ---: | ---: |
| Señales | 1 | 1 |
| Acciones programadas | 1 | 1 |
| Acciones canceladas | 1 | 0 |
| Acciones sin Quote ejecutable | 1 | 0 |
| Intenciones teóricas de fixture | 0 | 9 (Base + 7 DCA + TP) |
| Cancel_requests | 0 | 0 |
| Eventos observados | 1587 | 1596 |

Sin Quote: no hay Base ni reserva pendiente al finalizar; el motivo missing_quote
queda en emisiones aunque block_reason actual termine en None.
Con evidencia explícita: Base confirmada, P0 efectivo 100 (precio **sintético**),
siete intenciones DCA y TP, ninguna ejecución DCA/TP confirmada. Bloqueo actual:
base_confirmed. El estado conserva toda la evidencia de esa única ronda.

Huellas SHA-256 de eventos:

- Sin Quote: `1de51441efde90d5059802f62aac9fb21926a16be4dd23b1327109f352d96746`.
- Quote + Base fixture: `380b59af21a844a4d1c1685b739f91012060c8c4abc92bd1b927af8aaf72bea1`.

**FINANCIAL_RESULTS_NOT_AVAILABLE**. Estos resultados no demuestran rendimiento
ni disponibilidad histórica real de Binance. No se ejecuta replay 2024–2026.

## 8. Archivos modificados

- README.md; docs/ARCHITECTURE.md; docs/REPLAY_SPEC.md; este informe.
- src/trading_engine/replay/__init__.py, clock.py, encoding.py, history.py,
  models.py, ports.py y runner.py.
- tests/unit/replay/__init__.py, conftest.py y test_runner.py.

## 9. Commit

Un único commit de implementación en la rama de entrega, con padre igual a la
base aprobada. Su SHA definitivo se entrega en la respuesta de publicación;
en la rama se obtiene mediante `git rev-parse HEAD`. No se incrusta el SHA del
propio commit en su contenido porque eso cambiaría ese mismo SHA.

## 10. Diferencia frente a main

Cambios limitados al paquete replay, pruebas nuevas y documentación pertinente.
Se conservan los 1500 tests anteriores. Los módulos aprobados de estrategia,
orchestration, execution, persistence, config, despliegue y runtime no se editan.
No hay merge ni modificación de main; se publica exclusivamente la rama feature.
No se despliega VPS, instalan timers, descargan datos reales, conectan credenciales
Binance ni modifican configuraciones del bot real. Runtime permanece STOPPED.

## 11. Decisiones pendientes para 4A-2

Quotes desde datos 1m, spread, slippage, vigencia temporal; prioridad/activación
LIMIT, parciales y liquidez/volumen; latencias; secuencia intraminuto; fees/funding;
datos insuficientes, shortfill y comparación de escenarios conservadores.
Ningún valor nuevo se escoge o presenta como dato histórico real.
La entrega se detiene para revisión antes de construir el simulador financiero.
