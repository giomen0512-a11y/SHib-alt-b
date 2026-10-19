# AUD-5D3-001 — Validación de sesión en la entrega financiera

Repositorio: giomen0512-a11y/SHib-alt-b. Rama exclusiva:
`phase-4a-5d3-historical-mark-price`.
HEAD auditado: `790fc39ef249c907d2c1bcdb29227776d3a00209`.
Main aprobado: `5ba8525bedd409673d902ab6b78d706d099f8efa`.
Runtime STOPPED. Sin merge, despliegue, LIVE, trading ni inicio de 4A-5D4.

## Flujo y protección previa

El constructor de ReplayRunner materializa las fuentes financieras hasta request.end.
El snapshot conserva los eventos pendientes. run() valida la fuente Mark al entrar,
agenda por received_at y llama a _financial_tick() después de los hechos y cierres.
El tick observa el ledger, los eventos coincidentes y VALUE en un estado local;
después registra la transición y confirma estado, cursor y pendientes en el snapshot.

El HEAD auditado sí comprueba indirectamente la sesión en cada observación:
FinancialPerformance.observe → _time → FinancialPerformance.identity →
HistoricalMarkPriceSource.identity → check → PreparedMarkHistory.check →
SealedSegmentSession.check (cuando corresponde). _time se ejecuta fuera del bloque
que transforma errores contables en conflictos. La pérdida de confianza se propaga.
Por eso la memoria de pendientes no permite por sí sola reconocer un nuevo Mark si
la fuente ya era inválida al entrar en observe. Esta protección no se elimina.

## Secuencia independiente dentro de un solo run()

Los tests usan registro MARK sintético real, preparación real, SQLiteHistory,
FinancialPerformance y ReplayRunner. No sustituyen check ni identity. Se construye
el runner con la fuente válida (materializando los futuros eventos); se inicia una
única llamada run(); se deja reconocer un prefijo con Mark; dentro de esa misma
llamada se invalida la fuente antes de un siguiente evento pendiente.

Se cubren cuatro fallos: PreparedMarkHistory.close(), SealedSegmentSession.close(),
reloj monotónico posterior al TTL (sin sleeps) y cambio del archivo original que
produce SOURCE_CHANGED. El último y el TTL corresponden al contrato SEALED; el
cierre de preparación también se prueba en STRICT_FULL.

Los puntos de inyección son anteriores al tick, posteriores a observar el ledger,
posteriores al Mark y posteriores a VALUE pero anteriores a confirmar el tick.
Los hooks llaman siempre a los métodos financieros originales. Se comparan el
PerformanceState completo, cursor, cola pendiente y emisiones financieras con el
prefijo anterior. Cierre y renovación entre dos run() no sustituyen esta prueba.

En la prueba final, el intervalo es 2026-01-02 11:45–12:05 UTC, con 20 cierres
Mark sintéticos de precio 95. El prefijo conserva 17 Mark (11:46–12:02), un fill
del fixture a las 12:02:01 y su duplicado idempotente a las 12:02:02, con
posición abierta y unrealized negativo. A las 12:03 el siguiente Mark ya está
pendiente en memoria. El hook after_value ejecuta observe(VALUE) original y luego
invalida la fuente antes de devolver su resultado local al tick. El código anterior
confirma el Mark número 18, su valoración y cursor pese a esa invalidación. El
código corregido falla a las 12:03 y conserva los 17 Mark, sus series/journal,
ledger, cursor y pendientes anteriores a ese tick. Los otros tres hooks invalidan
antes del tick, tras observe(ledger) o tras observe(Mark), siempre dentro del mismo run().

Existe una ventana adicional al terminar VALUE: si la fuente pierde confianza
después de que observe devuelve el estado local, el HEAD auditado confirma ese tick
sin una nueva comprobación. Una observación posterior o report falla, pero el snapshot ya
contiene el Mark, la valoración y el cursor del tick. No equivale a demostrar que
observe acepte una fuente previamente inválida; demuestra una confirmación local
sin vigencia de sesión. La corrección cubre explícitamente ambas fronteras.

## Cambio localizado

Solo se modifica código productivo en replay/runner.py. Un helper privado comprueba
HistoricalMarkPriceSource en la entrada del tick, antes de entregar cada marks,
antes de VALUE y antes de registrar/confirmar el tick. También se comprueba el
cierre de run() antes de avanzar a target y la entrada de report(). Si se pierde
confianza durante el manejo de una excepción, se propaga la causa original sin
crear un conflicto artificial en ledger ni volver a observar performance/riesgo.

Los estados locales que no superan la validación final no se confirman; el prefijo
financiero previamente reconocido permanece íntegro. No hay rollback del reloj o
de hechos estratégicos válidos procesados antes del fallo. No se publica un report
financiero usando una fuente inválida. La misma sesión invalidada no permite reanudar.

