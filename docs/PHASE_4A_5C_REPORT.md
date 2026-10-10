# Fase 4A-5C — lectura segmentada offline

Base aprobada: `efcf53e7fb0350c22b74c7ffe686dd24a006e3d2`.
Runtime STOPPED. Datasets reales: DATASET_NOT_PROVIDED.

## Diagnóstico previo a implementar

`HistoricalQuery.read` ejecuta `groups` dos veces (preflight y entrega).
Cada `groups` abre SQLite con integrity_check/foreign_key_check y reconstruye
cada dataset seleccionado mediante `verify_dataset`: hashes completos, auditoría
semántica con índice temporal efímero, `indexed_rows` y todos los segmentos.
Incluso fuentes de otra categoría se verifican. Trade usa número de fila como
locator lógico y recorre CSV/ZIP desde el inicio. Al finalizar vuelve a hashear
cada original. Un prefijo de tres minutos paga la verificación del mes entero
repetidamente. Recovery repite esa reconstrucción sin reutilizar raíces de sesión.
El informe de conciliación hace otro recorrido verificado. La apertura SQLite y
su integrity_check no equivalen a la protección del original externo.

La medición nueva, previa a editar los módulos aprobados, se conserva en /tmp;
los tiempos inclusivos del perfil se solapan y no deben sumarse. La referencia
previa de 30 días fue 83.04s registro, 125.40s consulta, 82.66s prefijo y 18.25s
recovery bajo carga concurrente; no constituye una comparación cold/warm actual.

## Entrega y validación

La entrega incorpora sesiones opt-in, consultas segmentadas y checkpoints de
recuperación. Las validaciones y mediciones se detallan a continuación.

## Base medida de nuevo

Un proceso nuevo, sin profiler/tracemalloc en estas cifras, sobre el main aprobado
sin editar: 43200 filas, registry22872064 bytes, RSS23128KiB.

| Operación | Segundos |
|---|---:|
| Registro |34.1806|
| Conciliación completa |30.5571|
| Consulta completa |70.8846|
| Verificación/recovery del registry |27.9124|
| Prefijo de3 minutos |41.4468|

Es una referencia actual, distinta de las cifras previas bajo otra carga. No se
compara directamente con latencias instrumentadas con tracemalloc como si tuvieran
la misma metodología. El benchmark compara STRICT/rápido en el mismo fixture,
proceso y configuración de instrumentación; sus factores son diagnósticos.

Perfil cProfile separado de prefijo3m:150.8844s. Incluye overhead del profiler;
los tiempos acumulados se solapan:

| Componente | Llamadas | Acumulado s |
|---|---:|---:|
| SHA completo original |10|0.0501|
| Auditoría semántica completa |2|90.7266|
| Reconstrucción indexed_rows |86402 resumptions|55.3621|
| Verificación segment_values |60|3.0658|
| verify_dataset |2|150.6950|
| groups preflight + entrega |8 resumptions|150.8836|
| Locator/parsing de3 registros |8 resumptions|0.0054|
| SQLite execute (todas las categorías) |173008|2.2607|
| connection (incluye checks) |4 resumptions|0.1736|

Prueba de componentes aparte, bajo carga posterior, source de referencia intacta:
SQLite open0.00045s; integrity_check1.10133s; foreign_key_check0.08555s;
primera lectura de locators3 registros0.01731s; segunda pasada0.00112s;
construcción/conciliación sobre3 eventos ya verificados0.000089s. Este último
experimento separa bookkeeping del informe y no se presenta como validación de
originales. No se suman cifras de experimentos distintos para fabricar un total.

## Implementación y archivos

Sólo diez archivos nuevos, sin modificar los211 archivos congelados de la base:

- verification.py: modos, estados, política explícita y checkpoints frozen/JSON seguro.
- segment_session.py: gate integral, snapshot de registro, locators físicos,
  cache auxiliar, raíces de segmentos en memoria, selección temporal protegida.
