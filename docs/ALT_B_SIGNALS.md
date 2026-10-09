# Señales y programación causal — Fases 3B-2B a 3B-2C-2

`AltBEngine` implementa el único contrato StrategyEngine por eventos, compartido
por los futuros Replay, Shadow y Live. Es un dataclass frozen con **solo config**;
no conserva estado entre llamadas, consulta reloj ni hace I/O. Runtime permanece
STOPPED. No se conecta el colector, SQLite, Binance privado, Telegram ni systemd.
La integración reside en `strategy/alt_b/events`, conservando intactos los
módulos y límites de dependencia de matemáticas, evaluación e ingesta aprobados.

```text
CoordinatorState(strategy_state=AltBStrategyState, pending_actions)
     + Candle cerrada / Quote / control / invalidación
                    ↓
             StrategyCoordinator
                    ↓ estado explícito
                AltBEngine
                    ↓
      ingest_closed_1m → evaluación canónica de checkpoint
                    ↓
       StrategyResult(state + Signal + ScheduledAction)
                    ↓
        nuevo CoordinatorState → conservar por el llamador
```

## Extensión mínima de los contratos

Antes de esta fase los métodos del motor no recibían estado ni lo devolvían.
Guardar AltBMarketState dentro de un motor mutable habría introducido estado
oculto; poner reglas Alt B en el coordinador habría mezclado responsabilidades.
Se añade `StrategyState`, base frozen de snapshots cuyo contenido pertenece a
la estrategia. No contiene metadata arbitraria. Todos los handlers reciben
`strategy_state` keyword opcional; StrategyResult devuelve `strategy_state` y
`cancelled_action_ids`, con los tres outputs anteriores sin modificar.

CoordinatorState conserva el snapshot opaco. El coordinador lo pasa al motor,
incorpora el nuevo snapshot y retira IDs cancelados antes de añadir acciones.
None en un resultado conserva el snapshot anterior, permitiendo resultados
vacíos de estrategias sin estado. El motor Alt B siempre devuelve su snapshot.
Los fakes existentes se adaptan solo al contrato; sus aserciones se conservan.

`StrategyState.validate_pending_actions` permite que el snapshot valide su cola
sin que el coordinador importe Alt B. AltBStrategyState exige que cada acción
pendiente corresponda exactamente a su entrada lógica. Una cola vacía es válida
tras extraer due; la reserva lógica permanece. Una reconstrucción incompatible
produce ValueError, bloqueando el procesamiento en lugar de generar otra señal.

Se añaden dos eventos explícitos, sin Telegram ni cambios de OperatingMode:
`coordinator.on_entry_control(state, allow_entries, at)` y
`coordinator.invalidate_action(state, action_id, at)`. El permiso es un bool real,
False por defecto. Estos eventos no habilitan LIVE ni ejecutan el runtime.

## Snapshot y consumo de eventos

AltBStrategyState es frozen y contiene únicamente:

- `market`: AltBMarketState de hasta 1560 Candle continuas.
- `allow_entries`: permiso explícito para nuevas señales.
- `pending_entry`: Signal y ScheduledAction propias, más evaluated_at opcional.
- `last_event_at`: watermark causal UTC de eventos observados.
- `block_reason`: bloqueo explícito.
- `base`: reserva consumida y evidencia de Order/Fill incorporada en 3B-2C-1;
  ver [ALT_B_BASE_FILLS.md](ALT_B_BASE_FILLS.md).

La PendingEntry valida los IDs UUIDv5, símbolo, kinds, checkpoint 15m y el delay
exacto. No es una Round financiera, posición, orden, Quantity, notional ni P0.
La ronda tiene identidad determinista; no se fabrica una Round abierta.

Cada evento retorna snapshots nuevos. El llamador debe conservar **el state
retornado**, incluyendo market, control, reserva y cola; conservar solo las
velas pierde información lógica. No se implementa persistencia ni serialización.
Conservar/restaurar snapshots íntegros permite reanudar determinísticamente una
secuencia offline. Esto no autoriza ejecutar una entrada antigua tras reinicio
operativo: la política segura de recuperación y reconciliación queda pendiente
para 3B-2C; antes de reanudar operativamente se debe invalidar la reserva antigua.

Candle sigue siendo un snapshot de dominio sin campo de exchange/is_closed.
El proveedor debe entregar únicamente minutos Futures realmente cerrados y
normalizados; el motor no certifica procedencia Binance ni consulta el reloj.
`ingest_closed_1m` exige símbolo configurado y minutos UTC consecutivos exactos.
Cada cierre 00/15/30/45 evalúa el historial causal aprobado; ningún otro cierre
evalúa entrada. Exige 1560 minutos y las referencias válidas del evaluador.
No cambian fórmulas, umbrales, taker-buy real ni parámetros canónicos.

