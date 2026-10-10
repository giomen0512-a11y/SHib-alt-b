# Lectura histórica eficiente — Fase 4A-5C

Base `efcf53e7fb0350c22b74c7ffe686dd24a006e3d2`. Offline opt-in,
Runtime **STOPPED**, datos reales **DATASET_NOT_PROVIDED**.
No hay integración financiera, normalizador nuevo ni cambios en estrategia.

## Arquitectura y selección explícita

```text
Original + Registry v1 + DatasetIds explícitos
  ├─ STRICT_FULL → HistoricalQuery aprobado, sin cambios
  └─ SEALED_SEGMENT_SESSION (opt-in)
       → verify_dataset completo + snapshot estable del registro
       → índice secundario temporal + raíces en memoria de proceso
       → selección temporal SQLite contrastada con esas raíces
       → verificación de segmentos solicitados + preflight en disco
       → QueryStream PROVISIONAL → COMPLETE / FAILED / INTERRUPTED
```

`historical_query(path, ids)` devuelve exactamente HistoricalQuery de 4A-5B.
La optimización usa SealedSegmentSession y SegmentedHistoricalQuery explícitos.
No cambia defaults, APIs financieras, ReplayRunner o schema1 del registro.
Registration, HistoricalEvent, HistoricalConflict, parsers y normalizador Futures
son los aprobados. HistoricalReconciliation puede recibir el query opt-in por
compatibilidad de su interfaz; no se modificó su implementación.

## Causa del coste

El query anterior verifica originales, auditoría y todos los segmentos por cada
`groups`, incluso otras categorías seleccionadas. `read` hace dos `groups`:
preflight y entrega. Trade resuelve locators lógicos recorriendo el original.
La auditoría semántica y `indexed_rows` reconstruyen registros repetidamente;
los hashes completos son necesarios pero no dominan el perfil medido. SQLite
integrity_check, reconstrucción de segmentos y otra verificación final también
se pagan. Detalle de perfil y mediciones en PHASE_4A_5C_REPORT.md.

La sesión rápida conserva ese coste inicial y lo amortiza en consultas repetidas.
No se presenta como eliminación de verificación integral o aceleración equivalente
en un proceso nuevo. La consulta de tres minutos verifica el segmento diario
correspondiente (1440 filas en el fixture), no solamente tres filas.

## Preparación y modelo de confianza

Al abrir, conexión aprobada mode=ro/query_only/foreign_keys/BEGIN, integrity_check,
foreign_key_check y schema1. `verify_dataset` reconstruye identidad, SHA completo,
auditoría semántica, registros y sellos desde originales. La selección externa
explícita de DatasetIds queda congelada. Después se recorre cada fuente para
construir locators, contrastando secuencia, hash crudo, identidad económica y
tiempos con el snapshot aprobado; SHA completo y estado observable se comprueban
al acabar. Un error elimina la cache incompleta; no escribe en originales o registry.

Raíces SegmentRoot frozen se retienen **en memoria**, separadas del SQLite secundario.
Vinculan DatasetId, SegmentId, bounds, conteo, digest de descriptors y sello aprobado.
root_id vincula selección y fuentes completas. El proceso y sus raíces deben estar
protegidos frente a escritura adversaria; es un supuesto explícito de la sesión.
Reescribir datos y hashes locales en disco no modifica estas raíces en memoria.
No son una firma, autenticación independiente de Binance o prueba de origen oficial.
La selección externa tampoco autentica al proveedor de esos archivos.

SessionPolicy exige max_seconds, max_queries y source_stability_assumption explícitos.
TTL usa monotonic del proceso, nunca cutoff de mercado. opened_at es UTC explícito
para auditoría; no se usa para anticipar eventos ni como fuente de precios. max_queries
acota lecturas y checkpoints emitidos; hasta 1000 raíces por sesión. Close/expiración,
error de segmentos o cambio observable invalidan la sesión; no hay fallback silencioso.
Reabrir requiere preparación integral nueva. No se guardan raíces confiables persistentes.

Estados distinguidos: FULL_SOURCE_VERIFIED al preparar/verificar integralmente;
SESSION_VERIFIED para reutilización inicial bajo política; SEGMENT_VERIFIED para
segmentos consultados; SOURCE_NOT_REVALIDATED para el original global durante una
lectura rápida; SOURCE_CHANGED ante invalidación observable; VERIFICATION_NOT_AVAILABLE
para raíz ausente/cerrada. Un checkpoint rápido separa source_state y original_state.
Su alcance incluye SELECTED_SEGMENTS_AND_STABLE_SESSION_SNAPSHOT, nunca el archivo
actual completo. STRICT_FULL conserva la verificación integral de referencia.

## SQLite secundaria, locators y ZIP

Directorio temporal nuevo, fuera de Git, borrado al cerrar. Esquema secundario1:
cache_meta; locators(dataset,sequence,effective,available,valid_until,event_id,
raw_digest,offset,length,byte_digest,payload); links(segment,dataset,sequence);
segments(id,dataset,start,end,row_count,digest). PK/FK, índices time_locators y
**temporal_segments(dataset,start,end)**. El lector es mode=ro/query_only/FK/BEGIN.
Integrity/FK check se hacen al construir; no se escanea toda la cache por consulta.
Cada selección temporal se compara con la lista acotada de raíces independientes:
un índice adulterado no puede ocultar segmentos sin bloquear la consulta.

CSV/JSONL sin comprimir: offset y longitud exactos en bytes, fronteras de registros,
hash SHA-256 del span leído. CSV contempla BOM, CRLF, quotes y registros multilínea;
la validación semántica sigue exigiendo las columnas numéricas aprobadas. JSONL
contempla blancos previos sin confundir línea física con ordinal. El tamaño se
comprueba antes de materializar el span. Una fila no equivale a un offset.

