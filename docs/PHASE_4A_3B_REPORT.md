# Entrega Fase 4A-3B — costos, funding, valoración y patrimonio

Base aprobada: `5e8821b3f109f69d47f504c2f5d5a3bb337ba57d`.
Feature: `phase-4a-3b-financial-performance`.
Especificación: [FINANCIAL_PERFORMANCE_SPEC.md](FINANCIAL_PERFORMANCE_SPEC.md).

## 1. Arquitectura

FinancialPerformance frozen e independiente, opt-in en ReplayRunner con ledger
explícito. FinancialLedger 4A-3A permanece sin cambios. Fuentes independientes de
costos/cobertura y Mark, journal y valoración, sin estrategia/matching/API/margen.
No se modifican AltBEngine, Order/Fill, simulador, TP 0.80%, siete DCA o leverage 4.

## 2. Contratos de evidencia

FinancialEvent(received_at, sequence, payload), payload con effective_at/event_id
estable y Reference(source/dataset/provenance/assumptions). FeeEvidence por FillId/
OrderId, FundingEvidence por ronda/exposición, TransferEvidence, MarkEvidence y
CoverageEvidence. CapitalEvidence inicial conocido en start, sin default monetario.
FinancialSource/MarkPriceSource offline; fixtures identificados, sin red o secretos.

## 3. Comisiones

FeeEvidence USDC o FeeRatePolicy externa con tasas Decimal maker/taker y rol
explícito por FillId. Fee = notional efectivo × tasa, una vez. MARKET/LIMIT no
seleccionan tasa/rol. Fee desconocida/otra moneda sin conversión deja neto
NOT_AVAILABLE; dos comisiones/tasa+evidencia sobre mismo fill bloquean.

## 4. Funding

Importe firmado explícito o modelo LONG -cantidad histórica × precio referencia
× tasa con contrato de signo declarado. Exposición/fecha/ronda verificados;
no posición futura, calendario de ocho horas ni funding cero implícito. Cobertura
exige inventarios recibidos completos. Un fill tardío que cambia funding anterior
bloquea; funding/Fill simultáneos necesitan secuencia cruzada adicional.

## 5–9. Fórmulas

```text
flotante = cantidad_abierta × Mark - costo_contable_abierto
cash_neto = bruto_realizado - fees + funding
wallet = capital_inicial + aportes - retiros + cash_neto
equity = wallet + flotante
peak = máximo equity válido conocido hasta la muestra
drawdown = peak - equity; porcentaje = drawdown / peak × 100 si peak > 0
```

Decimal local 50/UTC; sin centavos prematuros ni nuevo factor 1000/leverage.
Wallet no funciona como nocionales Spot. Equity no es margen libre. Wallet
negativo se señala, sin liquidación fingida. Flotante cero sin Mark únicamente
con exposición exactamente cero. Ronda abierta informa cash acumulado, no beneficio
final. Neto cerrado exige closed_at conciliado, cero exposición y costos completos.
Retorno general/TWR/MWR permanecen NOT_AVAILABLE; flujos externos separados.

## 10–11. Completitud y procedencia

Cada métrica lleva value/status/reasons/provenances. AVAILABLE, MODELED,
NOT_AVAILABLE, INCOMPLETE, BLOCKED. Bruto permanece independiente de Mark opcional.
UNKNOWN no autoriza importes; OBSERVED se reconoce pero requiere contrato de
validación futura y se rechaza en esta fase. Fixtures nunca son datos Binance.
CoverageEvidence FUNDING/TRANSFERS declara período e IDs; ausencia no prueba cero.
Datos financieros faltantes no bloquean el Replay; contradicciones sí bloquean.
Drawdown es de equity muestreado, no máximo continuo/histórico certificado.
Muestras inválidas mantienen cobertura PARTIAL y máximos INCOMPLETE; flujos pueden
mover raw equity y se etiquetan como no ajustados para retorno.

## 12. Pruebas numéricas

| Caso sintético | Resultado verificado |
| --- | --- |
| A: BUY 10×100, Mark 95 | costo 1000; flotante -50 |
| B: anterior, SELL 5×110, Mark 95 | bruto 50; cantidad 5; costo 500; flotante -25 |
| C: capital 1000, fees 2.10, funding -3 | wallet 1044.90; equity 1019.90; neto cash 44.90 |
| D: equity 1000→1100→990 | pico 1100; máximo DD 110 y 10% |
| E–G: fee/funding/Mark faltante | bruto válido; métricas dependientes NOT_AVAILABLE |
| H: cantidad cero | flotante cero; neto requiere costos completos |

Pruebas adicionales de tasas maker/taker explícitas, funding modelado positivo/
negativo, cero con cobertura, flujos/retorno ausente, exposición entre rondas,
FX ausente, referencias erróneas, contexto hostil y overflow/underflow.

## 13–14. Integración y compatibilidad

