# Replay causal — Fase 4A-1

Infraestructura offline implementada sobre **el mismo AltBEngine y
StrategyCoordinator** aprobados. No contiene fórmulas estratégicas, simulador
histórico de fills, transporte al exchange ni cálculo de rentabilidad.
`FINANCIAL_RESULTS_NOT_AVAILABLE` aparece siempre en el informe.
El runtime principal continúa admitiendo únicamente STOPPED.

## Intervalo y lectura

`ReplayRequest(start, end, allow_entries=False)` exige fechas UTC aware,
alineadas al minuto, con start < end. `AltBConfig` aporta el símbolo inicial
1000SHIBUSDC y el warmup canónico de 1560 minutos. Mercado
BINANCE_USDM_FUTURES, intervalo 1m, sin conversión de símbolo.

- Warmup: aperturas en `[start - 1560 minutos, start)`. Se entrega al motor con
  entradas deshabilitadas; el cierre exactamente en start pertenece al warmup.
- Evaluado: aperturas en `[start, end)`, disponibles al cerrar en `(start, end]`.
  Una acción/hecho exactamente en end se procesa; los posteriores permanecen
  pendientes. No se estira end para resolver una ronda ni completar warmup.
- La activación explícita del intervalo ocurre en start después del último
  cierre de warmup y antes de los controles externos del mismo instante.
  allow_entries autoriza exclusivamente intenciones teóricas offline.

`SQLiteHistory` utiliza `FuturesSQLite(read_only=True)`: mode=ro, query_only y
transacción de lectura estable durante toda la sesión del context manager.
No crea bases ausentes, índices, tablas, locks de escritura o manifiestos.
Cada tick consulta únicamente el minuto que **ya cerró**, por el índice existente;
no decodifica ni incorpora OHLC futuros para decidir antes. Conserva Decimal
exacto y taker-buy original, incluido None. La ventana del motor permanece
acotada a 1560 Candle; no se materializan años completos.

El esquema aprobado impide duplicados mediante su PK. La lectura también
rechaza duplicados/conflictos en una tabla dañada sin PK: un minuto se valida
completo antes de entregarlo. Intervalos u OHLC corruptos interrumpen el runner
con causa explícita; no se sobrescriben datos. Los conflictos de manifiesto
existentes bloquean la sesión antes de procesar datos (preflight conservador,
no decisión financiera ni selección de una versión del dato).

Los minutos ausentes se registran como rangos semiabiertos `gap`. Los anteriores
al límite de contrato ya aprobado se registran `pre_contract`; cualquier vela
persistida antes de ese límite se rechaza. El primer minuto guardado disponible
se informa separadamente y no se afirma que sea la disponibilidad global real
comprobada en Binance. No se descarga, interpola ni fabrica ninguna vela.

Un gap se conoce al cierre esperado del minuto ausente. En ese instante se
invalida la acción pendiente mediante el coordinador. La siguiente Candle real
hace que la ingesta canónica reinicie su ventana; vuelve a exigir 1560 minutos
consecutivos antes de otro checkpoint válido. La evidencia Base/DCA/TP permanece
intacta. Los bloques de checkpoint conservan su motivo estratégico/validez en
`checkpoint_blocks`, incluido insufficient_history y missing_current_taker_buy.

## Reloj y secuencia exacta

`SimulatedClock` es frozen, solo permite avance monotónico con UTC explícito.
No llama datetime.now(), sleep(), timers ni transportes. El runner elige el
siguiente instante entre cierre 1m, inicio evaluado, received_at de hechos y
not_before de acciones ya existentes. El scheduling estratégico procede
exclusivamente del coordinador; el runner no crea otra cola de acciones.

Para eventos que coinciden, el orden es:

1. Solo en start: último cierre de warmup y activación del intervalo.
2. Controles EntryControl (PAUSE/RESUME).
3. Snapshots Order explícitos.
4. FillEvidence explícitos (Fill + snapshot Order correspondiente).
5. Candle cerrada o detección del minuto ausente.
6. `advance_time()` extrae las acciones elegibles en orden canónico del
   coordinador `(not_before, ActionId.int)`.