Un duplicado idéntico de la última vela no vuelve a evaluar ni emitir; el mismo
intervalo con contenido diferente o una vela anterior fallan. Un gap reinicia
warmup con la nueva vela y cancela una entrada pendiente: su continuidad ya no
es verificable. No se fabrica ni completa ningún minuto. El watermark impide
evaluar un checkpoint anterior usando autorización/contexto de eventos futuros.
Un retry idéntico de la última vela puede llegar después de otros eventos: no
emite nada ni retrocede el watermark. Pausa/resume no reactivan señales antiguas.

## Señal y seguridad

Una señal requiere evaluación válida, tres filtros PASS, ausencia de vetos,
permiso operativo, Position=None, ninguna Round activa y ninguna PendingEntry.
Snapshots de posición, ronda incompatible, símbolo ajeno o tiempos de ronda
futuros bloquean. CLOSED/FAILED no son rondas activas cuando su contexto temporal
es verificable y no existe posición; no se infiere exposición de una Round.

Por señal se produce exactamente una acción, sin OrderIntent:

| Identidad | Componentes (revision=0) |
| --- | --- |
| SignalId | strategy_id canónico, símbolo, checkpoint T, `ALT_B_LONG_ENTRY` |
| RoundId | SignalId |
| ActionId | RoundId, `ALT_B_BASE_ENTRY`, created_at=T, not_before=T+120s |

Usa exclusivamente los helpers UUIDv5 existentes, sin cambiar namespace/schema.
Una reejecución con el mismo snapshot y evento reconstruye esos mismos IDs.
Consumir el snapshot retornado evita una segunda emisión del mismo checkpoint;
repetir deliberadamente desde un snapshot anterior reproduce el mismo output,
no proporciona deduplicación de efectos externos. Registrar/notificar señales
idempotentemente sigue siendo responsabilidad de una capa externa futura.

Pausa o contexto incompatible cancela la reserva y devuelve su ActionId para
retirarlo de la cola. La invalidación explícita también la cancela. Una acción
entregada sin reserva válida o con identidad/campos distintos invalida el intento;
nunca genera una intención. Inputs/estado estructuralmente inválidos producen
errores explícitos y ningún output ejecutable.

## Frontera de 120 segundos e intención Base — 3B-2C-1

`advance_time` conserva strategy_state y solo extrae acciones elegibles. El runner
entrega la acción extraída y Quote al handler. El guard existente del coordinador
rechaza Quote.timestamp < not_before; el motor aplica la misma barrera cuando se
llama directamente. La igualdad exacta es elegible. No se usan sleeps ni reloj.

No se recalculan filtros durante la espera ni con el Quote. Fase 3B-2C-1 consume
una acción válida autorizada y emite una única intención BUY MARKET teórica.
Se retira la acción y se conserva la reserva como BaseState. Un retry desde el
estado consumido no reemite. Quote.ask sirve solo para sizing; no es Fill ni P0.
Order y Fill explícitos se procesan como hechos independientes, con conciliación
de cantidades y preservación de evidencia tardía. La política y sus límites se
detallan en [ALT_B_BASE_FILLS.md](ALT_B_BASE_FILLS.md). No hay ejecución real.

## Uso offline

```python
from trading_engine.orchestration.coordinator import StrategyCoordinator
from trading_engine.orchestration.models import CoordinatorState
from trading_engine.strategy.alt_b.events.engine import AltBEngine

coordinator = StrategyCoordinator(AltBEngine())
state = CoordinatorState()
# start_at UTC explícito, anterior a los eventos cerrados del historial.
state = coordinator.on_entry_control(state, True, start_at).state
for candle in closed_futures_minutes:
    result = coordinator.on_candle(state, candle)
    state = result.state
    # result.signals se entregan al llamador; no hay envío de notificaciones.
due = coordinator.advance_time(state, now_utc)
state = due.state
for action in due.due_actions:
    result = coordinator.on_scheduled_action(state, action, current_quote)
    state = result.state  # Conservar reserva consumida; outputs teóricos, sin envío.
```

Los nombres de historial/tiempo/Quote del ejemplo son inputs explícitos del
llamador, no adaptadores implementados. Se prueba con datos sintéticos offline,
sin descargar datos ni conectar el colector. No hay runner operativo nuevo.

## Ronda posterior — Fase 3B-2C-2

El estado conserva gestión DCA/TP e historial financiero después de Base. PAUSE,
gaps y vetos no borran una ronda abierta. Las señales nuevas permanecen bloqueadas
hasta un cierre probado; después se conserva la evidencia histórica. Ver
[ALT_B_DCA_TP.md](ALT_B_DCA_TP.md).
