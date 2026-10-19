# Informe Fase 4A-5D3 — Mark Price histórico causal

Repositorio `giomen0512-a11y/SHib-alt-b`. Rama `phase-4a-5d3-historical-mark-price`.
Base exacta de main: `5ba8525bedd409673d902ab6b78d706d099f8efa`.
Commit de implementación: 4a7db65242f48269c28e16276ec76f01a2cba2b4.
El SHA final de publicación documental se comunica mediante `git rev-parse HEAD` en la entrega.
Runtime **STOPPED**; exclusivamente OFFLINE, SYNTHETIC_FIXTURE. Entrega para auditoría independiente.

## Git y alcance

La tarea previa autorizó el FF-only de 4A-5D2. Se verificó main remoto en ae1a6aa y feature en
5ba8525, 0 behind/2 ahead. La copia local main atrasada se actualizó por FF hasta ae1a6aa y
posteriormente se integró la feature por FF hasta 5ba8525. Push normal y consulta remota
confirmaron exactamente ese SHA; no se crearon commits adicionales o commits de merge.

La solicitud detallada 5D3 fijó luego esa nueva base y el nombre exacto de rama. Se verificó
nuevamente GitHub, árbol limpio y SHA antes de implementar. Todo el desarrollo posterior
pertenece a esta feature; main permanece en 5ba8525 y no se integra 5D3.

Añadidos:

- `src/trading_engine/replay/historical_validation/mark_history.py`.
- `src/trading_engine/replay/historical_validation/mark_source.py`.
- `tests/unit/replay/test_historical_mark_price.py`.
- `tests/unit/replay/historical_mark_price_benchmark.py`.
- `tests/unit/replay/fixtures/historical_mark_price_baselines.json`.
- `docs/HISTORICAL_MARK_PRICE_ADAPTER_SPEC.md`.
- `docs/PHASE_4A_5D3_REPORT.md`.
- `docs/fixtures/historical_mark_price_benchmarks.json`.

Modificados:

- `src/trading_engine/replay/models.py`: vínculo opcional a sesión Mark, omitido cuando None.
- `src/trading_engine/replay/runner.py`: validación localizada del vínculo y vigencia Mark.
- `docs/FINANCIAL_PERFORMANCE_SPEC.md`: enlace a la continuación opt-in de fixtures.
- `docs/HISTORICAL_SOURCE_CATALOG.md`: enlace y límites de autorización.

No se editaron tests existentes ni dependencias. AltBEngine, configuración, matemática,
filtros, vetos, PreparedTradeHistory, ModeledQuotes, SimulatedExecution, ledger, performance,
fórmulas de riesgo y reservas conservan su contenido. La fuente Mark reutiliza AdapterLimits
/PreparedBlock; no convierte PreparedTradeHistory ni Candle de Trade en Mark.

## Arquitectura y contratos reutilizados

HistoricalDatasetRegistry → HistoricalQuery o SegmentedHistoricalQuery →
HistoricalReconciliation MARK → PreparedMarkHistory → HistoricalMarkPriceSource →
FinancialPerformance → IsolatedMarginRisk con evidencia adicional independiente.

Se reutilizan MarkKline, HistoricalEvent, Contract, DatasetDeclaration, Registration,
ReconciliationReport, QueryStream y su finalización, SealedSegmentSession, FinancialEvent,
MarkEvidence, MarkPriceSource, Reference y todos los motores financieros aprobados.

PreparedMarkHistory recibe DatasetIds explícitos y categoría MARK_PRICE_KLINES_1M. Exige
BINANCE / BINANCE_USDM_FUTURES / 1000SHIBUSDC / PERPETUAL / USDC / 1m; no factor1000 adicional.
Validación, preflight y conciliación deben finalizar antes de publicar el snapshot. Se comparan
digest económico, cantidad, duplicados y bindings; un QueryStream debe terminar COMPLETE.
Gaps son cobertura incompleta y conflictos bloquean, sin preferencias por fuente.

