# Modelos de dominio — Fase 3B-2C-1

Los valores y snapshots son dataclasses frozen y validan al construir. No añaden dependencias
externas, ejecución, almacenamiento ni lógica de estrategia. Se importan desde
los submódulos de `trading_engine.models`; `OperatingMode` conserva su import
original y sus garantías de Fase 0.

## Valores e identidad

| Concepto | Contrato |
| --- | --- |
| Symbol | Texto no vacío, sin ningún whitespace, normalizado a mayúsculas; sin catálogo de exchange. |
| Price / Quantity | Decimal finito estrictamente positivo; sin conversión implícita, cuantización ni redondeo. |
| Money | Decimal finito firmado, admite cero y negativos; currency no vacía, sin whitespace, en mayúsculas. |
| IDs | RoundId, SignalId, IntentId, OrderId, FillId y ActionId son NewType sobre UUID. |

Un float, bool, int o string no es un Decimal válido en campos financieros.
El llamador puede convertir texto/enteros mediante `to_decimal` antes de construir.
Money no define operaciones aritméticas ni conversión FX; no mezcla monedas
automáticamente. No se introduce Percentage/Ratio sin un uso especificado.

Los IDs son nominalmente distintos para mypy. En runtime son UUID, no wrappers
distintos; los modelos rechazan strings arbitrarios y exigen IDs explícitos al
construir, sin generación automática. StrategyCoordinator no genera IDs.
`models/identity.py` ofrece make_signal_id, make_round_id, make_action_id y
make_intent_id: funciones puras UUIDv5 con namespace fijo
`22842300-09df-530e-93ea-1e985518f6d1`, schema v1 y JSON canónico. La futura
estrategia concreta seleccionará componentes explícitos y usará esos helpers,
compartidos por Replay, Shadow, Live y recovery. No se añade auto-ID a los modelos.
OrderId/FillId quedan para ejecución/adaptadores posteriores. Revision no aumenta
por reprocesamiento, retry o reinicio. El encoding exacto y los vectores congelados
se especifican en [IDENTITY.md](IDENTITY.md).

Side: BUY/SELL. PositionSide: LONG/SHORT (cantidad siempre positiva, sin Hedge Mode).
OrderType: MARKET/LIMIT. OrderStatus: PENDING, OPEN, PARTIALLY_FILLED, FILLED,
CANCELED, REJECTED. RoundStatus: PENDING_ENTRY, OPEN, CLOSING, CLOSED, FAILED.
Los constructores requieren enums reales; no aceptan strings como estados.

## Datos y snapshots

- **Candle**: símbolo, apertura/cierre temporal, OHLC Price y volumen base Decimal >= 0.
  `open_time < close_time`; high contiene open, close y low; low no supera
  open, close ni high. No fija duración ni intervalo. El campo final opcional
  `taker_buy_base_volume: Decimal | None = None` conserva los constructores previos.
  Es volumen base comprado por takers: cuando está disponible exige Decimal real,
  finito y `0 <= taker_buy_base_volume <= volume`; no convierte float/int/bool/texto.
  None significa ausencia del dato, nunca cero. Ambos campos son inmutables.
- **Quote**: Symbol, timestamp UTC aware, bid y ask Price estrictamente positivos;
  exige bid <= ask y es frozen. Snapshot ejecutable básico sin profundidad,
  Mark Price, slippage ni restricciones de spread. Quote puede servir para sizing
  o decisiones, pero no es Fill ni garantiza un precio de ejecución.
- **ScheduledAction**: ActionId, RoundId, Symbol, kind textual no vacío,
  created_at y not_before UTC aware; exige not_before >= created_at.
  Frozen, con UUID suministrados por el llamador; sin generación automática,
  Quantity, metadata ni enums financieros. Expresa una reevaluación futura,
  no una orden concreta.
- **Signal**: SignalId, símbolo, timestamp y `kind` textual no vacío. No define
  condiciones de entrada, parámetros de Alternativa B ni metadata arbitraria.
