# Fase 3C-1 — Replay causal offline

`replay/runner.py` reutiliza exactamente AltBEngine y StrategyCoordinator. No cambia
estrategia, fórmulas, identidad o ejecución. Runtime STOPPED; sin descargas, servicios,
exchange, despliegue ni ExecutionAdapter. OrderIntent es salida de dominio, no orden enviada.

```text
FuturesSQLite.read(read_only=True) → Candle 1m cerrada ┐
Control / Quote / Order / FillEvent explícitos       ├→ ReplayRunner
                                                    ↓
                                          StrategyCoordinator
                                                    ↓
                                            mismo AltBEngine
                                                    ↓
                          CoordinatorState completo + outputs + auditoría
```

## API y prioridad causal

`ReplayRunner(config).run(events, state=None)` consume un iterable **ya ordenado**.
Admite Candle, Quote, Order y envoltorios frozen EntryControlEvent(at, allow_entries)
y FillEvent(fill, order). FillEvent exige Order asociada explícita; no hace poll ni
crea identidades del proveedor. Position/Round externos no se sintetizan: se conserva
el estado financiero explícito de Alt B, separado de un futuro libro de cuenta.
El permiso inicial es False; RESUME exige control explícito. PAUSE conserva rondas.

La clave causal es `(timestamp UTC, prioridad)`:

| Prioridad | Evento | Timestamp de disponibilidad |
| ---: | --- | --- |
| 0 | EntryControlEvent | at |
| 1 | Order | updated_at |
| 2 | FillEvent | max(fill.timestamp, order.updated_at) |
| 3 | Candle cerrada 1m | close_time exclusivo |
| 4 | Quote | timestamp |

Un Fill anterior puede conciliarse con una Order posterior, pero el par solo está
disponible en el máximo de ambos tiempos. No se entrega Order futura en el instante
anterior del Fill. Candle está disponible al cierre. Eventos de igual clave conservan
el orden del fixture. Retrocesos de tiempo/prioridad son errores, también al reanudar.
Las fronteras pueden dividir eventos simultáneos si mantienen la clave o una mayor.
No se admite un control después de Quote ya procesado del mismo instante.

Esta prioridad define entrega de hechos, **no** trayectoria intraminuto ni inferencia
DCA-antes-de-TP dentro de OHLC. Inputs anticipados para mezclar streams no se entregan
antes de su clave. No hay reloj real, UUID aleatorios ni lookahead. ReplayState conserva
last_key y through; through puede avanzar al fin explícito de un rango sin eventos,
sin alterar ni falsificar AltBStrategyState.last_event_at.

## ScheduledAction y Quote

La señal en cierre T conserva scheduling canónico a T+120s. Las acciones permanecen
en CoordinatorState.pending_actions mientras no exista Quote explícito admisible.
ReplayState.due_actions deriva las vencidas desde through sin retirarlas/evaluarlas.
No hay otra cola que pueda perder acciones extraídas. PAUSE y gaps eliminan la reserva
mediante la coordinación existente.

Al recibir Quote se procesa on_quote, luego advance_time(state, quote.timestamp) y
on_scheduled_action para cada acción due con ese mismo Quote, conservando cada estado.
El coordinador/motor comprueban símbolo y not_before. No se guarda Quote previo para
reutilizarlo al vencer; Candle.close no equivale a bid/ask.
Quote en T+119s no consume la acción. Sin Quote en T+120s sigue pendiente. Si el primero
admisible llega en T+180s, se evalúa allí y la intención usa **T+180s**, sin backdating.
No se inventa frescura o vencimiento adicional. La política histórica queda en 3C-2.

## Fuente SQLite y gaps

`run_sqlite(path, start, end, events=(), state=None)` abre FuturesSQLite(read_only=True)
y usa read(), sin importaciones, esquema nuevo, manifiestos o escrituras de mercado.
Una ruta inexistente falla sin crear SQLite. Solo 1000SHIBUSDC, BINANCE_USDM_FUTURES
y minutos normalizados; no Spot ni conversiones. Decimal y taker-buy se mantienen exactos.

Las aperturas pertenecen a [start,end); el cierre máximo es end. Límites UTC alineados
al minuto. Eventos adicionales pueden ocurrir entre start y end inclusive; en este método
las Candle solo provienen de SQLite. El llamador distribuye eventos de frontera una vez,
respetando prioridad. Segmentos SQLite deben ser contiguos; no anteriores al cursor.