7. Para cada acción: obtener QuoteEvidence explícita; entregar `on_quote()` y
   `on_scheduled_action()`, o invalidarla por ausencia/invalidez.
8. Registrar las emisiones de dominio y conservar el nuevo CoordinatorState.

Los hechos se ordenan `(received_at, prioridad de clase, sequence)`. sequence
es explícita y única, no se deduce del orden de inserción. PAUSE o gap coincidentes
con T+120 preceden a Base. Una señal en el cierre T solo permite Base en T+120,
nunca antes. Los filtros, IDs, reservas, DCA1–DCA7 y TP pertenecen al motor.

Cada resultado entrega señales, acciones nuevas/canceladas, intenciones y
solicitudes de cancelación como `ReplayEmission`. Una acción consumida por Base
no se cuenta como cancelada. `cancel_request` conserva su intención correlacionada;
no confirma cancelación. Order terminal y Fill siguen siendo hechos distintos.

## Puertos de Quotes y hechos

`QuoteSource.at(symbol, at)` retorna QuoteEvidence o None. Por defecto `NoQuotes`
no ofrece ninguna cotización. `FixtureQuotes` exige cotizaciones suministradas,
sin duplicados por símbolo/instante, y las etiqueta **SYNTHETIC_FIXTURE**.
Nunca transforma Candle.close/high/low en bid/ask.

Esta fase exige **Quote.timestamp == action.not_before == reloj actual**, con
el mismo símbolo. Es una política explícita de causalidad de fixtures, sin
asumir un spread, vigencia máxima o frescura histórica todavía no aprobados.
Un Quote anterior, posterior o de otro símbolo no permite Base. No se busca
silenciosamente el último Quote ni se espera a otro futuro.

En T+120 sin evidencia válida se emite missing_quote con su motivo, se llama
invalidate_action y se registra cancelled_action. Desaparecen tanto la acción
como su reserva PendingEntry. Una cotización tardía no reactiva esa señal;
se exige una nueva señal en un nuevo checkpoint. El motivo de ausencia queda
conservado aunque posteriores eventos cambien el bloqueo actual del motor.

`FactSource.read(cursor, until)` aporta hechos inmutables, con cursor ordinal
estable e identidad de fuente. `FixtureFacts` conserva ReplayFact explícitos
con procedencia SYNTHETIC_FIXTURE. No recibe intenciones para generar respuestas,
no implementa ExecutionAdapter.submit/poll ni un modelo de fills.

ReplayFact distingue `received_at` (disponibilidad/entrega) de Order.updated_at y
Fill.timestamp (ocurrencia). Ambos timestamps de evidencia deben ser <= received_at;
el runner no permite hechos previos al intervalo evaluado. La evidencia tardía
se entrega al mismo motor sin retroceder el reloj ni inventar nuevas ocurrencias.
Los retries idénticos son idempotentes en el motor; las entregas explícitas
repetidas siguen contando como eventos observados en la huella.

No se deduce ningún Fill por Candle.low/high. Si una vela toca DCA y TP, la
secuencia intraminuto sigue desconocida. Solo hechos inyectados pueden activar
P0 efectivo, escalera, reemplazos TP, cancelaciones confirmadas y cierre.

## Snapshot y reanudación

`ReplayRunner.snapshot` es frozen e incluye:

- Request y configuración original, identidad de fuentes y token de sesión SQLite.
- Reloj lógico y next_open, incluso cuando el minuto anterior era un gap.
- Cursor de hechos y activación del intervalo evaluado.
- CoordinatorState completo: cola pendiente y AltBStrategyState con ventana,
  permiso, PendingEntry, Base, órdenes/fills, gestión de ronda e historial.
- Contadores, rangos inválidos, motivos de checkpoints y emisiones ya producidas.
- Huella acumulada, número de eventos y causa de interrupción.
- Checksum integral en memoria que detecta pérdida accidental de campos al restaurar.

