# Preparación causal de funding — Fase 4A-5D4

Base exacta: `b483a1c531e16903f98f2bceac13444974b96ca5`.
Opt-in, offline, runtime STOPPED. **Integración financiera dinámica pendiente**.
Se aplica la alternativa preparatoria autorizada en el apartado 11 del encargo.

## Arquitectura implementada

HistoricalDatasetRegistry → HistoricalQuery / SealedSegmentSession →
HistoricalReconciliation FUNDING + FundingInventory → PreparedFundingHistory →
HistoricalFundingSource → HistoricalFundingRateEvent / FundingInventoryPublication.

La salida es tasa histórica e inventario declarado, nunca pago, CoverageEvidence,
FinancialEvent o FundingEvidence. HistoricalFundingSource **no implementa** el puerto
FinancialSource. No se conecta al ReplayRunner ni se cambia FinancialPerformance.
No existe asignador productivo por posición/ronda en esta entrega. No se puede
anunciar rentabilidad, funding económico completo o replay multianual con esta capa.

## Contrato y fuentes

DatasetIds explícitos, únicos y acotados. Solo FUNDING_RATE_HISTORY, EVENTS,
BINANCE / BINANCE_USDM_FUTURES / 1000SHIBUSDC / PERPETUAL / USDC.
Se reutilizan Contract, FundingRate, FundingInventory, HistoricalEvent, registro,
conciliación, parsers, sesiones, límites AdapterLimits y encoding aprobados.
No se admiten sustituciones Spot, USDT, Trade, Mark, Index o reglas.
El destino financiero pendiente es LONG, ISOLATED, ONE-WAY; no se modifica el 4x
aprobado ni se introduce una variante 6x. Una tasa no demuestra una posición.

Tasas Decimal exactas expresadas como fracción: 0.0001 = 0.01%. El parser aprobado
valida números finitos, rango declarativo y Mark opcional positivo. No hay ×100,
factor 1000 adicional, inferencia de apalancamiento o conversión monetaria.
mark_price ausente se conserva; no se rellena con Close, bid/ask o una vela cercana.

## Preparación atómica y modos

PreparedFundingHistory(path, dataset_ids, start, end, cutoff=..., inventory=...)
exige UTC aware, start >= lanzamiento, start < end y cutoff > start. No exige
alineación a minuto: funding son eventos. inventory es un parámetro explícito,
incluido None para una selección sin inventario. Un inventario aportado debe
corresponder exactamente al período solicitado y respetar el límite de filas.
Los inventarios persistidos no pueden contradecir al explícitamente seleccionado.
No se agrega ni se prefiere un calendario diario/mensual incompatible.

Se concilia el intervalo efectivo [start,end), con disponibilidad <= cutoff. Los
conflictos bloquean antes de abrir una preparación. Se consumen las consultas
completamente, se verifica QueryStream.COMPLETE en modo segmentado y se comparan
conteos únicos, duplicados y digest con la conciliación. Los bindings se vuelven
a contrastar antes de aceptar. Excepción o cuota agotada limpia el temporal.

STRICT_FULL valida íntegramente originales y consume un snapshot preparado.
SEALED_SEGMENT_SESSION exige la sesión explícita: TTL monotónico, estado de archivos
y sellos mantienen SEGMENT_VERIFIED / SOURCE_NOT_REVALIDATED. Nunca se elevan a
autenticación global. El caller conserva la propiedad de la sesión segmentada:
cerrar la preparación no cierra automáticamente esa sesión externa.

## Orden de entrega y almacenamiento

La consulta económica se recorre por instante efectivo para acreditar su digest.
SQLite temporal reordena en disco por (market_available_at, market_effective_at)
y asigna un ordinal estable. El orden económico no demuestra orden de recepción:
una tasa anterior publicada tarde debe entregarse después de otra ya disponible.
Los conflictos ya excluyen dos economías distintas del mismo instante efectivo.

Bloques de hasta 1440 filas, raíces SHA-256 de (ordinal, disponibilidad, payload),
retenidas en memoria. Cache máximo un bloque. Antes de interpretar cualquier JSON
del bloque se verifican todas sus filas, ordinales, conteo y raíz. Manipulación
de payload, cursor o raíz invalida y cierra; no se transforma en tasa ausente.
La conexión aceptada es read-only, query_only, con transacción estable.

Hay cuotas de filas, TEXT y disco; el ordenamiento temporal usa temp_store=FILE.
La tabla de staging y la de entrega pueden coexistir durante preparación y sus
páginas cuentan contra la cuota de disco. El archivo puede conservar páginas libres
después de DROP. No se presenta su tamaño como el de una sola copia lógica.

## Inventario y suficiencias independientes

FundingInventory conserva período [start,end), timestamps exactos ordenados, fuente
y market_available_at >= end. No se deduce un calendario de ocho horas ni se
construye la lista esperada a partir de las tasas presentes en producción.
En tests/benchmarks el calendario es sintético y se declara independientemente.

