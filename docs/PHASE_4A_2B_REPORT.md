# Entrega Fase 4A-2B — simulador causal offline

Base: `7fbc249ab532d9c21b4b72e67436caa1ce14e7ee`.
Feature: `phase-4a-2b-simulated-execution`.
Especificación: [SIMULATED_EXECUTION_SPEC.md](SIMULATED_EXECUTION_SPEC.md).

Este informe conserva la evidencia de la entrega inicial v1
(`5e0bec5345504fe4a84b60d37c6ccd222d418726`), incluidos sus resultados y fixture
de Base persistente. El ciclo MARKET fue corregido posteriormente a una única
oportunidad: véase [PHASE_4A_2B_MARKET_FIX_REPORT.md](PHASE_4A_2B_MARKET_FIX_REPORT.md)
y la especificación vigente. Sus métricas/huella anteriores no describen el replay v2.

## 1. Arquitectura

SimulatedExecution independiente de estrategia/adaptadores recibe las intenciones
y cancelaciones del mismo AltBEngine y devuelve Order/FillEvidence de dominio
en ReplayFact MODELED_EXECUTION. Integración opt-in en ReplayRunner, default None.
Estrategia, modelos Order/Fill, fórmulas, filtros, leverage 4 y TP 0.008 intactos.
Sin nuevas dependencias. Runtime STOPPED.

## 2. MARKET

Base se emite únicamente mediante la acción T+120 y Quote válida del runner.
No se ejecuta automáticamente: PENDING, aceptación explícita, demora y primera
vela completa posterior a elegibilidad. BUY = ask × (1 + slippage_bps/10000),
spread ya incluido en Quote. Sin Quote/volumen/hipótesis de cola suficiente no
hay Fill; motivo explícito. También se soporta rechazo modelado.

## 3. LIMIT

Precio/quantity originales intactos. Toque o cruce es candidato; requiere ventana
temporal, hipótesis de cola y volumen. Slippage es buffer de elegibilidad low
BUY/high SELL; Fill al límite exacto, sin mejora ni doble aplicación. La política
ADDITIONAL_EVIDENCE exige además Quote causal que soporte el límite.
BUY nunca excede límite; SELL nunca baja del límite. Una orden nueva no usa
trayectoria anterior a aceptación ni la vela que la creó.

## 4. Liquidez compartida

Cota = volumen base de vela cerrada × max_volume_participation. Una sola cota
para todas las órdenes del minuto, con asignaciones por IntentId. Unidades
1000SHIBUSDC. Cola y profundidad siguen desconocidas; BUDGET_FIFO es una hipótesis
configurada, WITHHOLD abstiene. SELL reduce-only se limita a exposición de fills.

## 5. Parciales

Se asigna como máximo el restante y la cota remanente. Parciales actualizan
OrderStatus y cantidad reportada; su Fill permite al motor calcular VWAP y TP.
WITHHOLD_IF_PARTIAL_REQUIREMENT_UNKNOWN abstiene ante capacidad parcial.
Sumas canónicas por FillId y lado en Decimal local de 50 dígitos, sin tolerancias
para inventar un cierre o recortar exposición.

## 6. Cancelación/reemplazo TP

Solicitud y confirmación separadas por latencia obligatoria. El TP anterior puede
ejecutarse en una vela completa antes de confirmarse su cancelación. El hecho
terminal conserva la cantidad final y solo entonces AltBEngine reemplaza.
Confirmación antes del cierre coincidente; no se asignan recorridos de velas
parcialmente activas. Reintentos idénticos no duplican solicitudes ni fills.

## 7–8. Escenarios y ambigüedad

FAVORABLE prioriza TP como sensibilidad, BASE abstiene si hay DCA y TP candidatos,
ADVERSO prioriza DCA como sensibilidad. Se registra la hipótesis; no se afirma
haber observado secuencia intraminuto. Todos exigen temporalidad, precio, volumen
y cola configurada. El otro lado solo puede ejecutar con cota y exposición
compatibles; una hipótesis no finge cancelación instantánea ni trayectoria OHLC.

## 9. Causalidad

Se separan emisión, aceptación, elegibilidad, ocurrencia, confirmación y entrega.
Datos solo de velas cerradas; precio MARKET de Quote disponible al evaluar,
sin usar high/low futuro. Gap produce no ejecución; datos inválidos interrumpen
con causa. Evidencia con entrega futura permanece pendiente, no recibida por
el motor. No se alteran velas o decisiones pasadas.

## 10. Idempotencia y reanudación

UUIDv5 con namespace simulado separado; OrderId correlacionada por intención y
FillId por orden/minuto/modelo. Estado frozen completo con colas, fills, cancels,
liquidez y cursores entra en checksum. Se rechazan identidades/contenido
conflictivos, cambios de perfil, parámetros, dataset, reglas, sesión o estado
incompatible. Continuo/fragmentado idénticos incluso con cancelación pendiente
y aceptación cuya evidencia todavía no se entregó.

