# Coordinación por eventos y acciones programadas — Fase 2.1

```text
Candle / Quote / Order / Fill + Order / ScheduledAction + Quote
      ↓
StrategyEngine
      ↓
StrategyResult
      ↓
StrategyCoordinator
      ├─ signals → llamador
      ├─ order_intents → llamador (listas para riesgo/ejecución futura)
      └─ scheduled_actions → state.pending_actions
                                  ↓ advance_time(state, now)
                             DueActionsResult
                                  ↓ runner obtiene Quote actual
                         coordinator.on_scheduled_action
                                  ↓ StrategyEngine.on_scheduled_action
                             StrategyResult
```

Una única coordinación compartida por Replay, Shadow y Live. No hay reloj interno:
los eventos y sus tiempos explícitos se entregan al coordinador. Este no calcula
retrasos específicos de estrategia ni usa sleeps, timers, threads o asyncio.
No llama RiskManager ni ExecutionAdapter, no persiste ni consulta servicios.

## ScheduledAction y OrderIntent

**ScheduledAction != OrderIntent**. La acción representa «reevaluar cuando llegue
un instante futuro», sin Quantity ni orden completa. Tiene ActionId, RoundId,
Symbol, kind genérico no vacío y tiempos explícitos UTC aware:
`not_before >= created_at`; igualdad permite elegibilidad inmediata.

OrderIntent representa una orden suficientemente concreta para procesamiento
inmediato sujeto al riesgo y ejecución. Conserva created_at, Quantity y sus
invariantes; no contiene not_before. Order.created_at debe ser >=
OrderIntent.created_at y <= Order.updated_at. No existe compatibilidad artificial
con el scheduling anterior de intenciones.

## Estado y resultados

`CoordinatorState.pending_actions` es una tupla frozen de ScheduledAction.
Valida tipos, rechaza ActionId duplicados y normaliza el orden canónico
`(not_before, action_id.int)`, con el valor numérico del UUID para desempatar.
El orden de inserción no cambia el resultado. No deduplica silenciosamente.

`CoordinationResult` contiene únicamente `signals`, `order_intents` y `state`.
Las señales e intenciones mantienen el orden recibido. Las scheduled_actions
producidas por el motor se incorporan a state.pending_actions; las order_intents
se exponen al llamador y no se guardan como acciones pendientes.

`DueActionsResult` reemplaza a DueIntentsResult y contiene únicamente `due_actions`
y `state`. Las acciones due también son canónicas y sin duplicados. Todos los
resultados son frozen y validan contenedores tuple, sus elementos y el estado.
No hay historial de señales, rondas, órdenes, balances o fills en el estado.

## Handlers

Todos reciben `state` primero y snapshots opcionales `position=None` y
`round_state=None` al final:

| Handler del coordinador | Evento entregado al método homónimo de StrategyEngine | `event_time` causal mínimo |
| --- | --- | --- |
| `on_candle` | Candle | `candle.close_time` |
| `on_quote` | Quote | `quote.timestamp` |
| `on_order` | Order | `order.updated_at` |
| `on_fill` | Fill y Order | `fill.timestamp` |
| `on_scheduled_action` | ScheduledAction y Quote | `quote.timestamp` |

Cada handler valida inputs antes de llamar exactamente una vez al método del
motor, valida StrategyResult, integra scheduled_actions y retorna señales,
intenciones y un nuevo estado. Un Fill debe pertenecer al Order entregado
(`fill.order_id == order.order_id`); el Quote de una acción debe corresponder a
su Symbol. Los errores de estrategia y los duplicados se propagan, sin ocultarlos.
No se crean rondas, órdenes, IDs ni fills automáticamente.

## Barreras de causalidad

Antes de invocar StrategyEngine, `on_scheduled_action` exige
`quote.timestamp >= action.not_before`. Un Quote anterior produce ValueError
sin llamar al motor; igualdad y tiempos posteriores son válidos. Esta barrera
no depende de que el runner haya llamado correctamente a advance_time.

Después de recibir StrategyResult, el helper de integración exige para cada
ScheduledAction y OrderIntent nuevos `created_at >= event_time`, según la tabla.
Igualdad es válida. Un output anterior al evento causal produce ValueError
antes de crear el nuevo CoordinatorState. No se corrigen timestamps ni se
ocultan errores; el estado previo permanece intacto. Las acciones preexistentes
del estado no se vuelven a validar contra el nuevo evento causal.

En `on_fill` se mantiene `fill.order_id == order.order_id`, pero no se exige
`fill.timestamp >= order.created_at`: los hechos pueden observarse o reconciliarse
fuera de orden. Sus nuevos outputs sí se validan desde `fill.timestamp`.

## Elegibilidad y evaluación explícita

`advance_time(state, now)` exige datetime UTC aware y separa:

- `action.not_before <= now`: due_actions.
- `action.not_before > now`: pendientes del nuevo estado.

Las due desaparecen del estado retornado. Extraerlas no llama al motor ni las
ejecuta. El runner debe conservar ese estado, obtener un Quote actual y entregar
la acción elegible mediante `on_scheduled_action`. El handler no vuelve a extraer
acciones ni usa un reloj implícito: el runner es responsable de ese flujo y del
contexto temporal del Quote, y el coordinador aplica además la barrera de
elegibilidad descrita arriba. No se define aún una política de frescura de mercado.

Una señal en T puede producir una ScheduledAction sin Quantity para T + delay.
La reevaluación con el Quote futuro puede producir una OrderIntent con Quantity
concreta. No hay un delay financiero hardcoded ni OPEN_BASE real en esta fase.

## Quote, Fill y causalidad de precios

**Quote != Fill**. Quote es un snapshot básico de bid/ask válido para sizing o
decisiones; no garantiza el precio ejecutado ni modifica una posición u orden.
El precio real ejecutado pertenece al ExecutionAdapter o al futuro modelo de
ejecución Replay. P0 seguirá dependiendo de fills reales, nunca del Quote.

La arquitectura permite posteriormente:

- ExecutionAdapter → Fill + Order → coordinator.on_fill → StrategyEngine.on_fill
  → StrategyResult. Los futuros cálculos de P0, DCA y TP usarán esos fills.
- Quote → coordinator.on_quote → StrategyEngine.on_quote → StrategyResult,
  para futuras decisiones durante una ronda sin esperar un cierre 15m.

Esos cálculos y triggers no se implementan aquí. El motor recibe exclusivamente
snapshots de dominio; no Binance, database, Telegram, adaptadores, riesgo o secretos.

## Garantías y límites

Mismos eventos, IDs, timestamps, estado y resultados de estrategia producen los
mismos outputs y estado final. Las operaciones no mutan el estado previo.
Avanzar nuevamente el estado retornado no reemite las due retiradas. No hay
registro de ActionId ya retirados, watermark, reintentos, persistencia o política
de ejecución. El llamador gestiona la secuencia y conserva el estado.

Riesgo y ejecución siguen siendo pasos posteriores separados. No se implementan
estrategia financiera, Alternativa B ni modos operativos; runtime sigue STOPPED.

## Política de IDs pendiente

ScheduledAction, Signal, OrderIntent y Round requieren IDs explícitos al
construirse; StrategyCoordinator no los genera. Fase 2.1 todavía no define cómo
una estrategia concreta asignará IDs a sus nuevos outputs. Antes de implementar
Alternativa B, Fase 3B deberá definir una política determinista e idempotente
compatible con Replay, Shadow, Live y recovery. No se permitirá generación
aleatoria implícita. Esta corrección documenta el pendiente sin diseñar ni
implementar un generador.
