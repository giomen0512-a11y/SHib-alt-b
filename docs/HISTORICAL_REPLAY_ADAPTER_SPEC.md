# Adaptador causal histórico para Replay — Fase 4A-5D-1

Base aprobada: `ab6a8ddeb2adfc3ad8bc4294d14a41d235b3b9c0`.
Operación exclusivamente offline; runtime **STOPPED**. Opt-in por construcción explícita.

## Arquitectura y alcance

`HistoricalQuery` o `SegmentedHistoricalQuery` → conciliación → preparación completa en SQLite
temporal → `PreparedTradeHistory` → puerto `CandleHistory` → `ReplayRunner` → mismo `AltBEngine`.

El puerto contiene únicamente `session`, `conflicts(symbol)` y
`read(symbol, start, end) -> Generator[Candle, None, None]`. La única ampliación de ReplayRunner
es su anotación de historia; ninguna instrucción de su lógica cambia. SQLiteHistory sigue siendo
compatible. No se altera estrategia, configuración, matching, ledger ni contratos financieros.

El contrato aceptado es Binance USDⓈ-M Futures, **1000SHIBUSDC PERPETUAL USDC**, intervalo 1m.
Sólo se aceptan DatasetIds explícitos de `TRADE_KLINES_1M`; ni Spot ni otros símbolos pueden
convertirse. No se escalan precio, cantidad, volumen o taker-buy por 1000. Decimal se reconstruye
exactamente con contexto local de precisión 50; timestamps UTC aware.

## Preparación atómica

Constructor: `PreparedTradeHistory(registry_path, dataset_ids, replay_request, *, mode,
session, cutoff, limits)`. `mode=STRICT_FULL`, `session=None`, `cutoff=request.end` y
`AdapterLimits()` son los valores predeterminados. El registro debe existir: no se crea,
migra ni escribe su versión 1. No hay selección automática de fuentes.

1. Se calcula el warmup canónico de 1560 minutos, recortando la consulta por lanzamiento del
   contrato. Un intervalo completamente anterior al lanzamiento no publica historia.
2. Se comprueban identidad contractual, categoría, importación completa, auditoría, digest de
   fuente y registro. El modo de consulta aprobado conserva sus validaciones integrales.
3. HistoricalReconciliation consume el prefijo causal y conserva cobertura, gaps, duplicados y
   conflictos. Todo conflicto bloquea; equivalencias se concilian sin prioridad diario/mensual.
4. Se consume íntegramente la lectura Trade en una transacción SQLite privada. Se verifica
   orden estricto, límites, cierre de minuto, cutoff, procedencia y autorización de cada evento.
5. En modo segmentado se exige un QueryStream real, estado **COMPLETE** y su digest causal.
   PROVISIONAL, FAILED e INTERRUPTED nunca publican una historia utilizable.
6. En ambos modos se comparan digest, número de registros y duplicados con la conciliación
   previa. Se comprueba que las vinculaciones a fuentes permanecen iguales.
7. Sólo tras commit, integridad SQLite y apertura de sólo lectura se publica el objeto preparado.
   Cualquier excepción cierra stream/conexiones y elimina el directorio temporal.

STRICT_FULL utiliza el generador aprobado, cuyo agotamiento exitoso incluye verificación final
de las fuentes. El digest independiente del adaptador debe igualar el de conciliación.

## Almacenamiento, raíces y ciclo de vida

La preparación no conserva listas de todas las velas. Usa SQLite temporal con cuotas explícitas:
como máximo 1.000.000 filas y 128 MiB para la historia preparada, reducibles mediante
AdapterLimits. Límites enteros reales; bool no los sustituye. Cuota de páginas, comprobaciones
de archivos temporales y límites de payload bloquean excesos. Los costes temporales de las
consultas aprobadas y de la sesión tienen además sus propios límites; la cuota del adaptador
no representa la suma global de todos esos módulos.

Cada bloque de hasta 1440 filas tiene una raíz SHA-256 independiente retenida en memoria:
ordinales, límites temporales y contenido canónico completo. El payload incluye Candle, clave
económica, DatasetIds, fuentes, procedencia y duplicados. No se modifica el registro original.

Las lecturas buscan por ordinales protegidos, verifican todo el bloque contra la raíz y sólo
después publican sus Candle inmutables. Conservan como máximo un bloque decodificado. Una
alteración del índice de fechas no puede ocultar una fila y presentarla como gap. Un checksum
recalculado dentro de SQLite no reemplaza la raíz de proceso. El cache validado en memoria
representa el contenido aceptado, incluso si posteriormente se altera el archivo temporal.
Un fallo de contenido al cargar un bloque invalida y limpia la historia.

`with PreparedTradeHistory(...) as history:` garantiza limpieza; `close()` es idempotente.
Después del cierre no se pueden leer velas ni recuperar Replay. No hay API para reutilizar
un cache temporal persistido como fuente confiable. Las raíces requieren confiar en el proceso
y su memoria; no son una firma del proveedor ni protección frente a quien controle ese proceso.

## Verificación y evidencia

STRICT_FULL conserva `FULL_SOURCE_VERIFIED` al instante de preparación. Replay lee el snapshot
aceptado, no vuelve a verificar todos los originales por cada minuto. Una preparación nueva
repite la verificación integral.