## 11. Ronda sintética reproducible

`test_complete_base_seven_dcas_dynamic_tp_and_closed_round` usa OHLC planos
sintéticos y volumen configurable. Incluye Base parcial, DCA1 en cuatro fills,
los siete DCA, sucesivos TP cancelados y TP final en dos fills. No depende de
asumir fill completo en cada toque. Resultado técnico reproducido:

| Métrica | Valor |
| --- | ---: |
| Intenciones / órdenes aceptadas | 14 / 14 |
| Rechazadas | 0 |
| Órdenes FILLED | 9 |
| Fills generados | 14 |
| Cancelaciones solicitadas / confirmadas | 5 / 5 |
| Conflictos | 0 |
| Rondas cerradas | 1 |
| Cantidad abierta | 0E-48 (exactamente cero) |

Fills DCA: `(4, 1, 1, 1, 1, 1, 1)`; fills cadena TP: `(0, 0, 0, 0, 0, 2)`.
Huella: `7ca63b42c8bf68c42f6e8d4f6d02f7b1db4bc167c9af8c49e48d3349edcd1993`.
FINANCIAL_RESULTS_NOT_AVAILABLE: esto no es un resultado financiero 2024–2026.

## 12. Métricas técnicas

Report expone estado y ExecutionMetrics: intenciones, aceptadas/rechazadas,
pendientes PENDING, parciales/full según status actual, cantidad bruta ejecutada
(BUY+SELL), sin fills, cancelaciones solicitadas/confirmadas/pendientes, fills
generados, no ejecuciones por causa, ambigüedades, cotas/asignaciones, conflictos,
identidad de escenario/modelo y procedencia. Órdenes OPEN y hechos pendientes
de entrega permanecen visibles en estado. Sin PnL, fees, funding o equity.

## 13. Comandos y verificaciones

84 tests nuevos; 1658 anteriores conservados; 1742 totales.

```sh
.venv/bin/python -m pytest -q tests/unit/replay/test_simulated_execution.py tests/unit/replay/test_simulated_replay.py
.venv/bin/python -m pytest -q tests/unit/replay/test_simulated_execution.py tests/unit/replay/test_simulated_replay.py tests/unit/replay/test_runner.py::test_no_clock_sleep_exchange_execution_or_strategy_copy
.venv/bin/python -m pytest -q
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
git diff --check
```

- Específicos: **84 passed in 16.45s**.
- Específicos + guard arquitectónico anterior, después de corregir integración:
  **85 passed in 19.95s**.
- Ruff format: **123 files already formatted**.
- Ruff check: **All checks passed!**
- Mypy: **Success: no issues found in 123 source files**.
- git diff --check: sin errores.
- Suite completa local final: **1742 passed in 231.21s (0:03:51)**.
- Docker offline completo final: **1742 passed in 282.78s (0:04:42)**.
- Bootstrap Docker con código actual: **bootstrap_ready / STOPPED**.

La primera suite completa local/Docker encontró el guard anterior que prohíbe
`submit` en ReplayRunner (1741 passed, 1 failed). El receptor puro se llama ahora
`receive_intent`; no implementa ExecutionAdapter. El test anterior quedó intacto.
La suite completa se repitió sobre esta corrección; los resultados finales se
registran arriba.

Comando Docker ejecutado sobre imagen existente y fuente actual:

```sh
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run --rm --user "$(id -u):$(id -g)" --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly -e PYTHONPATH=/app/src shib-alt-b:test python -m pytest -q -p no:cacheprovider
```

No hubo descarga de imagen/datos ni build con red. Los tests son fixtures offline.

## 14. Archivos

- `src/trading_engine/replay/simulated_execution/{__init__,core,policy,state}.py`
- `src/trading_engine/replay/{models,runner}.py`
- `tests/unit/replay/{test_simulated_execution,test_simulated_replay}.py`
- `docs/SIMULATED_EXECUTION_SPEC.md`, `docs/PHASE_4A_2B_REPORT.md`
- `docs/REPLAY_SPEC.md`, `docs/EXECUTION_MODEL_SPEC.md`, `docs/ARCHITECTURE.md`
- `README.md`

## 15–16. Git y comparación

Un único commit feature sobre el main aprobado, sin merge ni force push.
SHA exacto se entrega después de publicar/verificar GitHub; no se incrusta un SHA
autorreferente en el commit. Entrega prevista: 1 ahead, 0 behind, 0 merge commits.
Main permanece en `7fbc249ab532d9c21b4b72e67436caa1ce14e7ee`.

## 17. Pendiente para contabilidad y PnL

Ledger auditable de cash/exposición/costes; fees/funding verificados; mark/margen;
calibración fuera de muestra, incertidumbre de cola y datos más finos. Sin
Add Margin, liquidación o supuestos de beneficios. No habilitar LIVE para obtener
estos resultados. Runtime STOPPED; sin despliegue, servicios o trading.
Entrega detenida para revisión.
