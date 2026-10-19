# Adaptador causal Mark Price — Fase 4A-5D3

Base exacta: `5ba8525bedd409673d902ab6b78d706d099f8efa`. Opt-in, offline, runtime STOPPED.

## Arquitectura y autorización

Registry → HistoricalQuery/SegmentedHistoricalQuery → HistoricalReconciliation MARK →
PreparedMarkHistory → HistoricalMarkPriceSource → FinancialPerformance → IsolatedMarginRisk.
Se reutilizan MarkKline, HistoricalEvent, registro, parsers, reconciliación, sesiones,
FinancialEvent, MarkEvidence y los motores financieros aprobados. No cambia Reference:
OBSERVED sigue rechazado; MODELED no es una vía de autorización para históricos reales.

PreparedMarkHistory conserva NOT_AUTHENTICATED y NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES.
La preparación es válida para revisión histórica sin permiso financiero. La publicación exige
`publication="SYNTHETIC_FIXTURE_ONLY"` explícito y todas las fuentes registradas como
SYNTHETIC_FIXTURE con evidencia de procedencia. Fuente declarada, UNKNOWN o selección mixta
bloquean antes de entregar el primer evento, incluso si su SHA es correcto. El gate concede
únicamente uso de fixture a esta instancia; no cambia la autorización del registro.
La autenticación independiente y la autorización de datos reales permanecen pendientes.

## Preparación, almacenamiento e identidad

DatasetIds explícitos, únicos y acotados; categoría exclusivamente MARK_PRICE_KLINES_1M,
BINANCE/BINANCE_USDM_FUTURES/1000SHIBUSDC/PERPETUAL/USDC/1m. Precios USDC por unidad
contractual 1000SHIB; sin factor 1000 adicional. No se acepta Trade, Quote, Index o Funding.

Constructor recibe start/end UTC alineados a minuto y cutoff UTC exacto. Conciliación y consumo
completo deben coincidir en digest, filas y duplicados. QueryStream debe finalizar COMPLETE.
Conflictos bloquean antes de publicar. Gaps se conservan en la cobertura; no se fabrican filas.
No se prepara un intervalo sin un minuto completo disponible; una selección sin muestras bloquea la publicación financiera. La preparación es atómica en SQLite temporal con límites de
filas/bytes y limpieza ante fallo. Solo el estado completo abre un snapshot read-only.

Bloques de hasta 1440 registros, raíz SHA-256 del ordinal y payload completo retenida en
memoria. Lectura por ordinal, verificación íntegra del bloque antes de publicar y cache máximo
un bloque. El payload JSON se decodifica únicamente después de verificar la raíz completa del bloque. Cursor, payload, índice o raíz incoherentes invalidan y cierran. El cache en memoria
es el snapshot aceptado. Los originales/registro no se modifican. Se confía en el proceso y
sus raíces; no son firmas de Binance ni recovery persistente.

Identidad preparada versionada incorpora selección/bindings, intervalo/cutoff, modo,
conciliación y raíces. La identidad económica separada conserva los mismos valores y EventIds
entre modos; conserva fuente/procedencia por separado. Bindings incluyen adquisición y hash
original. La misma declaración/archivo produce la misma identidad; mover el archivo cambia
la identidad del registro conforme a su contrato existente.

STRICT_FULL verifica originales integralmente durante preparación y consume el snapshot.
SEALED_SEGMENT_SESSION mantiene sesión explícita, TTL y estabilidad; conserva SEGMENT_VERIFIED
/SOURCE_NOT_REVALIDATED, sin elevarlo a autenticación global. Cierre, expiración y cambios de
fuente bloquean. Los fallos de confianza se propagan; nunca se convierten en ausencia de Mark.

## Tiempo, frescura y gaps

MarkKline: effective=apertura, available=cierre. MarkEvidence: effective_at=cierre de la muestra,
received_at=cierre+latencia explícita. Se entrega únicamente si received_at<=until y
cierre<=cutoff. Adquisición queda como procedencia. Verificar físicamente un bloque no habilita
sus muestras futuras. until subminuto se respeta exactamente y consultar más allá del cutoff
falla. Latencia cero es una hipótesis explícita del fixture, no una observación de Binance.

MarkPublicationPolicy requiere latencia, freshness_limit_ms, límite de eventos por lectura,
procedencia/hipótesis y gate explícitos, sin parámetros financieros por defecto. Esta primera
integración de cierres aislados exige 0<=freshness_limit_ms<60000: una muestra no acredita el
siguiente cierre ausente. La cifra 60000 corresponde al intervalo contractual de un minuto;
no se afirma una frescura oficial del exchange. Latencia puede exceder frescura: la muestra
llega obsoleta y el motor conserva NOT_AVAILABLE según sus validadores actuales.

No interpolación, forward-fill, retimestamp ni sustitución por Trade. continuous es verdadero
para una primera muestra en el inicio solicitado y para muestras consecutivas; una primera
muestra tras un gap se publica con continuous=False. La siguiente consecutiva recupera la
continuidad. Esta regla conservadora no afirma continuidad intraminuto; la trayectoria
continúa UNKNOWN_INTRAMINUTE_PATH. Un precio ausente no produce MarkEvidence artificial.

## Publicación, evidencia y sesión