Lectura streaming mediante cursor, con un input anticipado como máximo por stream.
No se materializan años: Alt B conserva su ventana de 1560 minutos. Cursores/generadores
se cierran antes de SQLite incluso ante error. Se retienen outputs/evidencia solicitados,
no todas las Candle; no se promete memoria constante para años de rondas archivadas.

La auditoría registra gaps iniciales, internos y finales, incluido rango vacío. Intervalos
adyacentes se unen entre segmentos sin doble conteo. Al llegar Candle posterior al gap,
ingest_closed_1m aplica su reset aprobado, invalida espera y exige warmup nuevo. No fabrica
minutos/señales. Un gap final sin otra Candle queda como ausencia registrada: no inventa
evento de reset ni elimina evidencia financiera.

## Ejecución y cierre

Order/Fill solo ingresan como hechos explícitos de fixtures. Los mismos handlers de
3B-2C-2 concilian intención, OrderId, FillId y cantidad. Duplicados idénticos no reemiten
outputs. Conflictos se propagan o conservan el bloqueo aprobado. No se recortan fills ni
se usa Order.filled_quantity como sustituto de Fill.

OrderCancelRequest se conserva en outputs/estado. Solicitar no confirma terminalidad,
cambia Order.status ni genera TP de reemplazo. La transición espera hechos terminales
y fills conciliados; garantizar total final corresponde a futura ejecución.
OHLC nunca produce Fill, precio ejecutado, spread o cancelación exitosa.
Fin de rango no fuerza cierre/cancelación: Base/ronda incompleta permanece pendiente.
Solo Alt B puede archivar un cierre conciliado.

## Reanudación y auditoría

ReplayState frozen conserva config, CoordinatorState completo, cursor, fin SQLite,
ReplayOutputs acumulados y ReplayAudit. Reanudación exige la misma config; no se
reconstruye estrategia desde solo Candle ni se ejecutan señales antiguas tras reset.
No hay serialización ni persistencia operativa.

```python
runner = ReplayRunner()
prefix = runner.run(events_prefix)
complete = runner.run(events_suffix, state=prefix)
# Igualdad exacta con runner.run(events_full), para iguales inputs y fronteras.
```

Igualdad cubre Signals, ScheduledActions, acciones evaluadas con Quote, OrderIntents,
CancelRequests, Order/Fill observados, IDs, timestamps, auditoría, snapshot final e
historial. No se promete equivalencia de tapes diferentes ni se elimina causalidad.

ReplayAudit conserva rango, contador de Candle únicas/checkpoints, gaps, aperturas de
ronda y transiciones de bloqueo; sin un log por cada minuto de warmup. Snapshots repetidos
no se cuentan nuevamente; distintos informes de una misma Order se preservan.
ReplayOutputs conserva las decisiones y evidencia completa, incluidas cancelaciones.
report() devuelve un mapping de dominio con objetos frozen, no JSON serializado:

- **OBSERVADO**: Candle, Order y Fill suministrados; tests son fixtures sintéticos,
  sin afirmar conectividad o ejecuciones reales.
- **SIMULADO**: señales, scheduling/evaluación, intenciones Base/DCA/TP, solicitudes,
  aperturas/cierres conciliados de dominio; no órdenes enviadas ni resultado económico.
- **PENDIENTE**: acciones programadas/due sin Quote, cancelaciones sin confirmación,
  Base y ronda activa conservadas.
- **NO VERIFICABLE**: Quotes históricos, modelo de ejecución, rentabilidad, fees y funding.

El informe incluye símbolo, mercado, rango, gaps, checkpoints, bloqueos y estado final.
No se calcula PnL ni se reporta rentabilidad válida.

## Decisiones pendientes 3C-2 y límites

Pendientes: fuente/política histórica de bid/ask, ejecución económica, latencia/finalidad
de cancelaciones, orden intraminuto verificable, fills parciales, slippage, fees y funding.
No se aprueba aquí ningún valor/modelo nuevo. Add Margin, liquidación, backtest financiero
2024–2026, Binance privado, LIVE, Telegram, persistencia financiera, servicios Ubuntu y
despliegue están fuera. MarketDataProvider/ExecutionAdapter permanecen intactos;
sin submit/poll, adaptador operativo ni activación de trading. Runtime STOPPED.