PreparedFundingEvidence conserva el inventario, bindings con DatasetIds, hash del
original, auditoría, registro/procedencia y una identidad del conjunto. Son evidencia
de integridad local, no autenticación Binance. Los estados están separados:

| Campo | Significado |
| --- | --- |
| inventory_status=COMPLETE | Las ocurrencias disponibles coinciden exactamente con el inventario explícito ya disponible al cutoff. |
| INCOMPLETE | Faltan/sobran timestamps del inventario publicado. |
| NOT_AVAILABLE | No hay inventario explícito o todavía no está disponible al cutoff. |
| reference_status=INCOMPLETE | Alguna tasa conservada carece de Mark de referencia. |
| CONFLICTED | La preparación falla por tasas o inventarios incompatibles. |
| authentication=NOT_AUTHENTICATED | No existe autenticación independiente. |
| economic_authorization=NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES | No se concedió permiso económico. |

Un inventario explícito vacío puede coincidir como inventario de tasas. No produce
un pago cero ni cobertura financiera. Sin filas, la suficiencia de referencias es
vacua y no acredita patrimonio. Se conserva además el ReconciliationReport legado:
su funding_status incluye suficiencia Mark y sigue considerando incompleto el caso
vacío; no se modifica ni se reemplaza ese contrato por el nuevo campo independiente.

## Publicación y tiempos

FundingPublicationPolicy exige latency_ms entero no negativo, cuota por lectura
1..10080, publication="SYNTHETIC_FIXTURE_ONLY" y assumptions no vacío, sin defaults
de mercado. Todas las fuentes seleccionadas deben declarar procedencia FIXTURE.
Fuentes reales declaradas, UNKNOWN o selecciones mixtas bloquean la publicación,
aunque puedan prepararse para revisión. MODELED y OBSERVED no son opciones del gate.

Cada HistoricalFundingRateEvent conserva la FundingRate exacta, HistoricalEvent,
DatasetIds, fuentes, adquisición en bindings, verificación e identidades. Su
received_at es funding_market_available_at + latencia explícita; solo se entrega
si received_at <= until <= cutoff. Puede ser posterior al end efectivo del período.
No se desplaza el timestamp efectivo. La adquisición no se vuelve disponibilidad
pasada por ejecutar un backtest; la simulación fixture declara tiempos hipotéticos.

inventory_at(until) solo devuelve el inventario después de
inventory_market_available_at + latencia. La publicación no incluye una afirmación
de cobertura económica ni promete que todas las tasas esperadas estén recibidas:
puede existir una tasa tardía aún ausente en ese instante. La coincidencia offline
al cutoff no debe usarse como conocimiento económico anterior a su recepción.

Un cursor es cantidad de tasas entregadas, no minutos ni inventarios. Las páginas
de una misma preparación conservan eventos e identidades exactamente. La identidad
de preparación incluye modo, bindings, período, cutoff, inventario y raíces. Los
EventIds de tasa dependen de su EventId económico y política, y coinciden entre
modos y prefijos económicos equivalentes. Los metadatos globales de procedencia
pueden cambiar al extender un archivo; no se proclama invariancia de esos metadatos.

El token history_session es opaco, propio de cada preparación y no persistente.
No es intercambiable por la identidad determinista. Se comprueba la fuente al
entrar, antes de cada entrega, al finalizar lecturas y antes de devolver inventario.
Cierre, expiración o SOURCE_CHANGED se propagan; el prefijo devuelto no se reescribe.
Son checks de disponibilidad/TTL/stat, sin rehash completo de originales por evento.

La frontera de entrega exige construir cada HistoricalFundingRateEvent en una
variable local y volver a comprobar confianza inmediatamente antes de `yield`.
Una invalidación durante la construcción descarta ese candidato y propaga el error
original de confianza (incluidos SESSION_EXPIRED y SOURCE_CHANGED); nunca lo entrega
para fallar en la siguiente iteración. Una excepción del constructor se propaga sin
sustituirla por un segundo error de validación. El prefijo ya entregado conserva
contenido, identidades y ordinales; el consumidor solo avanza su cursor por eventos
efectivamente recibidos. La sesión invalidada no permite reanudar ni saltar filas.
Esto no añade locking entre hilos ni modifica las garantías STRICT_FULL/SEALED.
`inventory_at()` conserva su comprobación posterior a construir la publicación y
sus contratos de disponibilidad, latencia, cutoff e inventario ausente.

## Bloqueo de integración financiera

ReplayRunner materializa FinancialEvent en su constructor. FundingEvidence ya exige
RoundId y exposición; inventarlos allí usaría fills futuros o cantidades teóricas.
_financial_tick observa ledger, eventos y VALUE; _validate_snapshot exige igualdad
exacta entre los eventos reconocidos y la lista financiera original. Introducir
tasas como pagos falsos, omitir el validador o aceptar eventos extra adulterables
violaría los contratos aprobados. Reference mantiene el bloqueo OBSERVED intacto.

Además, FundingInventory usa [start,end) y CoverageEvidence usa [start,end], con
publicación no anterior al final. Copiar extremos o listas esperadas no demuestra
cobertura del punto end ni inventario de pagos por ronda. No se retimestampa ni
publica cobertura anticipada para obtener una equity completa artificial.