SQLite temporal con cuotas, lectura ro/query_only, bloques de 1440 filas y SHA-256 en memoria.
Ordinal y payload completo integran la raíz. La lectura valida el hash completo antes de decodificar JSON o entregar eventos y
retiene como máximo uno. Adulterar cursor, payload, registro, segmentos, checksum o cache
bloquea. Los errores de lectura se propagan, sin fallback a Mark ausente. Cierre/TTL/fuente
segmentada alterada invalidan; los temporales se limpian. STRICT_FULL consume el snapshot
integral aceptado y no promete revalidar cada original por cada acceso posterior.

## Fuente, precios, autenticidad e identidad

Trade Price, bid/ask modelado, Mark, Index y referencia funding conservan sus categorías.
Solo MarkKline.close de un registro MARK aceptado puede producir esta muestra. Una vela Trade
seleccionada se rechaza, aunque tenga las mismas columnas o precios. Un fichero local con
categoría declarada no autentica al proveedor: la semántica y la procedencia se auditan por separado.

PreparedMarkEvidence mantiene **NOT_AUTHENTICATED** y
**NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES**. La publicación requiere una política explícita
`SYNTHETIC_FIXTURE_ONLY` y que todas las fuentes sean fixtures declarados con su evidencia.
Fuentes SOURCE_DECLARED, UNKNOWN o mixtas permanecen candidatas históricas y bloquean antes
de construir FinancialPerformance. No se modifica Reference para aceptar OBSERVED; MODELED
no elude el gate. No existe ruta de autorización de datos reales en esta fase.

HistoricalMarkEvent extiende FinancialEvent y conserva HistoricalEvent íntegro, OHLC, calidad,
EventId, DatasetIds/fuentes, apertura/cierre, recepción, secuencia, política, modo, identidad
preparada, etiqueta de sesión de verificación y autorización de fixture. La etiqueta de sesión
no sustituye el token opaco por preparación que vincula source y ReplaySnapshot.

Las identidades de dataset conservan el contrato de Registry: declaración, contenido, hash,
localización y procedencia explícitos. La identidad preparada incluye intervalo/cutoff, modo,
bindings y raíces; la identidad económica separada coincide entre modos del mismo input.
Los EventIds de muestra derivan determinísticamente del evento económico y política; no hay
UUID aleatorio, reloj real o duplicación por datasets equivalentes.

STRICT_FULL conserva FULL_SOURCE_VERIFIED. El modo segmentado conserva
SEGMENT_VERIFIED / SOURCE_NOT_REVALIDATED con TTL y estabilidad explícitos. No es autenticación
global ni equivalente a verificar todos los bytes actuales de todos los originales.

## Causalidad, latencia, frescura y cutoff

MarkKline mantiene apertura como market_effective_at y cierre como market_available_at.
MarkEvidence.effective_at es el **cierre** de esa muestra; FinancialEvent.received_at es cierre
más latencia explícita. Adquisición permanece en bindings. Nunca se entrega antes de recepción
ni después del cutoff. Los límites subminuto, incluido un microsegundo posterior, se comprueban
exactamente. Verificar un bloque físico no habilita sus minutos futuros económicamente.

La política exige latencia, frescura, hipótesis, gate y límite de eventos por lectura. Los tests
usan latencias sintéticas 0/250/70000 ms; ninguna se presenta como observada o calibrada en Binance.
Para cierres 1m, frescura debe ser inferior a 60000 ms: una muestra no acredita el siguiente
cierre ausente. No hay valor financiero predeterminado. Una entrega más tardía que su frescura
conserva la muestra obsoleta; performance no la reetiqueta como actual.

Un gap no produce evento o precio artificial. La primera muestra después del gap tiene
continuous=False; la siguiente consecutiva recupera continuidad. La primera muestra del inicio
solicitado es válida si coincide con ese inicio. La trayectoria intraminuto permanece desconocida.
Sin Mark fresco/continuo, las métricas dependientes son NOT_AVAILABLE. No interpolación,
forward-fill silencioso, sustitución Trade o uso del High/Low como tick observado.

## Casos numéricos A–H y motores financieros

