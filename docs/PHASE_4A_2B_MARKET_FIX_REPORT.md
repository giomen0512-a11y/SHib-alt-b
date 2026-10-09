# Corrección Fase 4A-2B — ciclo MARKET acotado

Rama: `phase-4a-2b-simulated-execution`.
Commit publicado anterior: `5e0bec5345504fe4a84b60d37c6ccd222d418726`.
Main aprobado: `7fbc249ab532d9c21b4b72e67436caa1ce14e7ee`.
Contrato vigente: [SIMULATED_EXECUTION_SPEC.md](SIMULATED_EXECUTION_SPEC.md).

## Causa y alcance

El filtro de órdenes activas incluía toda orden no terminal. Una MARKET parcial
volvía a participar en cada vela completa y podía completar Base varios minutos
más tarde, convirtiéndola implícitamente en una orden persistente.

Se corrige únicamente su vida modelada. LIMIT, cancelación/reemplazo TP, fórmulas
Alt B, modelos Order/Fill, runner y reglas de shortfill del motor permanecen
intactos. No hay otra implementación de P0, DCA o TP.

## Ciclo nuevo y evidencia temporal

La MARKET aceptada consume una sola oportunidad al cierre del primer intervalo
1m completo elegible. `market_evaluated_at` bloquea nuevas ejecuciones, incluso
mientras su estado terminal sigue pendiente. Un gap en ese primer intervalo
también consume la oportunidad: no se espera una vela futura para mejorarla.

Con condiciones suficientes se produce como máximo un Fill. Si queda remanente,
se programa `market_expire` y Order CANCELED para el cierre de evaluación más
`execution_delay_ms`, parámetro obligatorio, configurable y positivo ya existente.
Se reutiliza explícitamente para esta resolución; no se inventa otra latencia
numérica. El Fill conserva timestamp, ID, precio y cantidad; el Order terminal
conserva la cantidad ejecutada y tiene `updated_at` estrictamente posterior.
La entrega al motor conserva su demora independiente `delivery_latency_ms`.

La transición tiene audit propio y no fabrica una OrderCancelRequest estratégica.
Una confirmación externa coincidente precede a la resolución; solo se genera un
snapshot terminal. No se modifica la política de cancelaciones LIMIT/TP.

Sin Quote, volumen, cola asumida o condiciones para un parcial, se registra
`no_execution`, no Fill, y se resuelve el remanente cancelándolo. No se reintenta
en minutos posteriores ni se sustituye evidencia ausente con precios futuros.

Este modelo es una hipótesis offline acotada, **no una reproducción certificada
del matching engine de Binance ni una afirmación sobre sus remanentes MARKET**.

## Ejemplos sintéticos

En los tests unitarios, cota de volumen 5 y Quote.ask 100.05, slippage 3 bps:

| Solicitud | Fill único | Resultado |
| --- | --- | --- |
| Q=4 | q=4 a 100.080015 | FILLED en el cierre; no transición extra |
| Q=8 | q=5 a 100.080015 | PARTIALLY_FILLED; CANCELED 100ms después con cantidad ejecutada 5 y remanente 3 sin ejecución |
| Sin Quote o volumen | Ninguno | CANCELED 100ms después; cero cantidad ejecutada |

Los valores son fixtures, no estimaciones calibradas de Binance. Spread ya está
en ask; el slippage se aplica una sola vez, con Decimal local de 50 dígitos.

La integración real con ReplayRunner/AltBEngine comprueba Base de volumen
insuficiente: un único Fill de cantidad 0.05 y luego CANCELED. Se conserva su
promedio efectivo, `p0=None` y `base_shortfill_reconciliation`; no hay DCA ni TP.
Sin ningún Fill, el bloqueo existente es `base_order_canceled`. Base completa
obtiene P0 de sus fills por el mismo AltBEngine y permite sus siete DCA.

La regresión Base→DCA1–DCA7→TP→CLOSED aporta ahora volumen suficiente en la
única oportunidad Base. Conserva DCA1 y TP parciales, reemplazos, exposición
final cero y todas las verificaciones anteriores; añade un único Fill Base.
El fixture que comprueba fill del TP viejo antes de confirmar cancelación se
adelanta un minuto, junto con sus cortes, porque Base completa un minuto antes.
Ningún test ni assertion anterior se elimina o relaja.