Ronda Base→DCA1–DCA7→TP→CLOSED con el mismo motor, DCA/TP parciales, latencia
500ms de fills y fee adicional 200ms, cancelaciones/reemplazos y exposición cero.
Variante con TP viejo parcial antes de confirmar cancelación mantiene estado y
ledger idénticos al runner anterior. Funding explícito -3/+1, fees sintéticas por
cada fill y Marks manuales; ecuaciones independientes verifican bruto/fees/funding/
wallet/equity/neto final. Estos tiempos/tasas son fixtures, no medidas de Binance.
Base shortfill conserva 0.05 contractual y bloqueo, sin P0/DCA/TP ni cierre forzado.

Tres vectores previos NoQuotes/FixtureQuotes/Modeled+simulator y uno adicional
ledger activo congelado desde main exacto verifican event_digest, checksum y
encoding anterior. Campos nuevos None se omiten y no hay eventos nuevos en modo
desactivado. No se modificaron, borraron o relajaron las 1840 pruebas anteriores.

Comparación de tres escenarios configurable y determinista, por período/capital
comparable, con sus fuentes/supuestos. Tasas comparables ordenadas si se proporcionan
las tres; no se promete orden de PnL ni se afirma equivalencia automática de mercados.

## 15. Snapshots y reanudación

Policy/fuentes/capital, ledger, eventos, fees/funding/flujos/Marks recibidos,
cobertura, journal, series, picos/DD, conflictos, cursores y pendientes. Eventos
futuros solo son calendario configurado; no están reconocidos en métricas.
Reconstrucción completa desde journal, validando vistas del ledger aprobado y
vínculo con snapshot del runner. Checksum recalculado no salva adulteración.
Misma sesión SQLite, período, dataset/fuentes/política/versión. Continuo ==
fragmentado en siete puntos con fills/fees/cancelación pendientes y huellas iguales.
Restauración de conflictos permite revisar prefijo y causa, sin continuar trading.
No hay serialización persistente, recovery entre procesos ni recovery LIVE.

## 16–17. Validación ejecutada

108 tests nuevos + 1840 existentes = 1948.

- Específico: **108 passed in 48.25s**.
- Completo local: **1948 passed in 308.40s (0:05:08)**.
- Docker offline: **1948 passed in 393.04s (0:06:33)**.
- Ruff format: **138 files already formatted**.
- Ruff check: **All checks passed!**.
- Mypy: **Success: no issues found in 138 source files**.
- `git diff --check`: sin errores.
- Bootstrap Docker: **bootstrap_ready / STOPPED**.

Comandos ejecutados sobre la implementación final:

```sh
.venv/bin/pytest -q tests/unit/replay/test_financial_performance.py tests/unit/replay/test_performance_replay.py
.venv/bin/pytest -q
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
git diff --check
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run --rm --pull never --user "$(id -u):$(id -g)" --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly -e PYTHONPATH=/app/src shib-alt-b:test python -m pytest -q -p no:cacheprovider
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run --rm --pull never --user "$(id -u):$(id -g)" --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly -e PYTHONPATH=/app/src -e TRADING_MODE=STOPPED shib-alt-b:test python -c 'import runpy; import trading_engine.replay.financial_performance.core as performance; print(performance.__file__); runpy.run_module("trading_engine", run_name="__main__")'
```

Docker usa imagen local existente, fuente actual read-only y red none; sin build,
pull, dependencias o datos descargados. Pruebas offline de red/dependencias LIVE
prohibidas incluidas. Una desconexión transitoria exec-server se recuperó; las
suites mantuvieron sus procesos y logs, sin reiniciarlos ni alterar resultados.

## 18. Archivos modificados

- `src/trading_engine/replay/financial_performance/{__init__,evidence,policy,models,core,comparison}.py`
- `src/trading_engine/replay/{models,runner}.py`
- `tests/unit/replay/{test_financial_performance,test_performance_replay}.py`
- `tests/unit/replay/fixtures/financial_performance_disabled_baseline.json`
- `docs/{FINANCIAL_PERFORMANCE_SPEC,PHASE_4A_3B_REPORT}.md`
- `docs/{FINANCIAL_LEDGER_SPEC,REPLAY_SPEC,ARCHITECTURE}.md`
- `README.md`

## 19–20. Git

Un único commit de implementación validada, publicación exclusivamente feature.
SHA publicado se entrega tras verificación en el informe final, sin autorreferencia.
Comparación con main aprobado: 1 ahead, 0 behind, 0 merge commits.
Main local/remoto permanece en la base aprobada. Sin merge, squash, rebase o force
push. Árbol limpio después de commit/publicación. Detenerse para revisión.

## 21–22. Pendientes

Margen, liquidación, brackets/garantías, margen libre y Add Margin requieren
contratos verificados e independientes; no se inventa su tratamiento. Retorno
ajustado por flujos también requiere política revisada.
Replay 2024–2026 requiere fuentes históricas auténticas de fees/rol por ejecución,
funding/calendario/cobertura, Mark/frescura/continuidad, capital/flujos y secuencia
cruzada, calibración de supuestos y validación fuera de muestra. Datos en memoria
no prometen almacenamiento de varios años; faltan recovery persistente y límites
de memoria. Fixtures/escenarios no son validación financiera histórica real.
Sin API privada, exchange, datos reales, VPS, Telegram, systemd o servicios.
Runtime **STOPPED**. Sin despliegue, trading, merge ni inicio de otra fase.