- **StrategyState**: base frozen de snapshots explícitos definidos por la estrategia.
  Su método puro validate_pending_actions permite validar la coherencia de la
  cola sin incorporar reglas Alt B en el coordinador. No contiene datos de exchange.
- **StrategyResult**: resultado frozen con `signals: tuple[Signal, ...]`,
  `scheduled_actions: tuple[ScheduledAction, ...]` y
  `order_intents: tuple[OrderIntent, ...]`, todas vacías por defecto e independientes.
  Rechaza contenedores distintos de tuple y elementos de tipo incorrecto.
  StrategyEngine → StrategyResult → signals + scheduled_actions + order_intents.
  Todos los modos compartirán el mismo StrategyEngine por eventos.
  Añade `strategy_state: StrategyState | None = None` y
  `cancelled_action_ids: tuple[ActionId, ...] = ()`. Un estado None conserva el
  anterior en el coordinador. Los IDs cancelados se retiran antes de incorporar
  acciones nuevas; rechaza IDs inválidos/duplicados o programar y cancelar el
  mismo ID en un resultado. Los tres outputs originales mantienen sus defaults.
  El coordinador valida que cada ScheduledAction/OrderIntent nuevo tenga
  created_at >= event_time del handler, sin corregir timestamps. Esto no impone
  una validación retroactiva sobre las acciones ya presentes en su estado.
- **OrderIntent**: petición concreta del motor con IntentId, RoundId, símbolo,
  Side, OrderType, Quantity y created_at UTC aware. Está lista para procesamiento
  inmediato, sujeto a RiskManager y ExecutionAdapter. No contiene not_before
  ni conserva compatibilidad con ese antiguo campo. Scheduling corresponde
  exclusivamente a ScheduledAction; no hay reloj ni generación automática.
  Contrato basado únicamente en cantidad:
  no existe notional. LIMIT exige limit_price Price; MARKET lo prohíbe.
  `reduce_only` es un bool real e inmutable, False por defecto; rechaza int/string
  y otros sustitutos. False permite aumentar o crear exposición. True expresa que
  la ejecución nunca debe aumentar exposición ni crear una posición contraria.
  Es una semántica de seguridad independiente del exchange y compartida por todos
  los modos, sin enforcement ni lógica de dirección/cierre en esta fase.
- **Order**: snapshot de respuesta/seguimiento del adaptador con OrderId,
  intención original inmutable, status, filled_quantity Decimal >= 0,
  created_at/updated_at y adapter_order_id opcional no vacío. IntentId, RoundId,
  símbolo y cantidad solicitada son propiedades de esa intención, sin duplicación.
- **Fill**: hecho de ejecución independiente con FillId, OrderId, timestamp,
  Price y Quantity. No contiene fees ni modifica una Order por sí mismo.
- **Position**: snapshot de exposición actual con símbolo, PositionSide,
  Quantity, average_entry_price Price y timestamp. Una posición vacía se
  representa con `None` en los contratos, no con precio/cantidad cero.
- **Round**: identidad y estado de una ronda con RoundId, símbolo y tiempos.
  No almacena colecciones de órdenes/fills ni calcula una Position.
- **BalanceSnapshot**: wallet_balance y available_balance Money en la misma
  moneda, ambos no negativos, más timestamp. No exige available <= wallet:
  esa relación depende del proveedor y de una especificación financiera posterior.
- **RiskDecision**: aprobación booleana y motivo opcional no vacío; un rechazo
  requiere motivo. No contiene reglas ni evaluación financiera.

Todos los timestamps deben ser datetime UTC timezone-aware. Las fuentes externas
normalizarán a UTC antes de construir. Order requiere intent.created_at <=
created_at <= updated_at. filled_quantity nunca supera la solicitada.
PENDING/OPEN/REJECTED requieren cero; PARTIALLY_FILLED exige 0 < filled < requested;
FILLED exige igualdad; CANCELED permite cero o parcial, nunca cantidad completa.
Son invariantes de snapshots, no una máquina de transiciones ni un reconciliador.

