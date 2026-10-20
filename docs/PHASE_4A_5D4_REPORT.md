# Informe Fase 4A-5D4 — Preparación de funding causal

Base exacta: `b483a1c531e16903f98f2bceac13444974b96ca5`.
Rama: `phase-4a-5d4-historical-funding`.
**Dictamen: BLOCKED_WITH_FINDINGS para integración financiera dinámica.**
La capa preparatoria se entrega para revisión; no se declara funding histórico
integrado en ReplayRunner. Runtime STOPPED, fixtures sintéticos, offline.

## Decisión y alcance autorizado

Se verificó main remoto exacto antes de crear la feature y se congelaron 202
fingerprints de src/tests/fixtures antes de editar producción. El encargo permite
preparación segura y diseño pendiente si A–D exigen reestructurar los motores.
Se aplica esa alternativa, sin modificar ningún archivo productivo o test previo.

| Objetivo A–D | Resultado |
| --- | --- |
| A: preparación histórica independiente | Implementada, STRICT_FULL y SEALED_SEGMENT_SESSION. |
| B: tasa publicada sin pago | Implementada, gate fixture explícito, inventario con recepción independiente. |
| C: pago dinámico al demostrar exposición | Pendiente de contrato de agenda, asignador y finalización de exposición. |
| D: snapshots financieros con esos pagos | Pendiente de identidad y reconstrucción de eventos generados durante run. |

Hallazgos arquitectónicos:

- FND-5D4-001: FinancialSource.read construye FinancialEvent antes de ejecutar fills;
  FundingEvidence ya exige RoundId y cantidad. El runner y su validador mantienen esa
  agenda estática; no hay puerto de tasas ni journal de asignación dinámica.
- FND-5D4-002: inventario de tasas [start,end) no equivale al inventario de pagos
  [start,end] de CoverageEvidence. Tampoco acredita exposición, referencia o todos
  los pagos. Falta aprobar el tratamiento del punto end y su disponibilidad.
- FND-5D4-003: una omisión por exposición cero necesita registro y finalización:
  un fill tardío podría cambiarla sin existir FundingEvidence previo que permita
  al validador actual detectarlo. No se inventa un watermark implícito.
- FND-5D4-004: autenticación independiente e inventario económico de fuentes reales
  pendientes. OBSERVED sigue bloqueado; MODELED no sirve para blanquear un dataset real.

Diseño detallado, cambios de contrato necesarios y pruebas de aceptación pendientes:
[HISTORICAL_FUNDING_ADAPTER_SPEC.md](HISTORICAL_FUNDING_ADAPTER_SPEC.md), sección
«Diseño pendiente para revisión». Ninguno de esos cambios de motores se implementó.

## Código entregado y conservación

Nuevos funding_history.py y funding_source.py: snapshot SQLite read-only atómico,
raíces por bloques, orden por disponibilidad, cursor, límites, conciliación,
inventario, metadatos y gate fixture. No importan FundingEvidence ni calculan pagos.
Reutilizan las consultas, sesiones, parsers y contratos existentes.

Nuevos test_historical_funding.py, historical_funding_benchmark.py y fixture de
fingerprints. Se crean este informe y la especificación, con referencia en catálogo.
Los 202 archivos protegidos permanecen idénticos al main aprobado, incluidos
AltBEngine, ledger, performance, Mark, precios modelados, simulador, riesgo, reservas,
runner, snapshot, AUD-5D3-001 y todos los tests/baselines antiguos. No se regeneran
fingerprints legados. Sin cambios de estrategia, capital, 4x, DCA, TP o Base T+120.

## Casos y fronteras

| Casos solicitados | Evidencia y límite |
| --- | --- |
| A/B | ±0.0001, LONG 10, referencia 100: −0.10/+0.10 USDC; asignación y coberturas manuales fixture, no runner dinámico. |
| C/D/E/F/K/P | Los contratos existentes de exposición se regresionan; el nuevo asignador para cero, antes/después, empate y rondas sucesivas sigue pendiente. |
| G | Mark ausente se conserva; reference_status INCOMPLETE y ninguna conversión a pago. |
| H | Duplicados equivalentes producen una tasa y conservan ambas fuentes. No prueba dedupe de pagos dinámicos aún inexistentes. |
| I | Tasas contradictorias bloquean preparación; conflicto aún no disponible no contamina prefijo válido. |
| J | Calendario explícito irregular, faltantes, inventario ausente/tardío, incompatibilidad y vacío explícito, separados de cobertura financiera. |
| L | Recepción tardía conserva instante efectivo, incluso después de end; no reescribe valores pasados. Asignación económica tardía pendiente. |
| M | Declaradas reales/UNKNOWN preparables para revisión; publicación bloqueada; gate rechaza MODELED/OBSERVED. |
| N | Páginas/cursor exactos y EventIds económicos entre modos; reanudación financiera funding pendiente. Regresiones Replay legadas preservadas. |
| O | Cierre, TTL y SOURCE_CHANGED durante una publicación bloquean nuevas tasas; inventario valida antes de devolver. Tick financiero funding pendiente; correctivo Mark se regresiona. |