Son bloqueos de contrato para una extensión conjunta de agenda, asignación y
restauración. Esta fase no reestructura esos motores. AUD-5D3-001 permanece intacto.

## Diseño pendiente para revisión

1. Aprobar un puerto de tasas/inventario separado de FinancialSource y su agenda
   opt-in por received_at. Conservar el orden de hechos, fills y cierres existente.
   Fill con executed_at exactamente en funding debe bloquear sin evidencia cruzada
   suficiente; el orden recibido o FillId no demuestra prioridad económica.
2. Reconstruir Exposición(T) desde FinancialLedger confirmado y recibido a R:
   BUY executed_at<T menos SELL executed_at<T, en orden canónico FillId aprobado.
   Validar ledger, contrato LONG/ONE-WAY y unicidad de ronda expuesta. Una ronda
   cerrada antes de T no paga; una cerrada después de T puede recibir tarde un pago
   anterior. No elegir la ronda actualmente activa al recibir una tasa tardía.
3. Validar referencia positiva exactamente en T; falta de referencia conserva
   NOT_AVAILABLE. Crear FundingEvidence solo con exposición >0, RoundId demostrado,
   tasa, referencia, FillIds, hipótesis y autorización fixture explícitas. La fórmula
   sigue en FinancialPerformance: -(cantidad × referencia × tasa). No duplicarla.
4. Conservar un journal de asignación separado, incluso para NO_EXPOSURE y datos
   insuficientes. Un fill tardío que cambie una exposición supuestamente cero debe
   bloquear también: no basta con proteger solo pagos ya creados. Necesita contrato
   explícito de finalización/watermark de exposición y política de llegada tardía.
5. Derivar identidad de pago de tasa, ronda, fills, exposición, referencia, inventario
   y política. Dedupe por identidad económica; tasas contradictorias bloquean.
   No cambiar ID de un pago reconocido ni volver a cobrar al reanudar.
6. Aprobar el inventario económico de pagos y su dominio temporal. Debe enlazar
   tasas esperadas con resultados de asignación demostrados; no basta con copiar
   timestamps a event_ids. Resolver explícitamente el punto final y disponibilidad
   antes de construir CoverageEvidence. Solo evidencia completa permite cero.
7. Vincular identidad de funding y token de sesión a performance y snapshot. Añadir
   opt-in cursor, pendientes y journal con omit_if_none para preservar legados. La
   restauración debe reconstruir desde fuentes/facts/simulador autorizados, no confiar
   en un journal auto-consistente ni relajar la igualdad actual de eventos. Comparar
   estado completo, series, cobertura, riesgo, reservas, digest y pendientes.
8. Preparar cada transición económica localmente. Validar sesiones antes de asignar,
   antes de observar, antes de VALUE y antes de confirmar en snapshot; incluir cierre,
   report y ruta de error. Pérdida de confianza descarta el tick y conserva el prefijo,
   manteniendo AUD-5D3-001 y sin contaminar el ledger con fallos de origen.
9. Probar A–P contra la nueva integración, especialmente fills modelados durante run,
   dos rondas sucesivas, cero seguido de fill tardío, empates, inventario tardío,
   invalidación después de VALUE y reanudación en ambos lados de cada evento.

Esto requiere revisión adicional; no es un plan autorizado para modificar motores
en esta entrega ni autorización para 4A-5D5, datos reales, LIVE o despliegues.

## Memoria, límites y uso

La preparación almacena tasas en disco, raíces O(bloques), cache O(1440 registros).
FundingInventory ya contiene una tuple O(eventos esperados); no se anuncia memoria
total constante. El consumidor de tasas puede contar por páginas sin retenerlas.
El runner y los journals financieros siguen siendo monolíticos y no se han medido
como consumidores de funding nuevo. Replay 2024–2026 no está certificado.

Para escala multianual faltan agenda por cursor, checkpoints verificables, inventario
económico autenticado, cierre de exposición, compactación/persistencia y política
de recovery. Requieren revisión específica. Benchmarks y resultados reales en
PHASE_4A_5D4_REPORT.md; no son una afirmación de rendimiento financiero multianual.

```python
# Selección local FUNDING explícita; inventory puede ser None para revisión incompleta.
with PreparedFundingHistory(path, ids, start, end, cutoff=cutoff, inventory=inventory) as history:
    source = HistoricalFundingSource(history, FundingPublicationPolicy(
        latency_ms=250, max_events_per_read=1440,
        publication="SYNTHETIC_FIXTURE_ONLY",
        assumptions="Calendario y disponibilidad sintéticos; no observaciones Binance",
    ))
    rates = source.read(cursor, until)  # HistoricalFundingRateEvent, no FinancialEvent
    declared_inventory = source.inventory_at(until)  # no CoverageEvidence
```

La muestra no crea capital, pagos, fees, riesgo ni una sesión operativa. En modo
SEALED se aporta SealedSegmentSession explícita y se conserva abierta durante consumo.