CSV/JSONL Trade no copia velas a una tabla autoritativa: locators.payload=NULL.
ZIP comprimido y arrays JSON no permiten este acceso físico arbitrario. Tras
validación completa/CRC/límites se crea snapshot derivado canónico por fila en
SQLite secundaria, enlazado a original, DatasetId, parser y segmentos. Durante una
sesión se verifican esos snapshots contra raíces; no se rehasea el ZIP completo.
La presencia/estado del original se comprueba igualmente. Un snapshot no sustituye
el artefacto original ni se convierte en fuente oficial o base del colector.

La vista del registry queda congelada al abrir: modificaciones posteriores requieren
nueva sesión/selección. Un BEGIN sobre registry/cache no protege CSV/ZIP externos.
Se comparan dev/inode/tamaño/mtime/ctime y fd al leer como **indicadores de invalidez**;
no se les atribuye integridad criptográfica. El segmento leído compara byte_digest,
raw_digest, identidad económica, tiempos, conteo y digest contra raíz en memoria.
No se reutilizan verificaciones de contenido seleccionado entre consultas: se
vuelve a leer y verificar cada segmento necesario, mientras se reutiliza la raíz.

## Preflight, causalidad y finalización

read_trade/read_mark/read_funding/read_rules conservan start/end/cutoff y tipos.
Índices seleccionan segmentos; se verifica íntegramente cada segmento relevante y
se copia temporalmente su intervalo a un spool secundario en disco. Luego una sola
pasada causal fusiona fuentes, agrupa duplicados y detecta contradicciones antes
de entregar el primer resultado de `read`. No se prefiere mensual, diario o reciente.

Se reutilizan _combine, _rules_overlap, economic_identity y parse_record aprobados.
CONFLICTED_SAME_TIMESTAMP y CONFLICTED_RULE_VALIDITY bloquean `read`; `groups` conserva
conflictos auditables. Overlaps anidados, gaps y fuentes parciales se mantienen.
Un conflicto no disponible antes del cutoff no contamina el prefijo anterior.

Trade/Mark sólo tras cierre del minuto. Funding exige publicación explícita;
funding rate no significa pago ni periodicidad inferida. Reglas VERIFIED_FOR_PERIOD
requieren vigencia/publicación causal; CURRENT_ONLY/UNKNOWN_HISTORY no se autorizan.
market_effective_at, market_available_at y dataset_acquired_at conservan sus roles.
High/Low no son ticks ni reconstrucción intraminuto. Decimal exacto, sin floats
para economía y sin redondear precios. No se cambia Reference/OBSERVED.

El spool tiene inputs y results, índices temporal y económico, cache SQLite1MiB y
límite TEXT2MiB. Su digest completo esperado queda en memoria al acabar preflight.
El stream verifica orden, economía, cantidad y digest de entrega; consulta estado
observable/TTL antes de cada item y al finalizar. La cache limita TEXT a1MiB; filas
físicas y recursos heredan Limits, con restricciones adicionales documentadas.
Máximo 1 millón de filas de inputs/resultados y 1000 hallazgos de conflicto. Se
rechaza exceder límites; no se carga un mes en listas Python. El disco crece con
índices/spools; heap de merge se acota por fuentes y grupo por 10000 registros.

Los objetos entregados son candidatos **PROVISIONAL** hasta agotamiento exitoso:
stream.state=COMPLETE. Error final deja FAILED/reason; close/interrupción deja
INTERRUPTED. Un digest fallido o cambio durante stream no certifica el prefijo
parcial. El consumidor debe verificar COMPLETE antes de usar el resultado como
final; **no publicar ningún commit económico de datos PROVISIONAL**. No hay consumer
financiero en esta fase. Al cerrar se eliminan spool/cache; se conserva checkpoint
explícito sólo si el caller lo pidió. Una lectura abandonada debe cerrarse.

## Ejemplo opt-in offline

```python
from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
from trading_engine.replay.historical_validation.fast_query import SegmentedHistoricalQuery
from trading_engine.replay.historical_validation.verification import SessionPolicy

with SealedSegmentSession(path, ids, session_identity="review-session",
        opened_at=validation_time,
        policy=SessionPolicy(3600, 100, "Original local inmutable en sesión controlada")) as session:
    query = SegmentedHistoricalQuery(session)
    with query.read(kind, start, end, cutoff=cutoff) as stream:
        for candidate in stream:
            review(candidate)  # Sólo auditoría provisional, sin commit económico.
        assert stream.state == "COMPLETE"
```

## LO QUE EL MODO RÁPIDO NO GARANTIZA

- No demuestra integridad actual de segmentos no consultados; indicadores pueden
  quedar ocultos por un adversario fuera del modelo de estabilidad declarado.
- Para ZIP/JSON snapshot, comprueba contenido derivado anclado, no todos los bytes
  comprimidos actuales del original. original_state sigue SOURCE_NOT_REVALIDATED.
- No protege frente a un atacante que modifica memoria/raíces/código del proceso.
- No hereda confianza entre procesos o autentica un checkpoint con su propio hash.
- No garantiza ausencia de cambios transitorios/revertidos entre comprobaciones.
- No prueba Binance, publicación real, vigencia oficial, saldos, fills o pagos.
- No autoriza fuentes económicas ni certifica supervivencia/rentabilidad.
- No promete tiempos absolutos, acceso aleatorio ZIP o eficiencia financiera multianual.

Para exigir comprobación global actual: STRICT_FULL. El modo rápido requiere
aceptar explícitamente su alcance menor; está OFF por defecto. Los benchmarks
incluyen preparación, validación parcial, recuperación y proceso nuevo por separado.