Alternativa B añade restricciones temporales en `strategy/alt_b/market.py`, sin
fijarlas en Candle genérico: cada 1m representa `[open_time, close_time)` con
apertura al minuto exacto y cierre exactamente un minuto después. Un bloque 15m
requiere 15 minutos consecutivos del mismo Symbol desde 00/15/30/45 UTC;
agrega OHLC y volúmenes, conservando taker-buy=None si falta en cualquier minuto.
Las fuentes deben entregar intervalos ya normalizados; no se corrigen timestamps
del proveedor ni se completan gaps en la estrategia.

`strategy/alt_b/entry.py` define `EntryEvaluation`, independiente de StrategyResult:
checkpoint_at, data_valid, entry_allowed y reason; change_24h/range_4h/pullback_1h
con sus flags pass; v15, med_vol_4h, drop_1h, veto_a; buy_ratio_real, med_vol_24h,
rv30m, rv_ref_24h y veto_b. Es frozen, exige bool reales y métricas Decimal finitas
en sus rangos. Una evaluación válida exige todas las métricas/flags disponibles
y denominadores/referencias positivos. entry_allowed exige la conjunción de
filtros PASS sin vetos.
Datos inválidos siempre bloquean la entrada con razón reconocida; las métricas
y los flags no calculados quedan en None. No representa una señal ni una orden.
Los códigos de razón y la evaluación causal se describen en STRATEGY_ALT_B.md.

Los modelos temporales `CoordinatorState`, `CoordinationResult` y `DueActionsResult`
residen en `orchestration/models.py`. Contienen acciones pendientes/due, señales, intenciones y estado,
sin interpretar contexto financiero. CoordinatorState añade un strategy_state
opaco opcional y lo conserva incluso al extraer acciones due. Su orden canónico y uso de tiempo explícito se describen
en [ORCHESTRATION.md](ORCHESTRATION.md).

Round requiere created_at <= opened_at/closed_at cuando existen y opened_at <=
closed_at cuando ambos existen. PENDING_ENTRY no admite apertura/cierre;
OPEN/CLOSING requiere apertura sin cierre; CLOSED requiere ambos.
FAILED permite fallo antes o después de apertura y cierre opcional, respetando
siempre el orden temporal. No se validan transiciones entre snapshots.

## Límites

ScheduledAction expresa una reevaluación futura sin orden completa.
OrderIntent expresa una orden concreta, Order lo reportado por un adaptador y Fill una
ejecución individual. Quote no fija fills: el precio real pertenece al adaptador
o al futuro modelo de ejecución Replay, y P0 seguirá dependiendo de Fill.
Round identifica el ciclo; Position describe exposición,
sin deducir que un snapshot equivale al estado de una ronda completa.

Implementaciones externas → Protocols centrales → modelos de dominio.
Ni modelos ni contratos importan Binance, Replay, Shadow, Live, Telegram o
persistencia. Los fakes de tests solo devuelven datos prefijados para probar
compatibilidad estructural, ausencia de intención, rechazo y eventos parciales.

No existe MarginAdjustmentIntent: ningún contrato actual lo necesita. No hay
política Add Margin, PnL, liquidación, funding ni fees. Fase 3B-2B conecta la
evaluación aprobada con señales y acciones +120s, sin ejecución ni Quantity:
[ALT_B_SIGNALS.md](ALT_B_SIGNALS.md). La configuración conserva leverage canónico
como parámetro, sin implementar ejecución ni cálculo de liquidación.

## Base explícita de Alternativa B — Fase 3B-2C-1

AltBStrategyState añade BaseState opcional, frozen, sin cambiar contratos genéricos.
Conserva acción consumida, Quote de referencia, intención teórica BUY MARKET
reduce_only=False, Order vinculada, tuple de Fill únicos y conflicto de orden
explícito. Deriva cantidad efectiva y VWAP solo de esos fills. P0 requiere Base
resuelta y conciliada, nunca Quote o cantidad solicitada. No fabrica Position ni
Round financiera y no envía órdenes. La reserva no desaparece por PAUSE, retry,
gap o entrega tardía. Cancel/reject parcial bloquea para reconciliación.
Consultar [ALT_B_BASE_FILLS.md](ALT_B_BASE_FILLS.md) para invariantes y límites.