Se prueban también: hash antes de JSON, manipulación de cache/raíces, consultas
PROVISIONAL, límites de filas/disco/publicación, IDs/cursor inválidos, cutoff exacto,
disponibilidad desordenada respecto al instante efectivo, dos bloques/cache único,
identidad determinista/token distinto y no relectura de originales por evento.

## Resultados financieros sintéticos manuales

El test entrega una tasa fixture al contrato FundingEvidence manualmente con RoundId,
cantidad y referencia explícitos. CoverageEvidence procede de un inventario económico
manual independiente: no convierte FundingInventory ni demuestra integración C/D.
Se usa FinancialPerformance sin modificar su fórmula ni crear un asignador alterno.

| Hipótesis | Rate +0.0001 | Rate −0.0001 |
| --- | --- | --- |
| LONG / referencia funding | 10 / 100 USDC | 10 / 100 USDC |
| Funding LONG | −0.10 USDC | +0.10 USDC |
| PnL bruto / fees explícitos | 0 / 0 | 0 / 0 |
| Neto cash / wallet (capital fixture 1000) | −0.10 / 999.90 | +0.10 / 1000.10 |
| Equity con Mark 110 | 1099.90 | 1100.10 |
| Equity posterior con Mark 90 | 899.90 | 900.10 |
| Drawdown máximo de esas dos muestras | 200 USDC | 200 USDC |

Son ejemplos MODELED de fixtures autorizados, no rentabilidad histórica real. Riesgo,
liquidación y demás dependencias no aportadas permanecen NOT_AVAILABLE. No se invoca
Binance, servicio operativo, cuenta, orden, transferencia o cambio de margen real.

## Validación y benchmarks

Resultados realmente ejecutados en esta fase. El total histórico de 2657 tests es
solo referencia; no se reutiliza como validación nueva.
Evidencia de esta fase: `/tmp/shib-funding/evidence/`.

| Verificación nueva | Resultado |
| --- | --- |
| Funding específico | 79 passed, 1.82 s |
| Regresiones requeridas | 619 passed, 557.44 s |
| Suite completa host | 2736 passed, 1067.36 s |
| Suite completa Docker offline | 2736 passed, 1345.12 s |
| Ruff format --check / Ruff check | PASS, 198 archivos |
| mypy --strict src tests | PASS, 198 archivos |
| git diff --check | PASS |

Los XML confirman 2736 tests en cada suite completa: los 2657 existentes más los
79 nuevos. Cero fallos, errores y casos omitidos en ambas suites y en las ejecuciones
específica y de regresiones; no se infieren los resultados desde la colección.
Las regresiones incluyen test_historical_mark_price, test_mark_session_delivery,
test_financial_performance, test_financial_ledger, test_runner,
test_performance_replay, test_historical_registry, test_historical_fast_query,
test_historical_validation, test_margin_risk_replay y test_reserve_transfer_replay.

79 pruebas específicas finales pasaron en 1,82 s antes de las suites generales.
Ruff format/check y mypy --strict src tests pasaron sobre 198 archivos Python.
Host Python 3.12.14, Docker Python 3.12.15, pytest 8.4.2, Ruff 0.13.3 y mypy 1.18.2.
Las ejecuciones preliminares se conservan diferenciadas; una prueba de cuota de
disco tuvo que ampliar el fixture para superar realmente 16 KiB. No se modificó
producción para satisfacer esa expectativa ni se cambió un test anterior.