HistoricalMarkEvent amplía FinancialEvent con el HistoricalEvent original, identidad preparada,
política, modo y autorización de fixture. Conserva OHLC, calidad, EventId, DatasetIds, fuentes,
apertura/cierre, recepción y secuencia. MarkEvidence conserva Close exacto sin recalcular
exposición, VWAP, costos o PnL. Reference usa SYNTHETIC_FIXTURE e identifica la fuente histórica.
Los IDs son deterministas, derivados del registro económico y política; duplicados equivalentes
producen una sola muestra con todas las fuentes. El cursor cuenta eventos publicados, no minutos.

La fuente conserva un token opaco por preparación. ReplaySnapshot añade un vínculo opcional a
esa sesión, omitido cuando no se usa. Es una condición de confianza por identidad, excluida del
checksum igual que history_session. Restaurar con otra preparación se rechaza aunque sus IDs
coincidan y se recalcule el checksum. El runner consulta/checkea la fuente durante consumo,
además de validar al materializar; no hereda confianza de eventos pendientes de un snapshot.
Los formatos y fingerprints sin esta fuente permanecen idénticos.

AUD-5D3-001 explicita esas comprobaciones en ReplayRunner: entrada del tick financiero,
antes de cada evento pendiente de clase marks, antes de VALUE y antes de confirmar el
estado local (journal, serie y cursor), al finalizar run() y al entrar en report(). La
comprobación de identidad de FinancialPerformance continúa intacta como protección
adicional. Una pérdida de confianza se propaga sin convertirla en conflicto contable ni
volver a observar con la fuente inválida; el prefijo financiero confirmado se conserva.
Son comprobaciones de disponibilidad, TTL y metadatos de archivos en modo segmentado;
no releen ni recalculan el hash de los datasets por evento. STRICT_FULL conserva su
contrato de snapshot preparado: no incorpora vigilancia nueva de los originales.
Secuencia de reproducción y evidencia: AUD_5D3_001_REPORT.md.

## Memoria y límite de integración

PreparedMarkHistory lee streaming y mantiene un bloque; disco/raíces crecen de forma acotada
por AdapterLimits. La fuente convierte por iterador y usa un cursor ordinal, sin tuplas de años.
ReplayRunner y los journals/series financieros existentes acumulan eventos. En esta fase,
max_events_per_read es obligatorio y no puede superar 10080 (una semana de muestras 1m).
La fuente cuenta el rango antes de entregar y falla si excede el límite, también en el
constructor del runner. Esta cota de recursos no es un supuesto de mercado. Para consultar
una preparación mayor se permiten páginas acotadas explícitas mediante cursor/until.

No se certifica Replay financiero multianual. Ese uso queda bloqueado por el contrato de
materialización/journals actual. Una fase posterior necesitaría agenda financiera por cursor,
checkpoints de consumidor y persistencia/compactación; requiere aprobación antes de modificar
estructuralmente runner o motores. Los benchmarks separan preparación/lectura/conversión y
no afirman que leer streaming elimine la retención del consumidor.

## Finanzas y límites

FinancialLedger sigue siendo autoridad de exposición/costo. Se conservan
unrealized=open_quantity*Mark-open_cost_basis y las dependencias de equity/wallet/neto.
Sin capital, fees, funding o cobertura, las métricas dependientes permanecen NOT_AVAILABLE.
Riesgo utiliza únicamente el Mark reconocido por performance con evidencias independientes
para margen y brackets. Sin ellas, mantenimiento/buffer siguen indisponibles. No cambia ninguna
fórmula, estrategia, filtro, reserva o ejecución. Mark1m no acredita supervivencia, liquidación
oficial ni trayectoria intraminuto. No hay datos reales, red, credenciales, LIVE o SHADOW.

Validación y resultados reproducibles: PHASE_4A_5D3_REPORT.md.

## Uso explícito de biblioteca

```python
from trading_engine.replay.financial_performance import FinancialPerformance
from trading_engine.replay.runner import ReplayRunner
from trading_engine.replay.historical_validation.mark_history import PreparedMarkHistory
from trading_engine.replay.historical_validation.mark_source import (
    HistoricalMarkPriceSource, MarkPublicationPolicy,
)

# path/ids/start/end/cutoff provienen de una selección MARK local explícita.
with PreparedMarkHistory(path, ids, start, end, cutoff=cutoff) as marks:
    policy = MarkPublicationPolicy(
        latency_ms=250, freshness_limit_ms=59999, max_events_per_read=1440,
        publication="SYNTHETIC_FIXTURE_ONLY",
        assumptions="Fixture sintético; latencia/frescura asumidas, sin calibración Binance",
    )
    source = HistoricalMarkPriceSource(marks, policy)
    # Solo dentro de este with, con ledger/política/otras fuentes explícitas:
    performance = FinancialPerformance(performance_policy, financial_source, source)
    runner = ReplayRunner(trade_history, request, ledger=ledger, performance=performance)
    report = runner.run()
```

El ejemplo no aporta capital, fees, funding o brackets. Sus variables requieren instancias
explícitas conforme a sus contratos existentes; tampoco activa runtime operativo. Para el modo
segmentado se aporta SealedSegmentSession y mode explícitos. El límite de 1440 de este ejemplo
es de recursos del fixture; no una restricción ni recomendación oficial de mercado.