- fast_query.py: preflight en disco, merge causal, estados de finalización y recovery.
- fast_query_benchmark.py: fixtures sintéticos1/7/30, cold/warm, memoria y restart real.
- test_historical_fast_query.py: contratos, causalidad, fraude e interrupciones.
- test_historical_fast_query_scale.py: escala/recovery/benchmarks reproducibles.
- historical_query_process_probe.py: lanzamiento fijo del proceso de prueba fuera del paquete histórico.
- HISTORICAL_FAST_QUERY_SPEC.md, HISTORICAL_VERIFICATION_CHECKPOINTS_SPEC.md y este informe.

No se editó registry/query/reconciliation/normalizador/4A-5A/4A-5B; ni estrategia,
simulador, ledger, performance, riesgo, reservas o runner. Schema del registry1
inalterado. Cache/spool secundarios nuevos, temporales y eliminables. No se
incluyen fuentes, CSV/ZIP/SQLite, sidecars, checkpoints, logs o credenciales en Git.

## Garantías y resultado funcional

STRICT_FULL conserva exactamente HistoricalQuery aprobado y es default.
SEALED_SEGMENT_SESSION requiere política y apertura explícitas, con validación
integral nueva e índices. El contenido de cada segmento solicitado se vuelve a
verificar, mientras se reutilizan raíces de sesión independientes del disco.
FULL_SOURCE_VERIFIED inicial no se convierte en garantía global vigente de cada
lectura: las consultas rápidas publican SEGMENT_VERIFIED / SOURCE_NOT_REVALIDATED.
Un adversario que controla memoria/raíces queda fuera del modelo explícito.

CSV/JSONL usan offset/longitud/hash real, no número de fila como offset. ZIP/JSON
utilizan snapshot derivado después de CRC/auditoría íntegra, siempre ligado al
original intacto. Índices temporales SQLite y raíces en memoria impiden ocultar
segmentos/conflictos adulterando la cache. Una verificación parcial no certifica
segmentos ajenos ni el ZIP actual completo. No hay firmas o autenticación Binance.

Preflight único crea snapshot del intervalo en disco antes de entregar datos;
resuelve duplicados económicos conservando fuentes y bloquea conflictos para read.
Los resultados son PROVISIONAL hasta COMPLETE; fallo/interrupción impide certificar
el stream. Las consultas/generadores abandonados deben cerrarse. COMPLETE describe
finalización/integridad bajo el modo seleccionado, no cobertura completa ni
habilitación financiera. Histórico parcial puede finalizar y seguir PARTIAL/MISSING.

UTC y Decimal conservados; tres tiempos independientes; Mark al cierre, funding
publicado, vigencias y calidad de reglas; sin ticks High/Low o fees/funding inferidos.
La conciliación conserva gaps/cobertura/duplicados/conflictos/digest del modo integral.
No hay conexión a motores financieros, SOURCE_AUTHENTICATED u OBSERVED.

Checkpoints frozen vinculados a originales, selección y schema/parser. Sin raíz
independiente de confianza no se restaura un checkpoint externo. Rehash local
fraudulento no basta. Proceso nuevo vuelve a verificar íntegramente fuentes e índice;
no hereda confianza. Reanudación reconstruye cursor/prefijo/preflight y no duplica,
omite o anticipa eventos. resume_groups conserva auditorías conflictivas;
resume mantiene bloqueo de lectura utilizable. No hay recovery LIVE.

## Pruebas A–P y regresión

| Requisito | Protección probada |
|---|---|
| A/B | Cambio antes de apertura falla gate integral; cambio observable tras preparación invalida sesión. |
| C | Bytes/locators/payload consultados adulterados bloquean contra raíz protegida. |
| D | Cambio ajeno observable invalida; ocultándolo deliberadamente, lectura parcial puede pasar con SOURCE_NOT_REVALIDATED. STRICT_FULL falla. |
| E/F | SQLite del registry modificado antes de abrir falla; snapshot ya preparado permanece estable. Índice temporal/links/digest falso no ocultan alteraciones seleccionadas. |
| G/H | Checkpoint y hash local reescritos fallan emisión/raíz externa y reconstrucción independiente. |
| I/J/K | Identidad/dataset/selección/parser/schema/fuentes/preflight incompatibles bloquean recovery. |
| L/M/N | Truncación/sustitución/ausencia invalida; original nunca se sobrescribe. |
| O | ZIP corrupto antes de abrir falla; lector ZIP/CRC/límites aprobados se reutiliza. Snapshot comprimido no prueba cambios globales posteriores ocultos. |
| P | Mutación durante stream deja FAILED; snapshot adulterado no finaliza con éxito. |