Se restaura pasando `snapshot=` a otro ReplayRunner en **la misma sesión abierta
SQLiteHistory**, con Request, configuración y fuentes idénticos. Se rechazan
reloj/estado futuros, acciones vencidas, pérdida de una acción reservada y cambios
de configuración/fuentes. Se validan el checksum de todos los campos y los contratos públicos del mismo
motor; reconstruir solamente velas no permite borrar reservas u órdenes.
El checksum detecta alteraciones accidentales; no es una firma de seguridad.
El token de sesión es una identidad opaca en memoria, excluida de las huellas y
resultados técnicos; no es un ID de trading ni un identificador persistente.

La equivalencia continua/fragmentada incluye estado, emisiones, reservas,
órdenes/fills, cancelaciones pendientes, contadores y huella. `run(until)` admite
UTC subminuto para verificar T+120 y entregas; no registra llamadas vacías ni
límites artificiales de fragmento, por lo que los retries no alteran el resultado.

No existe deserialización validada, journal, recovery entre reinicios ni garantía
exactly-once externa. `ReplayReport.as_dict()` es una representación para informe,
**no un formato de restauración**. Una interrupción es terminal en esa sesión;
se conserva el último estado de evidencia válido para revisión.

## Informe técnico

ReplayReport contiene símbolo, mercado, intervalo y período solicitado;
processed_until; inicio efectivo evaluado y primer minuto leído;
minutes_read, warmup_read/warmup_applied y evaluated_read; rangos gap/pre_contract;
checkpoints del intervalo evaluado y sus bloqueos; señales, acciones programadas/canceladas,
actions_without_quote, fixture_intents/order_intents y cancel_requests;
CoordinatorState final, block_reason/interruption y huella SHA-256 de eventos.

La huella encadena el digest anterior y JSON canónico de `(reloj, kind, payload)`;
Decimal y UUID se codifican como texto, datetime en ISO UTC y campos con orden
estable. Incluye Request/configuración, velas consumidas, ausencias, controles,
hechos, Quotes utilizados, acciones due, emisiones e interrupciones. No incluye
el reloj del sistema, el token de sesión ni el contenido futuro no consumido.
La lista de Candle permanece limitada; solo se retienen las emisiones y evidencia
necesarias para el estado/resultados, no un log completo de todas las velas.

No informa rentabilidad, equity, drawdown, fees o funding calculados.
`financial_results = FINANCIAL_RESULTS_NOT_AVAILABLE` es obligatorio.

## Uso de biblioteca offline

```python
from datetime import UTC, datetime
from pathlib import Path
from trading_engine.replay import ReplayRequest, ReplayRunner, SQLiteHistory

period = ReplayRequest(
    start=datetime(2026, 1, 2, 11, 45, tzinfo=UTC),
    end=datetime(2026, 1, 2, 12, 5, tzinfo=UTC),
    allow_entries=True,  # autorización de fixtures offline, sin ejecución
)
with SQLiteHistory(Path("/ruta/explicitamente/elegida/existente.sqlite3")) as history:
    runner = ReplayRunner(history, period)  # NoQuotes: Base permanece fail-closed
    report = runner.run()
    technical_result = report.as_dict()
```

No se añade una CLI a producción ni se habilita OperatingMode.REPLAY.
Las pruebas usan exclusivamente bases temporales y fixtures pequeños.
El replay histórico completo 2024–2026 permanece fuera de esta entrega.

## Decisiones pendientes para Fase 4A-2

Revisar antes de construir el simulador financiero:

- Construcción de Quotes desde datos 1m y limitaciones frente a bid/ask real.
- Spread, slippage y validez temporal del Quote.
- Activación/prioridad LIMIT, fills parciales, volumen y liquidez.
- Latencia de ejecución/cancelación y orden intraminuto desconocido.
- Fees y funding USDⓈ-M Futures.
- Datos insuficientes y política de shortfill.
- Comparación de escenarios conservadores y sensibilidad a esos supuestos.

No se eligen valores arbitrarios ni se presentan fixtures como datos reales.
Las fórmulas estratégicas aprobadas permanecen separadas de esas políticas.
