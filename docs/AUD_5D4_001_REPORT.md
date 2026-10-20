# AUD-5D4-001 — Confianza antes de entregar tasas históricas

Fecha: 2026-10-20 (America/El_Salvador).
Repositorio: `giomen0512-a11y/SHib-alt-b`.
Rama exclusiva: `phase-4a-5d4-historical-funding`.
HEAD anterior exacto: `8bfefb4602d5a43b80b75c7f2184718465a450af`.
Main remoto aprobado: `b483a1c531e16903f98f2bceac13444974b96ca5`.
Correctivo probado; pendiente de **AUDITORÍA INDEPENDIENTE**.
El commit correctivo es el que contiene este informe; su SHA se obtiene con
`git log -1 --format=%H -- docs/AUD_5D4_001_REPORT.md`.

## Recuperación y alcance

Se confirmó un workspace funcional en `/workspace/SHib-alt-b`, lectura de Git local
y acceso real a `git ls-remote origin`. Ambos SHA remotos coincidieron con el
encargo. El checkout inicial estaba limpio en `work`, sobre el main aprobado.
La referencia **local** `main` apuntaba a
`141eb9e119a27283ee1552e82f6a7fa9022cd8f7`; se dejó intacta. No se confundió esa
referencia desactualizada con el main remoto.

Se leyó AGENTS.md, se descargaron únicamente las referencias remotas pertinentes
y se cambió a la feature exacta, limpia, antes de editar. El primer intento de
activar tracking falló porque el fetchspec del clon solo contemplaba main; se
completó el cambio con `--no-track`, comprobando de nuevo HEAD y árbol limpio.

El Python global no tenía pytest. Se verificó el `.venv` existente del workspace:
Python 3.12.14, pytest 8.4.2, Ruff 0.13.3 y mypy 1.18.2. No se instalaron dependencias.
No se consumieron archivos ni evidencias de una ejecución anterior en `/tmp`.

Solo cambia producción en `funding_source.py`. Se añade un archivo de pruebas,
este informe y una precisión de HISTORICAL_FUNDING_ADAPTER_SPEC.md. No se cambian
tests existentes, fórmulas financieras, fixtures ni fingerprints de referencia.

## Reproducción antes de corregir

`read()` comprobaba confianza antes de evaluar los argumentos y construir
HistoricalFundingRateEvent. El `yield HistoricalFundingRateEvent(...)` entregaba el
objeto directamente. Una invalidación dentro del constructor quedaba sin detectar
hasta otra iteración; el consumidor ya había recibido una tasa inválida.

El nuevo test envuelve el constructor real, crea el candidato y después invalida
la confianza antes de devolverlo al generador. No sustituye `check()`, `identity`,
los validadores de preparación/sesión ni el mecanismo real de detección de archivos.
Se prueba invalidación tanto en la primera entrega como después de un evento válido.

| Invalidación en construcción | Modos | Error exigido |
| --- | --- | --- |
| PreparedFundingHistory.close() | STRICT_FULL y SEALED | PREPARED_FUNDING_NOT_AVAILABLE |
| SealedSegmentSession.close() | SEALED | VERIFICATION_NOT_AVAILABLE |
| Reloj posterior al TTL, inyección de monotonic | SEALED | SESSION_EXPIRED |
| Append al archivo original, detectado por stat productivo | SEALED | SOURCE_CHANGED |
| TTL monotónico real de dos segundos | SEALED | SESSION_EXPIRED |

En el caso TTL real, SessionPolicy(2, ...) se proporciona al abrir la sesión.
Se verifica vigencia dentro del constructor y se espera solo el tiempo restante
más 20 ms. No se modifica monotonic, `_started`, policy ni funciones de validación.

Contra producción idéntica al HEAD anterior: **12 fallos por DID NOT RAISE ValueError
y 8 aprobados**, tanto en host (6.42 s) como en Docker offline (6.53 s). Los doce
fallos son las seis variantes de la tabla, cada una sin prefijo y con prefijo válido.
Los ocho aprobados son seis controles de inventario y dos de excepción original.
Además, las 79 pruebas antiguas de funding pasaron antes del correctivo (3.22 s).