También: límites físicos antes de materializar bytes, política/TTL/query-count,
checkpoints frozen, JSON sin NaN/duplicados y acotado, precisión hostil, ausencia
comprobada de acceso de red, calidad CURRENT_ONLY bloqueada, seis categorías y
fuentes JSON/JSONL/CSV/ZIP, gaps/duplicados/versiones contradictorias, overlaps anidados,
cancelación de preparación, close/cleanup y preflight sin entrega utilizable parcial.

Fixtures de1440/10080/43200 filas comparan exactamente registros/valores/timestamps/
identidades/orden y digest STRICT contra modo rápido. Interrupciones antes del primer
item, dentro del segmento, en frontera, antes de finalización, durante groups de
conciliación y antes/después de checkpoint. Auditorías con conflictos también se
reanudan sin borrarlos. Proceso nuevo real prueba raíz independiente y gate íntegro.
Las pruebas anteriores permanecen intactas; hashes de código/tests/docs aprobados
comparados con la captura previa a editar. No hay cambios financieros con OFF.

## Resultados de validación

| Validación | Resultado |
|---|---|
| Suite específica host | **96 passed**, 1076.33s |
| Suite completa host | **2388 passed**, 1439.90s |
| Suite específica Docker | **96 passed**, 1419.20s |
| Suite completa Docker | **2388 passed**, 3151.74s |
| Ruff format --check . | 184 archivos, aprobado |
| Ruff check . | All checks passed |
| mypy | Sin errores, 184 archivos |
| git diff --check / --cached --check | Aprobados |

96 tests nuevos; 2292 anteriores preservados. Las ejecuciones preliminares/interrumpidas
no se cuentan como validaciones finales. Docker usa imagen local validada y COPY de
fuentes/tests finales; build --network none --pull=false y run --network none
--read-only --tmpfs /tmp; pytest sin cacheprovider. Sin cambios de proxy/CA/credenciales.

## Benchmarks completos

Latencias y memoria con tracemalloc activo; coste inicial separado.

| Días | Filas | Preparación D s | Prefijo STRICT s | Prefijo warm s | Factor | Consulta completa STRICT s | Completa warm s | Factor |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
|1|1440|5.3103|7.4454|1.9915|3.74x|10.6176|6.5151|1.63x|
|7|10080|34.2476|60.0481|1.3352|44.97x|157.4906|45.2083|3.48x|
|30|43200|528.3854|711.1740|3.2763|217.06x|775.9870|311.6500|2.49x|

Las dos rutas coincidieron en conteos/digest económicos. En I el conteo incluye
el prefijo reconocido; J cuenta sólo los dos restantes y conserva digest acumulado
de los tres. K mide reverificación tras descartar la sesión; J muestra la nueva
preparación. No se hereda confianza. Los bytes de fuente son estimaciones analíticas
de pasadas lógicas: A=9, B/K=6, D=9, C=3; STRICT read=14 completos + dos lecturas
de ventana. Se corrigieron esos coeficientes al consolidar los datos, sin alterar
tiempos, RSS, tracemalloc, conteos o resultados medidos. No son I/O físico del disco.

### 1 día — métricas por operación

