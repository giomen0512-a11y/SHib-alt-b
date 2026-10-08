# Arquitectura mínima — Fase 2.2

Un único núcleo de decisión, con dependencias externas detrás de adaptadores:

```text
Candle / Quote / Order / Fill + Order / ScheduledAction + Quote
    ↓
StrategyEngine (único núcleo compartido)
    ↓
StrategyResult (signals + scheduled_actions + order_intents)
    ↓
StrategyCoordinator
    ├─ signals → llamador
    ├─ order_intents → llamador → RiskManager → ExecutionAdapter (futuro)
    └─ scheduled_actions → state.pending_actions
                                ↓ advance_time(state, now)
                           DueActionsResult
                                ↓ runner obtiene Quote actual
                      coordinator.on_scheduled_action
                                ↓ StrategyEngine.on_scheduled_action
                           StrategyResult
```

Este flujo define límites mediante Protocols y coordinación pura por eventos.
RiskManager → ExecutionAdapter es un paso operativo futuro; el coordinador no
invoca esas capas. Una ScheduledAction programa una reevaluación sin Quantity;
una OrderIntent ya es concreta y está lista para procesamiento inmediato sujeto
al riesgo y la ejecución. Quote es un snapshot bid/ask, no un Fill ni una garantía
de precio ejecutado. El precio efectivo y el futuro P0 proceden de fills reales.
Los futuros adaptadores serán `ReplayExecutionAdapter`, `ShadowExecutionAdapter`
y `BinanceLiveExecutionAdapter`. Los contratos actuales son:

- `market_data/contracts.py`: `MarketDataProvider.candles(symbol)` expone un
  iterador de Candle; no conoce la estrategia ni fija intervalos/orígenes.
- `strategy/contracts.py`: `StrategyEngine` soporta `on_candle`, `on_quote`,
  `on_order`, `on_fill` y `on_scheduled_action`. Todos reciben snapshots de
  dominio y retornan StrategyResult inmutable con `signals: tuple[Signal, ...]`,
  `scheduled_actions: tuple[ScheduledAction, ...]` y
  `order_intents: tuple[OrderIntent, ...]`, vacías por defecto e independientes.
  `on_fill` recibe Fill y Order; `on_scheduled_action` recibe acción y Quote.
  El motor sigue siendo puro: no recibe adaptadores, riesgo, bases de datos,
  Binance, Telegram ni secretos. No implementa condiciones de entrada.
- `risk/contracts.py`: `RiskManager.evaluate(intent, balance, position)` retorna
  RiskDecision con aprobación explícita o rechazo con motivo, sin reglas de riesgo.
- `execution/contracts.py`: `ExecutionAdapter.submit(intent)` retorna un snapshot
  Order; `poll()` retorna una tupla finita de los Order/Fill actualmente disponibles
  sin bloquear. Aceptar una intención no presupone su llenado inmediato.

La dirección es implementaciones externas → contratos → modelos. Los modelos
solo dependen de biblioteca estándar y utilidades centrales; los contratos solo
de biblioteca estándar y modelos. `orchestration` depende de modelos y del contrato
StrategyEngine: incorpora acciones pendientes, expone señales e intenciones y
determina acciones due con tiempo inyectado. No usa reloj interno ni fija delays. Consulta
[coordinación temporal](ORCHESTRATION.md) para el estado y sus resultados.
Consulta [modelos de dominio](DOMAIN_MODEL.md) para sus invariantes.

El coordinador exige Quote.timestamp >= ScheduledAction.not_before antes de
evaluar una acción. También rechaza ScheduledAction/OrderIntent nuevos con
created_at anterior al evento causal; los tiempos de cada handler y el tratamiento
de fills observados fuera de orden se detallan en ORCHESTRATION.md.

Fase 2.2 define la política de identidad en `models/identity.py`: funciones puras
UUIDv5 con namespace fijo `22842300-09df-530e-93ea-1e985518f6d1`, schema v1 y
componentes explícitos codificados como JSON canónico. La futura estrategia
concreta elegirá los componentes y derivará SignalId, RoundId, ActionId e IntentId.
StrategyCoordinator y los modelos no generan IDs. OrderId/FillId corresponden a
la futura capa de ejecución/adaptador. Consulta [IDENTITY.md](IDENTITY.md) para
encoding, revision e idempotencia; no hay generación aleatoria ni persistencia.

Replay ≠ estrategia independiente.
Shadow ≠ estrategia independiente.
Live ≠ estrategia independiente.
Los tres compartirán **el mismo StrategyEngine** y la misma política de Add Margin.

OrderIntent incluye `reduce_only`, un bool real independiente del exchange.
False permite aumentar o crear exposición; True expresa que la ejecución nunca
debe aumentar exposición ni crear una posición contraria. Es la misma semántica
para Replay, Shadow y Live; no se implementa su enforcement en esta fase.

## Responsabilidades y dependencias

- `models`: valores, enums, IDs y snapshots inmutables sin dependencias de proveedores.
- `config`: configuración inmutable, leída del entorno y validada explícitamente.
- `market_data`: futura provisión de datos; no toma decisiones ni ejecuta órdenes.
- `strategy` y `signals`: futuro núcleo de decisión y sus señales; sin llamadas
  a Binance, Telegram, persistencia o adaptadores específicos de un modo.
- `risk`: futura validación de intenciones antes de ejecutar.
- `execution`: contrato mínimo de ejecución. Los futuros adaptadores de modo cumplirán
  esos contratos y no contendrán una estrategia duplicada.
- `orchestration`: coordinación y scheduling deterministas compartidos; sin
  ejecución, llamadas a riesgo ni efectos externos.
- `replay`, `shadow`, `live`: futura coordinación de cada modo y sus adaptadores.
- `add_margin`: futura política compartida, pendiente de especificación.
- `persistence`: almacenamiento separado de las reglas de decisión.
- `binance`, `telegram`: futuras interfaces externas, sin SDK ni acceso en Fase 0.
- `utils`: herramientas generales sin reglas financieras.

## Invariantes

Python 3.12. Precios, cantidades, dinero, PnL, margen y fees utilizarán `Decimal`;
no `float` en cálculos financieros críticos. `to_decimal` admite texto, enteros
y Decimal finitos; rechaza float/bool y no inventa precisión, redondeo o tamaños
de lote. Las políticas de cálculo se especificarán y probarán en su fase.

Timestamps internos UTC timezone-aware. `require_utc` rechaza fechas naive y
offsets no UTC; la normalización de fuentes externas corresponderá a sus adaptadores.

Configuración separada del código y secretos fuera del repositorio. La carga
rechaza modos no implementados o desconocidos en lugar de aceptar una operativa
incompleta. `OperatingMode` enumera estados futuros sin definir transiciones.
También en Fase 2.2 solo `STOPPED` es válido. No existe un interruptor que habilite Live.

El comando `python -m trading_engine` solo valida configuración y registra un
evento JSON con timestamp UTC, nivel, evento y modo. No registra el entorno
ni secretos. Los errores se propagan; no se sustituyen por resultados ficticios.

Tests localizados, cambios pequeños y dependencias justificadas. Los paquetes de
implementaciones futuras siguen reservados; no hay lógica de trading, DCA, TP,
filtros, vetos, replay funcional, Shadow, Live, Add Margin ni Telegram.