Los benchmarks independientes usan 1, 7 y 30 días, ambos modos, calendario explícito
de estrés de una muestra/minuto y disponibilidad variable. No es frecuencia real
Binance. Los casos incompletos conservan el inventario esperado y omiten una fila
central. Preparación y entrega se miden por separado; consumidor stream/count,
sin listas de años ni journal financiero. Inventory mantiene su tuple O(esperados),
raíces O(bloques) y cache máximo 1440 filas. Tracemalloc se activa después de registro
y, en SEALED, después de abrir la sesión: sus picos no incluyen esas entradas ya
creadas; RSS de proceso sí incluye todo el trabajo anterior. No se extrapola a
Replay multianual ni se confunde el benchmark de tasas con memoria de performance.
preparation_seconds mide el constructor PreparedFundingHistory: excluye registro
y apertura de SealedSegmentSession. El registro tiene tiempo separado en JSON;
la apertura SEALED no se cronometra aquí. La tabla no compara tiempo total de los
dos modos ni permite inferir un speedup global del proceso.

Las ocho ejecuciones terminaron correctamente; datos completos y picos Python en
[historical_funding_benchmarks.json](fixtures/historical_funding_benchmarks.json).
Se informa memoria RSS de cada proceso independiente, no una estimación del allocator.

| Días | Modo | Inventario | Tasas | Preparar s | Entregar s | Disco MiB | RSS MiB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | SEALED_SEGMENT_SESSION | COMPLETE | 1440 | 2.39 | 0.32 | 2.26 | 30.21 |
| 1 | STRICT_FULL | COMPLETE | 1440 | 3.52 | 0.21 | 2.26 | 30.23 |
| 7 | SEALED_SEGMENT_SESSION | COMPLETE | 10080 | 17.10 | 3.73 | 15.80 | 38.03 |
| 7 | STRICT_FULL | COMPLETE | 10080 | 19.83 | 2.36 | 15.80 | 38.88 |
| 30 | SEALED_SEGMENT_SESSION | COMPLETE | 43200 | 71.78 | 17.38 | 67.68 | 46.04 |
| 30 | STRICT_FULL | COMPLETE | 43200 | 107.39 | 15.59 | 67.68 | 51.96 |
| 30 | SEALED_SEGMENT_SESSION | INCOMPLETE | 43199 | 70.97 | 14.41 | 67.68 | 45.88 |
| 30 | STRICT_FULL | INCOMPLETE | 43199 | 109.81 | 14.97 | 67.68 | 51.94 |

El período de 30 días conserva 30 bloques; el caso incompleto omite exactamente una
tasa esperada. La preparación no carga una lista de tasas: usa SQLite y bloques.
El inventario esperado sí mantiene la tuple explícita prevista por su contrato.
La agenda y el journal financiero del consumidor no se han implementado ni medido.

Comandos de reproducción (solo herramientas locales, sin instalar dependencias):

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/unit/replay/test_historical_funding.py
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy --strict src tests
git diff --check
PYTHONPATH=src:. .venv/bin/python -m tests.unit.replay.historical_funding_benchmark \
  --days 30 --mode SEALED_SEGMENT_SESSION --incomplete
```

Las ejecuciones registradas añaden wrapper seccomp (socket/connect EPERM),
PYTHONDONTWRITEBYTECODE=1, `-p no:cacheprovider` y XML JUnit, con logs en evidencia.
Docker usa la imagen local
`sha256:654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf`,
--pull=never, --network=none, root read-only, usuario 1000:1000, capacidades quitadas,
no-new-privileges, repo /app readonly, evidencia /evidence writable y tmpfs /tmp.
Se usa PYTHONPATH=/app/src y el mismo pytest completo; no se descarga ni construye
una imagen, no se incluyen credenciales ni se invoca transporte al exchange.

## Entrega y límites

Solo feature, sin merge ni cambios de main. No se inicia 4A-5D5. Los SHA de commits,
ahead/behind, diff, manifiestos y confirmación remota se adjuntan en la entrega.
El informe integra un commit de la feature; no se crea un commit de informe en main.
Commit de implementación: `279626933d32a8ca450cb3556f83906124493936`, hijo directo
del main aprobado. Este informe se añade en un segundo commit descriptivo; el SHA
final y la comprobación de GitHub quedan en publication-final.json y en la entrega
externa para no introducir autorreferencia en el documento versionado.

La capa de preparación es revisable y fail-closed, pero faltan asignación causal
dinámica, autorización real e inventario económico para anunciar integración total.
Se entrega **BLOCKED_WITH_FINDINGS** y se detiene después de publicar la feature.