| Caso | Modo/temperatura | Verificación | s | Filas | RSS KiB | Python bytes | Fuente estimada bytes | SQLite bytes | Cache bytes | Spool bytes |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
|A_REGISTER|STRICT_FULL / COLD_START|FULL_SOURCE_VERIFIED|6.9616|1440|23544|758841|803520|835584|0|0|
|B_FIRST_FULL_VALIDATION|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|3.7352|1440|23544|159167|535680|835584|0|0|
|D_OPEN_VALIDATED_SESSION|SEALED_SEGMENT_SESSION / COLD_START|FULL_SOURCE_VERIFIED|5.3103|1440|24580|164774|803520|835584|1249280|0|
|E_PREFIX_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|7.4454|3|24580|165487|1250292|835584|1249280|0|
|E_PREFIX_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|1.9915|3|24580|54529|89280|835584|1249280|1314816|
|F_DAY|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|10.6176|1440|24580|164322|1428480|835584|1249280|0|
|F_DAY|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|6.5151|1440|24580|51999|89280|835584|1249280|2297856|
|I_INTERRUPTED_RECOVERY|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|1.6138|3|25356|52324|89280|835584|1249280|1314816|
|J_NEW_SESSION_RECOVERY|SEALED_SEGMENT_SESSION / COLD_START|FULL_VALIDATION_THEN_SEGMENT_VERIFIED|7.4031|2|25612|163899|892800|835584|0|No instrumentado|
|K_INVALIDATION_REVERIFY|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|4.0249|1440|25612|157467|535680|835584|0|0|
|L_DUPLICATE_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|0.0200|3|25612|89985|1984|77824|40960|No instrumentado|
|L_DUPLICATE_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|0.0115|3|25612|33306|248|77824|40960|No instrumentado|
|M_CONFLICT_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|0.0339|2|25612|89123|1984|77824|40960|No instrumentado|
|M_CONFLICT_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|0.0259|2|25612|33579|248|77824|40960|No instrumentado|
|C_BUILD_PHYSICAL_INDEX|SEALED_SEGMENT_SESSION / PREPARATION_COMPONENT|SESSION_VERIFIED|1.6036|1440|24580|164774|267840|835584|1249280|0|
|J_PROCESS_RESTART|SEALED_SEGMENT_SESSION / COLD_START|FULL_VALIDATION_THEN_SEGMENT_VERIFIED|7.6281|2|25612|766170|892800|835584|1249280|No instrumentado|

### 7 días — métricas por operación

| Caso | Modo/temperatura | Verificación | s | Filas | RSS KiB | Python bytes | Fuente estimada bytes | SQLite bytes | Cache bytes | Spool bytes |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
|A_REGISTER|STRICT_FULL / COLD_START|FULL_SOURCE_VERIFIED|38.7019|10080|25612|160925|5624640|5394432|0|0|
|B_FIRST_FULL_VALIDATION|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|20.0362|10080|25612|161208|3749760|5394432|0|0|
|D_OPEN_VALIDATED_SESSION|SEALED_SEGMENT_SESSION / COLD_START|FULL_SOURCE_VERIFIED|34.2476|10080|25612|166965|5624640|5394432|8495104|0|
|E_PREFIX_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|60.0481|3|25612|168414|8749812|5394432|8495104|0|
|E_PREFIX_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|1.3352|3|25612|53419|89280|5394432|8495104|1314816|
|F_DAY|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|94.9511|1440|25612|168959|8928000|5394432|8495104|0|
|F_DAY|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|11.0788|1440|25612|51771|89280|5394432|8495104|2297856|
|G_7D|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|157.4906|10080|25612|168289|9999360|5394432|8495104|0|
|G_7D|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|45.2083|10080|25612|996201|624960|5394432|8495104|16023552|
|I_INTERRUPTED_RECOVERY|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|1.4213|3|25972|52278|89280|5394432|8495104|1314816|
|J_NEW_SESSION_RECOVERY|SEALED_SEGMENT_SESSION / COLD_START|FULL_VALIDATION_THEN_SEGMENT_VERIFIED|45.4703|2|26612|167674|5713920|5394432|0|No instrumentado|
|K_INVALIDATION_REVERIFY|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|37.7179|10080|26612|161391|3749760|5394432|0|0|
|L_DUPLICATE_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|0.0204|3|26612|89569|1984|77824|40960|No instrumentado|
|L_DUPLICATE_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|0.0218|3|26612|33250|248|77824|40960|No instrumentado|
|M_CONFLICT_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|0.0231|2|26612|88967|1984|77824|40960|No instrumentado|
|M_CONFLICT_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|0.0149|2|26612|33289|248|77824|40960|No instrumentado|
|C_BUILD_PHYSICAL_INDEX|SEALED_SEGMENT_SESSION / PREPARATION_COMPONENT|SESSION_VERIFIED|12.6247|10080|25612|166965|1874880|5394432|8495104|0|
|J_PROCESS_RESTART|SEALED_SEGMENT_SESSION / COLD_START|FULL_VALIDATION_THEN_SEGMENT_VERIFIED|67.8422|2|26612|770197|5713920|5394432|8495104|No instrumentado|