| Caso | Resultado ejecutado |
| --- | --- |
| A: BUY 10×100; Mark95 | Cantidad10, costo1000; flotante **−50** exacto. |
| B: BUY10×100; SELL5×110; Mark95 | Cantidad5, costo500; realizado50 y flotante **−25** separados. |
| C: vela abierta T, cierre T+1m | Close no disponible antes de cierre; con latencia250ms no se publica hasta cierre+250ms. |
| D: Mark faltante | Ningún evento del minuto ausente; muestra previa vence y la recuperación respeta continuous. |
| E: registro en dos datasets equivalentes | Tres registros duplicados se concilian en tres eventos únicos, con ambas fuentes. |
| F: dos precios contradictorios | CONFLICTED bloquea preparación/publicación en ambos modos. |
| G: capital, fees y funding desconocidos | Flotante calculable con exposición confirmada; wallet/equity/neto/costos faltantes permanecen NOT_AVAILABLE. |
| H: BUY10×100; Mark75; evidencias independientes | Flotante−250; margen inicial modelado250, balanceaislado0, mantenimiento7.50, buffer **−7.50**. Equity750 con capital1000 y costos/cobertura explícitos de fixture. |

FinancialLedger sigue siendo autoridad sobre exposición/costo; el adaptador no calcula PnL,
VWAP, equity, mantenimiento o umbral. Se conservan las ecuaciones y los validadores existentes.
Para H, tasas/brackets/capital/coberturas son fixtures independientes y explícitos; no se
obtienen del Mark. Riesgo conserva NOT_DETERMINABLE_FROM_SAMPLES para supervivencia.

Se compararon ejecución continua y fragmentada con siete cortes por modo usando capital
incompleto, y cuatro cortes por modo con capital/fees/cobertura de fixture para equity y
drawdown disponibles. Coinciden reportes, snapshots, eventos, secuencias, estados, valoraciones,
PnL, equity/drawdown, digests y conflictos; repetir run no duplica eventos.
Otro PreparedMarkHistory, incluso con mismas identidades deterministas, rechaza el snapshot.
El checksum no concede confianza ni permite recovery entre procesos. Una sesión cerrada bloquea
run antes de avanzar; los errores de confianza pueden propagarse al caller con su causa original.

## Escala, memoria y bloqueo explícito

El almacenamiento y la conversión Mark son streaming; memoria del adaptador acotada a un bloque.
El consumidor actual conserva `_financial_events`, `performance_pending`, journal y series para
el intervalo. No se modificó esa arquitectura. La fuente exige un límite de publicación por
lectura y tiene un techo de 10080 muestras (una semana de minutos), comprobado **antes** del
primer evento. El constructor de runner bloquea un rango que exceda el presupuesto explícito.
La preparación mayor puede consultarse por páginas y cursor, sin tuplas indiscriminadas.

Ese techo es una cota de recursos, no garantía de memoria o latencia de cualquier replay de una
semana. Los benchmarks siguientes miden el adaptador y la conversión, **no** el pico de journals
financieros de un replay de 30 días. Replay financiero 2024–2026 permanece bloqueado. Para ese
uso se propone agenda financiera por cursor y checkpoints del consumidor, con persistencia/
compactación de journals y autorización previa para una modificación estructural localizada.
No se implementa esa fase ni se oculta la limitación con los fixtures pequeños.

| Minutos | Modo | Validación s | Prep. s | Lectura s | Conversión s | Reanudación s | RSS bytes | SQLite bytes | Eventos/bloques |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | STRICT_FULL | 0.004 | 0.012 | 0.000 | 0.000 | 0.000 | 22134784 | 24576 | 20/1 |
| 20 | SEALED_SEGMENT_SESSION | 0.004 | 0.008 | 0.000 | 0.001 | 0.001 | 22441984 | 24576 | 20/1 |
| 1440 | STRICT_FULL | 0.388 | 0.863 | 0.021 | 0.029 | 0.048 | 26947584 | 1187840 | 1440/1 |
| 1440 | SEALED_SEGMENT_SESSION | 0.200 | 0.435 | 0.022 | 0.053 | 0.050 | 27041792 | 1187840 | 1440/1 |
| 10080 | STRICT_FULL | 1.590 | 4.831 | 0.188 | 0.752 | 0.602 | 30138368 | 8286208 | 10080/7 |
| 10080 | SEALED_SEGMENT_SESSION | 1.424 | 3.124 | 0.197 | 0.716 | 0.826 | 30154752 | 8286208 | 10080/7 |
| 43200 | STRICT_FULL | 6.448 | 18.769 | 0.532 | 1.953 | 2.030 | 30052352 | 35483648 | 43200/30 |
| 43200 | SEALED_SEGMENT_SESSION | 4.943 | 10.953 | 0.669 | 2.334 | 2.527 | 31215616 | 35483648 | 43200/30 |

