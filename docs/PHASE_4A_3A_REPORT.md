# Entrega Fase 4A-3A — ledger financiero y PnL bruto

Main/base aprobada: `866e335a122e17ac1a328c03ecb2e7d2152e3e15`.
Feature: `phase-4a-3a-financial-ledger`.
Especificación: [FINANCIAL_LEDGER_SPEC.md](FINANCIAL_LEDGER_SPEC.md).

## 1. Arquitectura

FinancialLedger puro, frozen, independiente de estrategia, ejecución, riesgo y
persistencia. Consume únicamente FillEvidence+Order recibidos en ReplayRunner.
Activación `ledger=FinancialLedger(LedgerPolicy())`; default None. No crea fills,
Orders o intenciones, no lee Candle/Quote ni modifica AltBEngine o SimulatedExecution.

## 2–4. Fórmulas, promedio móvil y rondas

BUY: nocional q×p, aumenta cantidad/costo; promedio = costo abierto / cantidad.
SELL: ingreso q×p efectivo, libera q×promedio previo; PnL bruto = ingreso−costo.
Una compra nunca realiza PnL. Una venta final con cantidad canónica exactamente
cero libera todo el costo remanente, asignando únicamente el residuo de precisión
Decimal a esa salida; no tolera ni perdona cantidades residuales.

Cantidades y nocionales por lado se suman canónicamente por FillId a 50 dígitos.
Costo cambia en orden económico aceptado; el acumulado realizado se conserva en
BUY y, tras SELL, se concilia por ingresos−(compras−costo abierto). Sin ventas,
todos los nocionales BUY canónicos permanecen abiertos y PnL es exactamente cero.
Context local independiente del llamador; UTC, sin float o redondeo a centavos.
Unidades 1000SHIBUSDC/USDC, sin factor 1000 ni aplicar leverage adicional.

Cada RoundLedger conserva BUY/SELL ejecutados, nocionales, costo/cantidad/promedio
remanentes, PnL bruto, conciliación y cierre observado. No se trasladan costos
entre rondas. CLOSED del motor exige cero cantidad/costo del ledger; terminar el
período no cierra posiciones. Un cierre incompatible bloquea, sin venta ficticia.
El promedio financiero después de una venta y nueva compra puede diferir del
promedio estratégico de todos los BUY; TP/P0 quedan íntegros en AltBEngine.

## 5–6. Shortfill, cancelaciones y TP

Base MARKET parcial y luego CANCELED conserva exposición/costo económicos.
La integración prueba cantidad 0.05 y el bloqueo `base_shortfill_reconciliation`,
sin P0 definitivo ni DCA/TP. Orders/cancelaciones no revierten asientos.

TP viejo parcialmente ejecutado, cancelación confirmada y sustituto conservan
solo los FillIds únicos, sin duplicar ingresos/cantidades. La ronda completa
sintética mantiene DCA1 y TP parciales, siete DCA y cierre conciliado; se compara
PnL bruto contra cálculo independiente de flujos efectivos, sin TP teórico.

## 7–8. Conciliación, causalidad e integración

Vinculación uno-a-uno OrderId/IntentId e identidad inmutable Fill/Order/Intent/
Round/Symbol/Side/procedencia. Duplicado idéntico no añade economía, incluso si
cambia el Order acumulado o el tiempo de retry; contenido incompatible bloquea.
SELL que supera posición o fills que exceden la Order no generan SHORT ni se recortan.

Política económica explícita: rechazar ocurrencia tardía; para empates exigir
ReplayFact.sequence creciente por ronda. No se reordena evidencia pasada ni se
inventa secuencia de Binance. Solo MODELED_EXECUTION y SYNTHETIC_FIXTURE permitidos;
REAL_OBSERVED reconocido pero reservado a otro contrato futuro verificable.
Timestamp económico y received_at separados; delivery latency se respeta. Los
fills futuros en colas del simulador nunca se contabilizan por adelantado.

LedgerState guarda asientos, journal, FillIds, rondas, policy/identity y conflictos.
Snapshot valida checksum y reconstruye contabilidad exacta desde journal, incluso
si alguien recalcula checksum tras adulterar costos. Restore con otro policy,
ledger desactivado, dataset/fuentes/sesión o estado incompatible se rechaza.
Continuo == fragmentado incluye estados, costos, PnL, IDs, asientos, reportes y
huellas, con checkpoint durante cancelación TP, delivery y resolución MARKET.

Los fallos financieros interrumpen el replay, conservan prefijo válido y conflicto;
reporta PNL_BRUTO_REALIZADO_BLOQUEADO, sin atribuirlo como resultado completo.
Caso válido activo: PNL_BRUTO_REALIZADO_DISPONIBLE. Sin ledger, se conserva
FINANCIAL_RESULTS_NOT_AVAILABLE y el JSON/huellas anteriores.

## 9. Ejemplos numéricos reproducibles

