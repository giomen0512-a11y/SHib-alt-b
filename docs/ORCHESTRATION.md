# Coordinación por eventos y acciones programadas — Fase 3B-2B

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
CoordinatorState añade strategy_state opcional, un snapshot frozen definido por
la estrategia cuyo contenido el coordinador no interpreta. Lo pasa como keyword
a cada handler y conserva el snapshot nuevo de StrategyResult; None conserva
el anterior. El snapshot puede validar su cola mediante validate_pending_actions,
sin dependencias Alt B en orchestration.

StrategyResult añade cancelled_action_ids para retirar acciones antes de añadir
las nuevas. IDs ausentes pueden cancelarse idempotentemente. Un resultado no
puede cancelar y programar el mismo ID; no se deduplican outputs inválidos.

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
| `on_entry_control` | bool allow_entries y at UTC | `at` |
| `invalidate_action` | ActionId y at UTC → on_action_invalidated | `at` |

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

Las due desaparecen de la cola retornada; strategy_state se conserva sin cambios.
Extraerlas no llama al motor ni las
ejecuta. El runner debe conservar ese estado, obtener un Quote actual y entregar
la acción elegible mediante `on_scheduled_action`. El handler no vuelve a extraer
acciones ni usa un reloj implícito: el runner es responsable de ese flujo y del
contexto temporal del Quote, y el coordinador aplica además la barrera de
elegibilidad descrita arriba. No se define aún una política de frescura de mercado.

Una señal en T puede producir una ScheduledAction sin Quantity para T + delay.
La reevaluación con el Quote futuro puede producir una OrderIntent con Quantity
concreta en una implementación posterior. El coordinador no fija delays.
AltBEngine añade +120s a sus acciones en 3B-2B, sin ejecutar Base:
[ALT_B_SIGNALS.md](ALT_B_SIGNALS.md).

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
registro genérico de ActionId ya retirados, watermark propio del coordinador,
reintentos, persistencia o política
de ejecución. El llamador gestiona la secuencia y conserva el estado.

Riesgo y ejecución siguen siendo pasos posteriores separados. AltBEngine integra
señales y scheduling con estado explícito, sin ejecución ni modos operativos;
runtime sigue STOPPED. Su snapshot conserva reserva y watermark para impedir
duplicados, cancelar entradas y bloquear la ejecución pendiente de 3B-2C.

## Política de identidad — Fase 2.2

ScheduledAction, Signal, OrderIntent y Round requieren IDs explícitos al
construirse; StrategyCoordinator y los modelos no los generan. AltBEngine
selecciona componentes explícitos para señales/rondas/acciones y usa las funciones
puras UUIDv5 de models/identity.py, con namespace fijo
`22842300-09df-530e-93ea-1e985518f6d1`, schema v1 y JSON canónico. Mismo hecho
y mismo estado lógico deben reconstruir los mismos IDs en Replay, Shadow, Live
y recovery. Reprocesar o reintentar no incrementa revision. OrderId/FillId quedan
fuera de esta capa. Consulta [IDENTITY.md](IDENTITY.md); no se implementan
persistencia ni generación aleatoria. Los componentes Alt B se detallan en
ALT_B_SIGNALS.md y IDENTITY.md; los helpers genéricos no cambian.