Cada escenario ejecutó un proceso separado, fuente CSV sintética incremental y ambos modos.
Se midieron registro, validación inicial de sesión, preparación (incluye reconciliación), lectura,
conversión, reanudación, RSS máximo y tamaños SQLite. Conversión paginada continua/fragmentada
coincide exactamente; los digests económicos coinciden entre modos para cada duración.
La prueba de 30 días rechaza la lectura financiera única y permite páginas; se verifican 43200
eventos y 30 bloques, sin cargar el mes completo en una tupla. RSS incluye imports, registro,
sesión, snapshot y caches; no es memoria exclusiva del adaptador. No se extrapola a tres años.
Datos completos: `docs/fixtures/historical_mark_price_benchmarks.json`.

## Validación realmente ejecutada

Antes de editar producción se congelaron SHA de archivos y se ejecutaron los nueve fingerprints
legados contra main: nueve aprobados, 12.37 s. El fixture nuevo conserva esos nueve escenarios
completos y SHA de once módulos de estrategia/finanzas/datos sin cambios. La suite de integración
5D2 vuelve a verificar los fingerprints anteriores después del cambio. Los fingerprints de ledger,
performance, riesgo y reservas existentes permanecen bajo sus tests previos, que no se editaron.

| Ejecución | Comando | Tests | Tiempo | Resultado |
| --- | --- | ---: | ---: | --- |
| Pre-edición: fingerprints legados | `python -m pytest -q -p no:cacheprovider tests/unit/replay/test_historical_modeled_execution.py -k frozen_main` | 9 | 12.37 s | Aprobado; 0 fallos/errores/omisiones |
| Específicas 5D3 | `python -m pytest -q -p no:cacheprovider tests/unit/replay/test_historical_mark_price.py` | 90 | 51.36 s | Aprobado; 0 fallos/errores/omisiones |
| Regresiones de doce módulos | `python -m pytest -q -p no:cacheprovider <12 archivos detallados abajo>` | 710 | 543.77 s | Aprobado; 0 fallos/errores/omisiones |
| Completa host | `python -m pytest -q -p no:cacheprovider` | 2635 | 774.36 s | Aprobado; 0 fallos/errores/omisiones |
| Completa Docker | `python -m pytest -q -p no:cacheprovider` | 2635 | 974.79 s | Aprobado; 0 fallos/errores/omisiones |

Regresión: `tests/unit/replay/test_historical_validation.py`, `tests/unit/replay/test_historical_registry.py`, `tests/unit/replay/test_historical_fast_query.py`, `tests/unit/replay/test_historical_fast_query_scale.py`, `tests/unit/replay/test_historical_replay_adapter.py`, `tests/unit/replay/test_historical_modeled_execution.py`, `tests/unit/replay/test_financial_performance.py`, `tests/unit/replay/test_performance_replay.py`, `tests/unit/replay/test_isolated_margin_risk.py`, `tests/unit/replay/test_margin_risk_replay.py`, `tests/unit/replay/test_runner.py`, `tests/unit/replay/test_reserve_transfer_replay.py`.

Las suites se solapan; no se suman como tests únicos. Las primeras iteraciones detectaron y
corrigieron problemas de fixtures/tipado y verificaron el comportamiento de sesión cerrada.
Se interrumpieron ejecuciones completas preliminares al añadir las pruebas de equity/
drawdown y el endurecimiento del bloque; **no se consideran aprobadas**. La tabla recoge las ejecuciones finales con la
colección actual completa. La cifra histórica 2545 de 5D2 se distingue del recuento actual.

