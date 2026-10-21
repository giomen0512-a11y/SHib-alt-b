# Correctivo AUD-5D4B-001

Fecha: 2026-10-21 UTC. Alcance: fixture-only, offline, runtime STOPPED.

## Estado de entrega

**READY_FOR_INDEPENDENT_AUDIT.** Validación propia completada sobre el código final.
Este documento describe un correctivo propio, no una auditoría independiente ni
una autorización de merge.

Repositorio: `giomen0512-a11y/SHib-alt-b`.
Main comprobado: `cb8c13e21a8ac5f396209d6626279d6c1113b9cf`.
Feature: `phase-4a-5d4b-dynamic-funding`.
HEAD anterior: `26d3d66cfa1edc3e87dc9553f3bba3bf1c6af6e0`.
El commit correctivo es el que contiene este informe; su SHA se entrega después
de crearlo y verificar la referencia remota, sin un segundo commit documental.

## Causa raíz y corrección

`src/trading_engine/replay/dynamic_funding/core.py`, `_proof()`, antes líneas
250–267: `candidates[0]` elegía exclusivamente la primera declaración, ordenada
por recepción. Si esperaba un fill ejecutado después de T, el método devolvía
None sin considerar una segunda declaración compatible y completa para T.
El contrato no exige fronteras económicas crecientes entre declaraciones.

El único archivo productivo modificado es `dynamic_funding/core.py`. El cambio
está localizado en validate_fact (línea 195) y _proof (contraste en 253, selección
en 277). Examina todos los manifiestos causalmente recibidos con through >= T, verifica su
vínculo a FactSource y compara todos los IDs con el ledger hasta cada frontera.
Un faltante mantiene ese candidato incompleto; un fill confirmado no declarado
aborta por contradicción. Se completa el contraste antes de seleccionar, de modo
que una prueba completa no oculte una contradicción en otra declaración.

Entre pruebas completas se aplica el mínimo de `(received_at, through, identity)`.
La primera recepción conserva las elecciones previas sin empate. Frontera e
identidad estable resuelven empates explícitamente. Se mantienen las barreras de
confianza y se añade una tras examinar los candidatos, incluso si ninguno completa.
La ruta de finalización del simulador no cambia.

La validación adversaria adicional detectó que validate_fact protegía solo la
prueba elegida. Un fill posterior a T podía contradecir una declaración más amplia
ya recibida sin que se detectara. Se añade al mismo guard existente el contraste
del fill nuevo con todos los manifiestos recibidos, sin consultar hechos futuros.
El motivo de conflicto y el rollback siguen siendo los existentes. La reproducción
pública de componente y ReplayRunner fallaba por DID NOT RAISE antes de este guard
adicional: 2 fallos, 3.95 s. Los dos casos quedan incluidos en la matriz E.
Se comprobó además el mismo hueco en el HEAD original haciendo que el manifiesto
estrecho completo fuera el primero: no depende de introducir la selección nueva.
Evidencia: extra_manifest_check.py y unselected-original-head.log.

No se modifican FinancialPerformance._exposure, fórmulas monetarias, ReplayRunner,
modelos, identidad de fuentes, cursores, contratos de inventario, estrategia,
matching, ejecución, riesgo ni reservas. Solo se añaden pruebas y documentación.

## Reproducción anterior y resultado medido tras el correctivo

Se congelaron los SHA256 de los 289 archivos versionados antes de editar.
Se comprobaron Python 3.12.14 y dependencias instaladas, árbol limpio, SHA remotos
mediante Git contra GitHub, main ancestro y exactamente 2 ahead / 0 behind.
Los commits previos son e115710f5660db5ae1baeb1652bb50bb250c02a1 y el HEAD anterior.

Se ejecutaron dos pruebas nuevas contra producción sin modificar en ese HEAD.
También se extrajo una copia independiente con `git archive` del SHA anterior,
se verificaron sus bytes productivos y se copió únicamente el nuevo módulo de
regresiones. No hubo checkout temporal sobre main, reset ni sustitución de métodos.

Caso de componente: F1 BUY10 anterior a T; F2 posterior a T, entregado después del
fin solicitado. M1 cubre la frontera posterior y declara F1/F2; M2 cubre T y declara
solo F1, ya recibido. Ambos son compatibles y están publicados antes del fin.
Antes: PENDING_EXPOSURE, funding NOT_AVAILABLE, sin cobertura.
Después: ASSIGNED, -0.1000 USDC MODELED y cobertura.

Caso ReplayRunner: datos de mercado fixture SQLite, Base e intención DCA canónicas,
fill Base confirmado y fill DCA posterior a T recibido fuera del Replay. Ningún
método interno ni snapshot se sustituye para reproducir el hallazgo. Con M1/M2,
antes: PENDING_EXPOSURE y funding NOT_AVAILABLE. Después: ASSIGNED al
RoundId histórico, -0.016611111102769059082099656455944930844472100015331 USDC
MODELED, un solo fill confirmado, cursor funding 1 y cobertura publicada.
Estos importes son resultados sintéticos, no pagos ni rentabilidad observados.