### 30 días — métricas por operación

| Caso | Modo/temperatura | Verificación | s | Filas | RSS KiB | Python bytes | Fuente estimada bytes | SQLite bytes | Cache bytes | Spool bytes |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
|A_REGISTER|STRICT_FULL / COLD_START|FULL_SOURCE_VERIFIED|204.7865|43200|26612|163709|24105600|22872064|0|0|
|B_FIRST_FULL_VALIDATION|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|296.8338|43200|26612|173669|16070400|22872064|0|0|
|D_OPEN_VALIDATED_SESSION|SEALED_SEGMENT_SESSION / COLD_START|FULL_SOURCE_VERIFIED|528.3854|43200|26612|190536|24105600|22872064|36380672|0|
|E_PREFIX_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|711.1740|3|26612|180835|37497972|22872064|36380672|0|
|E_PREFIX_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|3.2763|3|26612|53470|89280|22872064|36380672|1314816|
|F_DAY|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|374.6125|1440|26612|180977|37676160|22872064|36380672|0|
|F_DAY|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|3.7614|1440|26612|51698|89280|22872064|36380672|2297856|
|G_7D|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|254.8893|10080|26612|180400|38747520|22872064|36380672|0|
|G_7D|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|69.2041|10080|26612|52802|624960|22872064|36380672|16023552|
|H_30D|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|775.9870|43200|26612|180176|42854400|22872064|36380672|0|
|H_30D|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|311.6500|43200|26612|997971|2678400|22872064|36380672|68726784|
|I_INTERRUPTED_RECOVERY|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|4.3541|3|26612|52205|89280|22872064|36380672|1314816|
|J_NEW_SESSION_RECOVERY|SEALED_SEGMENT_SESSION / COLD_START|FULL_VALIDATION_THEN_SEGMENT_VERIFIED|215.2251|2|26624|190384|24194880|22872064|0|No instrumentado|
|K_INVALIDATION_REVERIFY|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|104.0575|43200|26624|172660|16070400|22872064|0|0|
|L_DUPLICATE_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|0.0171|3|26624|89758|1984|77824|40960|No instrumentado|
|L_DUPLICATE_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|0.0132|3|26624|33137|248|77824|40960|No instrumentado|
|M_CONFLICT_3M|STRICT_FULL / FULL_VALIDATION|FULL_SOURCE_VERIFIED|0.0162|2|26624|89632|1984|77824|40960|No instrumentado|
|M_CONFLICT_3M|SEALED_SEGMENT_SESSION / WARM_SESSION|SEGMENT_VERIFIED|0.0107|2|26624|33420|248|77824|40960|No instrumentado|
|C_BUILD_PHYSICAL_INDEX|SEALED_SEGMENT_SESSION / PREPARATION_COMPONENT|SESSION_VERIFIED|162.4577|43200|26612|190536|8035200|22872064|36380672|0|
|J_PROCESS_RESTART|SEALED_SEGMENT_SESSION / COLD_START|FULL_VALIDATION_THEN_SEGMENT_VERIFIED|396.3667|2|26624|783214|24194880|22872064|36380672|No instrumentado|

Cache bytes corresponde al artefacto vivo en el contexto medido; las mediciones
STRICT pueden coexistir con cache ya preparada del modo warm. J_NEW_SESSION cerró
su cache al devolver: 0 representa el tamaño retenido, no el pico de disco durante
preparación. J_PROCESS_RESTART captura el tamaño de cache antes de cerrar. Spool
indica tamaño capturado antes de consumir read; no se instrumentó disco del spool
en los subconjuntos J/L/M. Esta limitación permanece explícita; no significa que
esos streams no utilicen spool. Los picos RSS son acumulativos del proceso.

Reproducir en directorios nuevos fuera de Git (sin datos externos):