| Caso | Fills confirmados | PnL bruto USDC | Cantidad abierta | Costo abierto USDC | Promedio abierto |
| --- | --- | ---: | ---: | ---: | ---: |
| A | BUY 10×100; SELL 10×101 | 10 | 0 | 0 | None |
| B | BUY 10×100; BUY 10×90; SELL 10×97 | 20 | 10 | 950 | 95 |
| C | BUY 10×100; SELL 5×110; BUY 5×80 | 50 | 10 | 900 | 90 |
| E | BUY 10×100; TP viejo SELL 4×101; cancelación; sustituto SELL 6×102 | 16 | 0 | 0 | None |

Fixtures sintéticos, sin fuente de calibración Binance ni PnL neto. Se usan
precios efectivos de Fill aunque el LIMIT teórico del fixture sea distinto.

## 10–11. Validación

76 tests nuevos y 1764 anteriores conservados; total 1840.
Suite completa local final: **1840 passed in 355.67s (0:05:55)**.
Suite Docker offline final: **1840 passed in 459.81s (0:07:39)**.

Comandos ejecutados sobre la fuente final:

```sh
.venv/bin/python -m pytest -q tests/unit/replay/test_financial_ledger.py tests/unit/replay/test_financial_replay.py
.venv/bin/python -m pytest -q
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
git diff --check
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run --rm --user "$(id -u):$(id -g)" --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly -e PYTHONPATH=/app/src shib-alt-b:test python -m pytest -q -p no:cacheprovider
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run --rm --user "$(id -u):$(id -g)" --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly -e PYTHONPATH=/app/src -e TRADING_MODE=STOPPED shib-alt-b:test python -c 'import runpy; import trading_engine.replay.financial_ledger.core as ledger; print(ledger.__file__); runpy.run_module("trading_engine", run_name="__main__")'
```

Docker usa imagen local existente y fuente actual read-only, con red deshabilitada,
/tmp temporal, capabilities retiradas y sin nuevos privilegios. No hubo build,
descarga de imagen, dependencias o datos. Bootstrap final: **bootstrap_ready / STOPPED**.

Pruebas específicas finales: **76 passed in 55.60s**.
Ruff format: **130 files already formatted**.
Ruff check: **All checks passed!**
Mypy: **Success: no issues found in 130 source files**.
`git diff --check`: sin errores.

Las 76 pruebas incluyen A–F, pérdida bruta, shortfill, TP parcial/reemplazo,
ventas seguidas de compras, cero fills, rondas abiertas/planas/cerradas, negativos,
conflictos, datos/timestamps inválidos, contexto hostil, overflow/underflow, duplicates,
receipt futuro, late/ties, qty/no escala, snapshots adulterados, reanudación,
gaps, procedencia y ausencia de red/dependencias LIVE.
Tres vectores congelados **antes de editar código**, sobre main aprobado,
comprueban event_digest, snapshot.integrity_digest, encoded(ReplayReport) y JSON
as_dict() de NoQuotes, FixtureQuotes y ModeledQuotes+SimulatedExecution sin ledger.
No se modificaron, eliminaron o relajaron tests anteriores.

## 12. Archivos

- `src/trading_engine/replay/financial_ledger/{__init__,policy,models,core}.py`
- `src/trading_engine/replay/{runner,models,encoding}.py`
- `tests/unit/replay/{test_financial_ledger,test_financial_replay}.py`
- `tests/unit/replay/fixtures/financial_ledger_disabled_baselines.json`
- `docs/FINANCIAL_LEDGER_SPEC.md`, `docs/PHASE_4A_3A_REPORT.md`
- `docs/{REPLAY_SPEC,ARCHITECTURE,SIMULATED_EXECUTION_SPEC,EXECUTION_MODEL_SPEC}.md`
- `README.md`

## 13–14. Git

Un commit de implementación desde el main aprobado, publicación exclusiva feature.
SHA final se entrega tras publicación/verificación, sin autorreferencia.
Comparación prevista: 1 ahead, 0 behind, 0 merge commits. Main permanece intacto.
No merge, rebase, squash o force push.

## 15–16. Limitaciones y pendientes para 4A-3B

Gross realizado disponible únicamente para el prefijo reconciliado de fills.
Neto, unrealized, equity, drawdown, fees, funding y liquidation_price se declaran
NOT_AVAILABLE. Presupuesto de órdenes no es patrimonio; no se valoran posiciones
sin mark ni inventan capital, tasas, funding, liquidación o Add Margin.

Pendientes: contrato verificable de fees/funding y timestamps/unidades, cash y
capital/saldos, mark causal, costos/neto, equity/drawdown y conciliación verificable
de evidencia externa fuera de orden. Margen y liquidación requieren otra
especificación validada. Estado en memoria, misma sesión SQLite y sin recovery
persistente LIVE. Las etiquetas no certifican mercado ni autenticidad; resultados
modelados no son backtest financiero completo 2024–2026 ni rentabilidad certificada.
Runtime STOPPED; sin Binance privado, descargas, trading, servicios o despliegue.
Entrega detenida para revisión.