Ruff format --check: **193 archivos conformes**; Ruff check: **aprobado**; mypy --strict src tests: **193 archivos sin errores**, tanto host como Docker. `git diff --check` aprobado. No se relajaron validadores o tests.

Host Python3.12.14; Docker Python3.12.15. pytest8.4.2, Ruff0.13.3 y mypy1.18.2 fijados por
pyproject.toml; dependencias locales preexistentes, sin instalar ni descargar paquetes/imágenes.
Host ejecutado con filtro seccomp que bloquea socket/connect, heredado por subprocess.
Docker usa imagen local `sha256:654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf`,
`--pull=never --network=none --read-only`, repo /app montado readonly, UID/GID1000:1000,
cap-drop=ALL, no-new-privileges y /tmp temporal. /evidence es el único mount de resultados escribible.

## Reproducción

Con Python3.12 y las dependencias dev aprobadas ya disponibles, sin red:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/replay/test_historical_mark_price.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy --strict src tests
git diff --check
```

Benchmarks: repetir cada combinación de `--minutes 20|1440|10080|43200` y
`--mode STRICT_FULL|SEALED_SEGMENT_SESSION` en un proceso nuevo:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m tests.unit.replay.historical_mark_price_benchmark --minutes 10080 --mode STRICT_FULL
```

Docker, únicamente con la imagen ya disponible localmente:

```sh
docker run --rm --pull=never --network=none --read-only --user 1000:1000 \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --mount "type=bind,src=$PWD,dst=/app,readonly" \
  --tmpfs /tmp:rw,exec,mode=1777 -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/app/src -w /app \
  sha256:654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf \
  python -m pytest -q -p no:cacheprovider
```

En este entorno gestionado se seleccionó explícitamente el daemon local con
`env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock`.
Los comandos de ejemplo no descargan dependencias ni habilitan servicios.

## Limitaciones, datos externos y siguiente dependencia

No se aportaron datasets reales: DATASET_NOT_PROVIDED. Una fuente real correctamente validada
localmente sigue NOT_AUTHENTICATED y financieramente bloqueada. Se requiere contrato independiente
de autenticación/autorización y procedencia protegida; ni SHA local ni sesión rápida lo reemplazan.

Mark1m no da ticks, orden intraminuto, supervivencia o liquidación oficial. Tampoco aporta fees,
pagos de funding, brackets históricos, capital del usuario, margen o transferencias observadas.
Esas dependencias siguen separadas. No hay ejecución observada, calibración real, datos externos,
replay financiero multianual, recovery LIVE, Add Margin real, Telegram, VPS o servicios.

Siguiente subfase recomendada: **funding histórico causal**, con inventario/calendario explícito,
publicación por evento, referencia Mark exactamente correspondiente, exposición confirmada y gate
de autorización independiente. Una tasa histórica no es un pago del bot. Fees requerirán además
rol maker/taker y reglas/tier vigentes; brackets/capital requieren sus evidencias propias.
No se inicia 4A-5D4.

**Preparación para auditoría: LISTA**, con los límites explícitos de fixtures, autenticación real pendiente y Replay financiero multianual bloqueado. Las verificaciones ejecutadas no detectan regresiones. Este informe de implementación no sustituye la auditoría independiente ni autoriza merge, despliegue o trading.

## Cierre

Feature construida desde 5ba8525 con un commit de implementación y un commit documental: 2 ahead, 0 behind respecto a esa base. La entrega final comunica SHA exacto, ahead/behind y confirmación remota después del push. main debe conservar 5ba8525; no se realiza merge de esta feature. Árbol de trabajo limpio al cerrar y fuentes/tests idénticos a los hashes congelados de las ejecuciones finales.

Se conserva Alternativa B LONG/ISOLATED/ONE_WAY/4×, presupuesto1200, Base+DCA7, TP0.80%, SL/trailing
OFF, desviación1.50%, multiplicadores1.15/1.40, pesos59/69, sin reinversión, warmup1560, filtros/vetos
y BaseT+120. Runtime comprobado STOPPED. Feature publicada para auditoría, sin merge de 5D3,
sin despliegue, credenciales Binance, conexiones privadas, órdenes o transferencias reales.