## Identidad, snapshots y reanudación

Reglas: `closed-minute-shared-budget-market-once-v2`. La identidad incluye esta
versión y los parámetros; no se reinterpretan snapshots v1 ni se reescriben
hechos publicados. OrderId/FillId siguen siendo UUIDv5 en el namespace simulado.

El estado conserva oportunidad consumida, resolución programada/confirmada y
transición futura. Se valida primera ventana elegible, como máximo un Fill,
resolución temporal coherente y rechazo de transiciones perdidas o adulteradas.
LIMIT no admite los campos específicos MARKET.

Se compara ReplayReport y ReplaySnapshot íntegros en ejecución continua y
fragmentada: checkpoints en el cierre, +50ms, +100ms y +500ms, para Base completa,
parcial y sin volumen, con entrega 0/500ms. Esto incluye fills, IDs, emisiones,
estado estratégico/simulado, bloqueos, métricas y huella.
`FINANCIAL_RESULTS_NOT_AVAILABLE` se mantiene.

## Validación ejecutada

22 tests nuevos; 1742 existentes conservados; total 1764.

| Validación | Resultado exacto |
| --- | --- |
| Pytest específico (simulador, integración, ciclo MARKET) | 106 passed in 58.01s |
| Pytest completo local | 1764 passed in 322.62s (0:05:22) |
| Ruff format --check | 124 files already formatted |
| Ruff check | All checks passed! |
| Mypy | Success: no issues found in 124 source files |
| git diff --check | Sin errores |
| Pytest completo Docker, sin red | 1764 passed in 376.11s (0:06:16) |
| Bootstrap Docker offline | bootstrap_ready, mode STOPPED |

Comandos ejecutados sobre el código final:

```sh
.venv/bin/python -m pytest -q tests/unit/replay/test_simulated_execution.py tests/unit/replay/test_simulated_replay.py tests/unit/replay/test_market_lifecycle.py
.venv/bin/python -m pytest -q
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
git diff --check
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run --rm --user "$(id -u):$(id -g)" --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly -e PYTHONPATH=/app/src shib-alt-b:test python -m pytest -q -p no:cacheprovider
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run --rm --user "$(id -u):$(id -g)" --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly -e PYTHONPATH=/app/src -e TRADING_MODE=STOPPED shib-alt-b:test python -c 'import runpy; import trading_engine.replay.simulated_execution.core as model; print(model.__file__); runpy.run_module("trading_engine", run_name="__main__")'
```

Docker utilizó la imagen local existente y la fuente actual montada read-only;
no se descargaron imágenes, dependencias ni datos. Las pruebas comprueban todos
los casos requeridos, incluidos shortfill, IDs estables, snapshots terminales,
continuo/fragmentado y la regresión sintética Base–DCA7–TP–CLOSED.

## Archivos de la corrección

- `src/trading_engine/replay/simulated_execution/core.py`
- `src/trading_engine/replay/simulated_execution/policy.py`
- `src/trading_engine/replay/simulated_execution/state.py`
- `tests/unit/replay/test_market_lifecycle.py`
- `tests/unit/replay/test_simulated_replay.py`
- `docs/SIMULATED_EXECUTION_SPEC.md`
- `docs/PHASE_4A_2B_REPORT.md` (nota de entrega histórica v1)
- `docs/PHASE_4A_2B_MARKET_FIX_REPORT.md`

## Git y límites

Un commit adicional de corrección sobre el feature publicado, sin reescribirlo.
Publicación exclusiva de feature, sin merge/force push; main permanece en el SHA
aprobado. Los SHA iniciales locales y remotos fueron verificados, incluido el
commit feature anterior. El SHA final se entrega tras publicar y verificar, sin
autorreferencia. Comparación con main después del commit adicional: dos commits
ahead, cero behind, cero merge commits.

Runtime STOPPED. Sin API privada, datos históricos reales, trading, despliegue,
servicios, PnL o fees. Entrega detenida para revisión.