```python
from pathlib import Path
from dataclasses import asdict
from trading_engine.replay.historical_validation.fast_query_benchmark import benchmark_fast_query
from tests.unit.replay.historical_query_process_probe import process_restart_measurement
for days in (1, 7, 30):
    for result in benchmark_fast_query(days, Path(f"/tmp/review-4a5c-{days}"), process_restart=process_restart_measurement):
        print(asdict(result))
```


Cada operación se mide con tracemalloc para Python y ru_maxrss del proceso para
RSS. Tracemalloc añade overhead material; las cifras no son tiempos de producción.
RSS es high-water acumulativo del proceso, no máximo aislado de cada operación.
La memoria de construcción C comparte el máximo de D como cota superior declarada.
Cold significa nueva raíz/proceso, no desalojar page cache del sistema operativo.
Warm reutiliza esa raíz, pero revalida contenido de segmentos. Fuente física estimada
es un modelo de pasadas/bytes lógicos, no un contador de disco o cache misses.
Los componentes estimados de registro y ventana se documentan junto a las tablas;
no se omite el coste de preparación. Hay suites concurrentes: no se prometen
latencias absolutas o throughput multianual a partir de una muestra sintética.

El benchmark largo comenzó antes de trasladar el lanzamiento de procesos al
harness de tests y de finalizar las guardas de longitud SQLite/lectura física.
Esas correcciones no cambian el algoritmo de consulta ni sus resultados; sus
costes individuales no se aislaron. Las cuatro suites finales utilizan los
archivos definitivos. La reproducción indicada usa la API final y su callback
explícito de reinicio; la biblioteca histórica no inicia procesos por defecto.

L/M usan fixtures explícitos de3 minutos con duplicado/conflicto, separados de
las ventanas completas. C es subcomponente de D y no se suma otra vez. J distingue
apertura nueva de sesión y proceso nuevo real: se revalida todo el original.
I reconstruye el intervalo solicitado y cursor, no una restauración O(1).

## Riesgos y requisitos posteriores

1. **DATASET_NOT_PROVIDED**: no existe histórico real aportado/verificado aquí.
2. Autenticidad/proveedor/publicación/fees/brackets reales requieren evidencia
   independiente; consultar más rápido no autoriza económicamente las fuentes.
3. La raíz de proceso exige memoria/código protegidos y originales estables por
   política. Metadatos detectan algunos cambios; no sustituyen hashes globales.
4. Revalidación global actual exige STRICT_FULL. La cache/snapshot parcial no
   detecta modificaciones ocultas fuera de los segmentos comprobados.
5. Persistir y proteger raíces externas de checkpoints requiere un mecanismo
   auditado separado; no se proporciona autenticación autosuficiente desde SQLite.
6. Preparar sesión sigue costando O(original + index) y disco crece con filas.
   Spools crecen con segmentos consultados; no son una tabla Trade de producción.
7. Recovery reconstruye el intervalo y prefijo reconocido. Proceso nuevo paga gate
   íntegro e índices. No se demuestra recuperación financiera multianual eficiente.
8. Un consumidor financiero posterior necesitará commit/rollback de PROVISIONAL,
   autenticación, adaptación de fuentes, correcciones versionadas y auditoría causal.
9. Sin trayectoria intraminuto real, prioridad de liquidación/reservas/ejecución
   oficial: no se certifican rentabilidad, supervivencia o liquidación Binance.

## Git y estado operativo

Un único commit de implementación y push normal exclusivamente
phase-4a-5c-sealed-segment-query; SHA publicado se entrega fuera del propio commit.
main debe permanecer en efcf53e7fb0350c22b74c7ffe686dd24a006e3d2, feature1 ahead/0 behind,
0 merge commits y working tree limpio. No hay merge/rebase/squash/force push.
No hay descarga externa, API Binance, backtest real, LIVE/Shadow, transferencia,
VPS, Telegram, systemd o despliegue. **Runtime STOPPED. Detenerse para auditoría.**

Durante validación, el test de seguridad previo detectó el import de subprocess en
el benchmark. Se retiró del paquete histórico y el lanzamiento fijo quedó únicamente
en el harness de pruebas. El test anterior no se modificó ni relajó. El callback
process_restart es explícito y default None; la biblioteca no lanza procesos.