check() consulta disponibilidad y, para SEALED, TTL y stat de los originales; no
relee el dataset ni rehace su SHA. No se cambia el contrato STRICT_FULL, los motores,
las fórmulas, políticas, autorización de fixtures o formato de snapshots. Ningún
fingerprint de referencia se regenera.

## Validación

La evidencia local se conserva en `/tmp/shib-aud-5d3-001/evidence/`: logs, XML JUnit,
versiones, aislamiento Docker, wrapper seccomp, test exacto de reproducción y
manifiesto SHA-256 de todos los archivos de código/tests/fixtures ejecutados.

Reproducción inicial contra el runner de 790fc39: 12 casos pasan preservando el
estado financiero reconocido y 4 fallan al invalidar después de VALUE (192,23 s).
Se conserva el test exacto de esa ejecución como audited-reproduction-test.py en
la evidencia. La prueba final refuerza el caso con fills, una posición abierta y
unrealized disponible negativo, y exige además no emitir transiciones, no observar
ni alterar el ledger después de perder confianza.

En una extracción independiente con git archive del SHA auditado, los cuatro casos
after_value de esa prueba con posición abierta vuelven a fallar por cambio del
PerformanceState confirmado (49,42 s; 18 casos no seleccionados). Los fallos son
contraejemplos esperados anteriores a la corrección, no resultados aceptados de
la suite corregida. Los XML y logs separan ambas ejecuciones de las validaciones.

También se conserva una ejecución preliminar interrumpida de las pruebas nuevas:
el test asumía una cantidad de fill incorrecta. Se sustituyó esa suposición por la
comprobación de exposición real y unrealized negativo; no se cambió ningún fixture,
fórmula o código financiero para satisfacerla. No se contabiliza como validación.

Comandos principales (Python 3.12; red bloqueada con el wrapper adjunto):

```sh
PYTHONPATH=src python3.12 /tmp/shib-aud-5d3-001/evidence/offline_exec.py \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/replay/test_mark_session_delivery.py

PYTHONPATH=src python3.12 /tmp/shib-aud-5d3-001/evidence/offline_exec.py \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/replay/test_historical_mark_price.py \
  tests/unit/replay/test_runner.py \
  tests/unit/replay/test_performance_replay.py \
  tests/unit/replay/test_margin_risk_replay.py \
  tests/unit/replay/test_reserve_transfer_replay.py \
  tests/unit/replay/test_historical_modeled_execution.py

PYTHONPATH=src python3.12 /tmp/shib-aud-5d3-001/evidence/offline_exec.py \
  .venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy --strict src tests
git diff --check
```

Las ejecuciones registradas añaden --junitxml y PYTHONDONTWRITEBYTECODE=1. El
contenedor utiliza la imagen local ya disponible
`sha256:654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf`,
sin pull ni instalación; Python 3.12.15 frente a 3.12.14 del host, pytest 8.4.2.
Se usa Docker local con `--network=none --read-only --user 1000:1000 --cap-drop=ALL
--security-opt=no-new-privileges`, repo en /app readonly, evidencia en /evidence,
tmpfs /tmp, PYTHONPATH=/app/src y `python -m pytest -q -p no:cacheprovider`.
docker-isolation.txt registra las propiedades inspeccionadas durante la ejecución.

Para reproducir el contraejemplo sin modificar esta rama: extraer
`git archive 790fc39ef249c907d2c1bcdb29227776d3a00209` en un directorio temporal,
copiar audited-position-counterexample-test.py de la evidencia como
tests/unit/replay/test_mark_session_delivery.py, usar PYTHONPATH apuntando al src
de esa extracción y ejecutar ese módulo con `-k after_value`. Se esperan cuatro
fallos por diferencia del PerformanceState. counterexample-provenance.json verifica
que el runner extraído coincide byte a byte con el objeto Git auditado.

| Comprobación | Resultado |
| --- | --- |
| Tests nuevos, versión final | 22 passed, 245,48 s |
| Regresiones relevantes | 325 passed, 354,67 s |
| pytest completo host offline | 2657 passed, 1011,07 s |
| pytest completo Docker offline | 2657 passed, 1298,18 s |
| Ruff format --check | 194 archivos conformes |
| Ruff check | PASS |
| mypy --strict src tests | PASS, 194 archivos |
| git diff --check | PASS |

Las regresiones conservan las referencias congeladas de NoQuotes, FixtureQuotes,
FixtureFacts y ejecución modelada, el fingerprint previo con ledger, y los hashes
de módulos financieros/estratégicos protegidos. La equivalencia continuo/fragmentado
se compara mediante report y snapshot completos en STRICT_FULL y SEALED, además
de las regresiones financieras/riesgo/reservas existentes. No se modifica ningún
archivo de baselines ni módulo de estrategia, PnL, equity, Mark o riesgo.