SEALED_SEGMENT_SESSION exige opt-in, SealedSegmentSession previamente validada y SessionPolicy
explícita. Conserva el checkpoint aprobado con `SEGMENT_VERIFIED` y `SOURCE_NOT_REVALIDATED`:
la comprobación de segmentos seleccionados no certifica autenticidad global de originales.
La sesión debe seguir válida durante el consumo; TTL, cierre o cambios observables bloquean.
El adaptador no cambia los supuestos de estabilidad ni los límites de la sesión.

PreparedTradeEvidence es inmutable. Incluye solicitud, intervalo, cutoff, bindings de registro,
conciliación, checkpoint cuando corresponde, digest de consulta, digest de Candle, procedencias,
cobertura y warmup total/consecutivo. Sus estados son `NOT_AUTHENTICATED`,
`NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES`, adaptación financiera `NOT_AUTHORIZED`, ejecución histórica
`NOT_AVAILABLE`; uso Trade `OFFLINE_REPLAY_CANDIDATE`. Los fixtures se etiquetan SYNTHETIC_FIXTURE.
No se habilitan OBSERVED ni SOURCE_AUTHENTICATED.

Para auditar resultados, conservar `history.evidence` junto con ReplayReport. El reporte legado
mantiene su formato y fingerprints; el alcance de verificación y procedencia histórica se
documenta en esa evidencia adjunta, no se infiere del reporte estratégico aislado.

## Causalidad y cobertura

Se mantienen separados `market_effective_at` (apertura), `market_available_at` (cierre) y
`dataset_acquired_at` (declaración de adquisición en cada binding). La adquisición no retrasa
ni adelanta disponibilidad económica; se conserva como procedencia, no como cotización.

`read` sólo entrega Candle cuyo minuto ha cerrado al límite solicitado. Consulta y preparación
no incluyen eventos más allá del cutoff. Un cutoff parcial permite preparar únicamente el
prefijo completo, informa INCOMPLETE y **bloquea** leer más allá del cutoff; no transforma el
futuro desconocido en un gap. Lecturas fuera de la solicitud/warmup o con otro símbolo se rechazan.

Gaps conocidos permanecen vacíos. Warmup inferior a 1560 minutos consecutivos que terminen en
request.start se informa INCOMPLETE, sin fabricar minutos o señales. AltBEngine conserva sus
reglas de reinicio y puede adquirir suficiente historia posterior según su contrato existente.
No se bloquea arbitrariamente para siempre una fuente con gaps. Se mantienen checkpoints de
15 minutos, vetos, T+120, una ronda activa y todos los parámetros aprobados.

## Ejecución y finanzas

ReplayRunner sigue utilizando **NoQuotes** si no se proporciona una fuente de cotizaciones
externa explícita. Trade OHLC nunca se convierte en Quote, Fill observado o Mark observado.
No se infieren bid/ask, spread, slippage, maker/taker, fees, funding, bracket o capital.

Los opt-in aprobados siguen bajo sus propios contratos y etiquetas MODELED/SYNTHETIC_FIXTURE;
el adaptador no los activa. `ModeledQuotes` actualmente requiere SQLiteHistory concreto: no se
amplía ese contrato ni se crea una subclase artificial. Vincularlo a esta historia requiere una
revisión posterior. Las cotizaciones y hechos de fixtures independientes siguen siendo puertos
compatibles, como demuestran las pruebas de equivalencia.

Validar Trade no autoriza las categorías Mark, Funding, Fees, Brackets, Capital o ejecución real.
Las métricas dependientes permanecen NOT_AVAILABLE sin sus evidencias explícitas. El adaptador
**no autentica a Binance, no obtiene fills reales, no certifica liquidaciones o supervivencia,
no calcula rendimiento histórico real y no habilita LIVE**.

## Recuperación

ReplaySnapshot conserva el vínculo opaco de sesión de la historia. En la misma historia
preparada y sesión válida, ejecución continua y fragmentada conservan eventos, orden,
timestamps, estado y digest. Un objeto nuevo no hereda confianza del snapshot anterior.
Otro proceso debe reconstruir fuentes/sesión y sus raíces independientemente; no existe en
esta fase recovery persistente de Replay entre procesos ni de LIVE.

## Pruebas y medición

El módulo de tests añade fixtures exclusivamente sintéticos: equivalencia exacta con
HistoricalQuery y SQLiteHistory, fingerprints de main congelados antes de editar, ambos modos,
reanudación, gaps, warmup, cierre, cutoff, conflictos, duplicados, fuentes modificadas antes y
durante consumo, cache/registro/segmentos adulterados, streams incompletos, cuotas, limpieza,
inmutabilidad, contexto Decimal hostil y sockets prohibidos.

`python -m tests.unit.replay.historical_replay_adapter_benchmark --minutes 20|1440|10080
--mode STRICT_FULL|SEALED_SEGMENT_SESSION` genera un fixture local en disco y mide cada modo en
un proceso separado. Reporta registro, validación inicial de sesión, preparación inclusiva,
conciliación, consumo Replay, restauración, consumo restante, RSS máximo de proceso y tamaños
SQLite preparados/de sesión. RSS incluye generación y registro; no se presenta como memoria
exclusiva del adaptador. Los tamaños son de archivos retenidos, no un pico agregado del disco.
La validación de sesión inicial y preparación se muestran separadas; no se anuncia warm como cold.

## Condiciones pendientes

La integración financiera requiere revisión de fuentes/evidencias causales para Mark, pagos de
funding, fees, brackets históricos, partición de capital y ejecución, con autenticación
independiente cuando corresponda. Un archivo correcto, checksum local o segmento verificado
no proporciona esas evidencias. No se utilizan históricos reales 2024–2026 en esta fase.