## Matriz de pruebas adversarias nuevas

Módulo: `tests/unit/replay/test_funding_finality_selection.py`.
Todos los casos de la matriz aprobaron en las suites finales host y Docker.

| Requisito | Caso ejecutable |
| --- | --- |
| A: primero incompleto, segundo completo | test_a_public_component y test_a_public_runner |
| B: ambos incompletos | test_b_both_manifests_incomplete_remain_pending |
| C: primero completo | test_c_first_complete_manifest_keeps_its_original_proof |
| D: dos completos, desempates explícitos | test_d_complete_candidates_have_explicit_canonical_order, 6 variantes |
| E: contradicción junto a candidato completo | test_e_contradictory_manifest, 2 órdenes; test_e_previously_received_contradiction; dos test_e de contradicción tardía de manifiesto no elegido |
| F: publicación futura no utilizada | test_f_future_complete_manifest_cannot_finalize_early |
| G: NO_EXPOSURE sin pago cero | test_g_second_manifest_can_finalize_zero_without_zero_payment |
| H: fill tardío contradictorio | test_h_late_contradictory_fill, ReplayRunner y rollback del tick |
| I: rondas sucesivas y tasa tardía | test_i_late_funding_uses_closed_historical_round_after_reopening |
| J: continuo, fragmentado, restaurado | test_j_continuous_fragmented_and_restored_are_exactly_identical |
| K: confianza durante selección y antes del commit del tick | test_k_trust_loss, 8 variantes |
| L: modo OFF | test_l_opt_out_keeps_the_entire_legacy_snapshot; baselines originales |
| Fuente distinta | test_foreign_manifest_is_rejected_before_state_publication |

K utiliza close de preparación, close de sesión, cambio físico SOURCE_CHANGED y
TTL monotónico real. Los hooks llaman primero al getter/observe original y después
invalidan la sesión; no sustituyen validadores ni el reloj productivo. El TTL espera
el vencimiento de una sesión real de 10 s y conserva su política e inicio originales.
Se exige igualdad del snapshot completo, prefijo y ausencia de conflictos contables
ficticios. Los tokens opacos se comparan mediante la igualdad completa del snapshot;
los bytes de los demás campos y el digest se contrastan por separado.

J compara reportes y snapshots completos después de cortes antes, durante y después
de la publicación de M2: journal, cursores, IDs, exposición, importe, cobertura y
digest exactos. La reconstrucción mantiene los validadores originales.

También se compararon tres escenarios ya correctos entre el código anterior y el
corregido usando exactamente el mismo registro fixture inmutable y las mismas
identidades: un manifiesto completo; primero completo seguido de otro incompleto;
y dos completos con recepciones distintas. Los SHA256 del FundingState completo,
IDs de fuente, decisión, prueba y pago, cursores e importes resultan idénticos.
Evidencia: compare_approved.py, approved-before.json y approved-after.json. Estos
resultados externos no sustituyen ni regeneran un baseline financiero del repositorio.

## Validación

Resultados ya ejecutados, sin reutilizar cifras de una entrega anterior:

| Comprobación | Resultado |
| --- | --- |
| Dos reproducciones públicas en copia git archive del HEAD anterior, módulo definitivo | 2 fallos por PENDING_EXPOSURE, 4.05 s |
| Contraejemplo exacto de la auditoría, copia anterior | 1 fallo por PENDING_EXPOSURE, 1.12 s |
| Contraejemplo exacto con el código final | 1 aprobada, 1.14 s |
| Dos reproducciones públicas con el código final | 2 aprobadas, 4.72 s |
| Nuevos adversarios A–L, contradicción tardía y origen | 30 aprobadas, 83.56 s |
| Suite completa host final | 2837 aprobadas, 1833.65 s (30 min 33 s), exit 0 |
| Suite completa Docker offline final | 2837 aprobadas, 2206.35 s (36 min 46 s), exit 0 |
| Ruff format/check host y Docker | 207 archivos conformes |
| mypy --strict src tests host y Docker | 207 archivos, sin errores |
| git diff --check | Conforme |
| Fingerprints contra objetos Git aprobados | 202 conformes: 200 actuales + 2 archivos históricos |
| Tres escenarios correctos con bindings idénticos antes/después | Estado, IDs, cursor e importe idénticos |

El XML host confirma 0 fallos, 0 errores y 0 skips. La colección final conserva las
2807 pruebas anteriores y añade 30. Las dos suites finales se ejecutaron sobre los
mismos 219 archivos fuente/prueba/fixtures congelados, sin cambios durante ellas.
Las primeras suites completas se interrumpieron al ampliar la guardia; sus logs
superseded y las pruebas específicas intermedias no se contabilizan como resultados
del código final. Las suites finales sí repiten todos los grupos requeridos:

| Grupo dentro de la suite host final | Aprobadas |
| --- | --- |
| Funding dinámico previo | 39 + 12 |
| Nuevos adversarios | 30 |
| Funding histórico y AUD-5D4-001 | 79 + 20 |
| AUD-5D3-001 | 22 |
| FinancialLedger y replay financiero | 49 + 27 |
| FinancialPerformance y replay performance | 75 + 33 |
| Simulación y replay simulado | 60 + 24 |
| Riesgo y replay de riesgo | 86 + 30 |
| Reservas y replay de reservas | 39 + 22 |
| ReplayRunner y snapshots de dominio | 52 + 39 |

La restauración de ReplaySnapshot también se verifica en las 12 pruebas previas
de replay funding y la prueba J nueva. El detalle por módulo está en host-summary.json.
Host Python 3.12.14; Docker Python 3.12.15. Imagen local shib-alt-b:test:
`sha256:654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf`.
No se instalaron dependencias ni se descargaron datos. Host y Docker se ejecutaron
concurrentemente; sus tiempos no son una comparación controlada de rendimiento.

El módulo histórico de 202 hashes y todos los baselines existentes conservan sus
bytes. Los 200 archivos protegidos actuales coinciden directamente con la base
aprobada; las dos fuentes archivadas runner/models coinciden con sus blobs Git
históricos y sus archivos actuales mantienen los hashes independientes ya declarados.
No se modificó ninguna prueba anterior. Los hashes afectados se congelaron antes
de editar; se contrasta también que ninguna fuente o prueba cambie durante las suites.

SHA256 de core.py antes: `7b0e89603fa810df389fdfcfd45eb7d2c92746e49dab9f0e98b2335e7b70773e`.
SHA256 de core.py final: `7fa3c1582b4fe727602db610c85b644e94ae21e7c1cc5a1cd99100123312e9c2`.

Las primeras iteraciones del módulo nuevo corrigieron únicamente su arnés: los
tokens de sesión no admiten encoding canónico y FinancialPerformance debe recibir
cada evento con su propio instante de recepción. No se relajó ningún validador ni
se cambió código de producción para acomodar esos errores del arnés.

## Reproducción

En el checkout, con Python 3.12 y las dependencias dev ya instaladas:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q -s \
  tests/unit/replay/test_funding_finality_selection.py -k test_a_public
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/unit/replay/test_funding_finality_selection.py
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/mypy --strict src tests
git diff --check 26d3d66cfa1edc3e87dc9553f3bba3bf1c6af6e0..HEAD
```

Para contrastar el fallo anterior, extraer con git archive el SHA anterior en un
directorio externo, copiar solo el nuevo módulo de pruebas y ejecutar los dos
test_a_public con PYTHONPATH apuntando a src de esa copia. Deben fallar por
PENDING_EXPOSURE, no por configuración o importación. Sobre el correctivo deben pasar.

Las ejecuciones de esta entrega se realizan con sockets IPv4/IPv6 denegados en
host y Docker con --network=none, --pull=never y checkout montado readonly.
Los logs, XML, hashes previos y diff se conservan fuera del repositorio en un
paquete de evidencia; su ubicación y hash se entregan al publicar.
El paquete incluye reproduce_previous.py, que verifica los dos fallos anteriores
sin modificar el checkout original, y offline.py, usado como wrapper en host.

Comando Docker completo ejecutado desde este entorno:

```sh
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY \
  -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run --rm \
  --pull=never --network=none --read-only --user 1000:1000 \
  --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly \
  --tmpfs /tmp:rw,nosuid,nodev,size=805306368 -e PYTHONPATH=/app/src \
  shib-alt-b:test python -m pytest -q -p no:cacheprovider
```

## Límites y riesgos residuales

La regla mantiene IDs de escenarios anteriores sin empate donde la primera prueba
ya era completa. Un empate antes dependiente del orden incidental puede cambiar de
elección; esa corrección es explícita. No se comparan IDs entre bindings diferentes.
Las decisiones ya finalizadas no se revisan al llegar otro manifiesto compatible.
El caso anteriormente pendiente ahora genera su primer FundingEvidence; no duplica
pagos anteriores ni reescribe el prefijo confirmado.

La restauración sigue exigiendo reconstrucción exacta desde las fuentes originales.
No se promete aceptar un snapshot anterior que contenga la decisión defectuosa:
no se fuerza su aceptación mediante checksum ni se modifica su journal a mano.
La comparación de estados anteriores cubre los tres escenarios correctos descritos,
no una migración general de snapshots entre versiones o procesos.

Se recorren todos los manifiestos pertinentes hasta las cuotas actuales. No se
añade una certificación universal de memoria o latencia. Los benchmarks anteriores
de componente de 1/7/30 días no demuestran rendimiento del runner financiero
completo; esta corrección tampoco certifica Replay multianual.
La suite completa incluye un caso de fast query de 30 días: valida consultas
históricas, no sustituye un benchmark del runner financiero completo de ese horizonte.

El alcance continúa exclusivamente fixture-only. No hay autenticación de datos
reales, aprobación independiente sobre el trabajo propio ni permiso de merge.
Sin Binance privado, LIVE/SHADOW, trading, transferencias, Add Margin real,
Telegram operativo, VPS, servicios ni desarrollo de otra fase. Runtime STOPPED.