Una ejecución preliminar del nuevo test usó una lista con el encoder canónico que
exige tuple: se corrigió el test antes de la reproducción válida. Ese intento está
registrado como `before.log`; no se contabiliza como evidencia del defecto.

## Corrección y contratos

Diff productivo exacto:

```diff
diff --git a/src/trading_engine/replay/historical_validation/funding_source.py b/src/trading_engine/replay/historical_validation/funding_source.py
index 7618a02..23133a1 100644
--- a/src/trading_engine/replay/historical_validation/funding_source.py
+++ b/src/trading_engine/replay/historical_validation/funding_source.py
@@ -133,7 +133,7 @@ class HistoricalFundingSource:
             event = row.event
             if not isinstance(event.record, FundingRate):
                 raise ValueError("FUNDING_RATE_REQUIRED")
-            yield HistoricalFundingRateEvent(
+            result = HistoricalFundingRateEvent(
                 "historical-funding-rate-v1:" + digest((event.event_id, self.policy)),
                 row.sequence - 1,
                 event.market_available_at + timedelta(milliseconds=self.policy.latency_ms),
@@ -148,6 +148,8 @@ class HistoricalFundingSource:
                     else self.history.evidence.verification.session_identity
                 ),
             )
+            self.check()
+            yield result
         self.check()

     def inventory_at(self, until: datetime) -> FundingInventoryPublication | None:
```

El candidato permanece local hasta superar el último check. Si la sesión perdió
confianza, la excepción productiva se propaga antes de entregar el evento. No se
captura ni transforma: SOURCE_CHANGED y SESSION_EXPIRED llegan como primera causa,
sin reemplazarse por el NOT_AVAILABLE de una comprobación posterior. Si falla la
construcción, se conserva la misma instancia de su excepción, incluso si también
cerró la preparación. No se añade un finally que pueda enmascararla.

Las pruebas comparan el prefijo completo y su encoding canónico antes/después del
fallo, los candidatos frente a la lectura válida y el cursor calculado únicamente
desde entregas. La iteración falla explícitamente, queda terminada y una lectura
nueva con la sesión invalidada también falla. No hay entrega del candidato fallido,
avance ficticio de cursor, salto silencioso ni nueva construcción tras el fallo.
Las pruebas existentes de páginas, latencia, causalidad, deduplicación e identidades
permanecen sin cambios y se ejecutan de nuevo.

`inventory_at()` no cambia. Sus seis controles de construcción exigen los mismos
errores originales; se conservan los contratos existentes de None, UTC, intervalo,
cutoff, disponibilidad y latencia del inventario.

## Validación final

| Comprobación | Resultado |
| --- | --- |
| Adversarias nuevas | 20/20 aprobadas dentro del grupo funding |
| Funding específico, antiguas y nuevas | 99 passed, 9.98 s |
| AUD-5D3-001 | 22/22 aprobadas dentro del grupo financiero |
| Regresión financiera: ledger, performance, replay, Mark, riesgo y reservas | 473 passed, 772.40 s |
| Suite completa host | 2756 passed, 1692.74 s |
| Suite completa Docker offline | 2756 passed, 2294.93 s |
| Ruff format --check, host y Docker | 199 archivos conformes |
| Ruff check, host y Docker | All checks passed |
| mypy --strict src tests, host y Docker | Sin errores en 199 archivos |
| git diff --check | Aprobado |
| Bootstrap local y Docker | bootstrap_ready, mode=STOPPED; termina sin servicios |

Los XML host confirman cero fallos, errores y omitidos. Docker completa los mismos
2756 casos recogidos, todos aprobados. Los 20 casos nuevos incluyen seis variantes
de invalidación por dos posiciones del prefijo, seis de inventario y dos de
propagación de la instancia original de una excepción de construcción.

El host ejecuta pytest mediante un wrapper seccomp creado en esta ejecución que
deniega crear sockets IPv4/IPv6 con EPERM y se hereda por los procesos hijos.
Se verificó el bloqueo con una creación de socket que falla con PermissionError.
Las operaciones Git remotas se realizan fuera de ese wrapper.

Docker usa la imagen local preexistente, sin build ni descarga:
`sha256:654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf`.
Se verifican Python 3.12.15 y las mismas versiones fijadas de herramientas. Los
fuentes y tests actuales se montan en `/app` read-only; PYTHONPATH=/app/src evita
consumir el código antiguo instalado en la imagen. Usuario 1000:1000, --network none,
root read-only, cap-drop ALL, no-new-privileges, tmpfs /tmp, sin bytecode ni cache pytest.
Se inspeccionaron aislamiento y estado del contenedor durante la ejecución.
Dentro del contenedor se contrastaron los mismos 208 hashes del manifiesto host
y la ruta efectiva del módulo importado: `/app/src/.../funding_source.py`.
La reproducción Docker monta además el archivo productivo original sobre su ruta;
el resto del árbol y el nuevo test son los mismos que en la ejecución corregida.

Comandos principales desde el repositorio:

```sh
PYTHONPATH=src .venv/bin/python /workspace/aud-5d4-001/evidence/offline_exec.py \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/replay/test_funding_session_delivery.py \
  tests/unit/replay/test_historical_funding.py
PYTHONPATH=src .venv/bin/python /workspace/aud-5d4-001/evidence/offline_exec.py \
  .venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/python -m ruff format --check
.venv/bin/python -m ruff check
PYTHONPATH=src .venv/bin/python -m mypy --strict src tests
git diff --check
git diff 8bfefb4602d5a43b80b75c7f2184718465a450af HEAD
```

La prueba anterior se reproduce extrayendo el HEAD anterior en otro directorio y
añadiendo exclusivamente `test_funding_session_delivery.py` de este commit. Las
ejecuciones host registradas añaden XML JUnit y logs; las Docker registran stdout.

## Fingerprints e integridad

Se verifican **202/202 archivos legados protegidos**, con el manifiesto existente;
ningún baseline se regenera. Frente al HEAD anterior, el único archivo existente
modificado de src/tests es funding_source.py. Se registra SHA-256 de los 208
archivos actuales de src/tests, incluido el nuevo test.

| Elemento | SHA-256 |
| --- | --- |
| funding_source.py anterior | 14cb52871222f0d2b1b3c3e67f273641db04278cd007d479cd1aed34f55d0040 |
| funding_source.py corregido | 4a0a1ec11283a4c2d1836c2d532203d69ebad60c20e4011471c90c579f539483 |
| test_funding_session_delivery.py | 0cb660191d1342960b316025accb2f2810fae6cd2d78d1eaad63686416f3d280 |
| historical_funding_baselines.json sin cambios | 6115deb6fc7a5aeb1eeaa83a5b93fc0519a48c59f0ed93bbe7947278bb37d37c |
| Manifiesto src/tests, JSON canónico ordenado | 730a9958d841d0ca515a1ec73b246a5e1630ad5d1584615b9fdeaa8fcc44f428 |

La evidencia nueva se guarda en `/workspace/aud-5d4-001/evidence/`: reproducción
antes, tests exactos, producción anterior, logs/XML después, wrapper offline,
manifiesto y resumen de hashes. El patch exacto y los SHA de publicación se guardan
allí al crear el commit; no se pretende incluir el SHA de un commit dentro de sí mismo.

## Riesgos residuales y entrega

La comprobación final cubre la invalidación sincrónica durante construcción. No
constituye exclusión mutua entre hilos ni una transacción con un proceso externo que
modifique archivos después del check. Se conserva el contrato de sesión existente:
STRICT_FULL usa el snapshot preparado; SEALED comprueba TTL y stat, sin rehash
íntegro por evento ni autenticación independiente del proveedor.

La asignación financiera dinámica de funding por ronda sigue **expresamente pendiente**.
No se conecta esta capa a FinancialSource ni ReplayRunner ni se autorizan pagos.
No se inicia 4A-5D4B, LIVE, SHADOW, Binance privado, trading, transferencias o VPS.

Se prepara un solo commit correctivo sobre el HEAD indicado: feature tres commits
ahead / cero behind respecto a main aprobado (los dos existentes y este correctivo).
La publicación debe dejar feature cero ahead / cero behind respecto a su remoto;
la comprobación final de las referencias se entrega junto al SHA nuevo. Main remoto
debe continuar en b483a1c; main local permanece intacta. Sin merge ni force push.
Runtime verificado STOPPED en host y Docker; el bootstrap solo comprueba configuración
y termina. Tras publicar se detiene el trabajo para **AUDITORÍA INDEPENDIENTE**.
