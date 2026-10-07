# Modelos de dominio — Fase 1

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
| IDs | RoundId, SignalId, IntentId, OrderId y FillId son NewType sobre UUID. |

Un float, bool, int o string no es un Decimal válido en campos financieros.
El llamador puede convertir texto/enteros mediante `to_decimal` antes de construir.
Money no define operaciones aritméticas ni conversión FX; no mezcla monedas
automáticamente. No se introduce Percentage/Ratio sin un uso especificado.

Los IDs son nominalmente distintos para mypy. En runtime son UUID, no wrappers
distintos; los modelos rechazan strings arbitrarios. El llamador asigna IDs y
garantiza unicidad. No hay generación automática: permite inyectar IDs deterministas
sin acoplar el dominio a un reloj o fuente aleatoria.

Side: BUY/SELL. PositionSide: LONG/SHORT (cantidad siempre positiva, sin Hedge Mode).
OrderType: MARKET/LIMIT. OrderStatus: PENDING, OPEN, PARTIALLY_FILLED, FILLED,
CANCELED, REJECTED. RoundStatus: PENDING_ENTRY, OPEN, CLOSING, CLOSED, FAILED.
Los constructores requieren enums reales; no aceptan strings como estados.

## Datos y snapshots

- **Candle**: símbolo, apertura/cierre temporal, OHLC Price y volumen Decimal >= 0.
  `open_time < close_time`; high contiene open, close y low; low no supera
  open, close ni high. No fija duración ni intervalo.
- **Signal**: SignalId, símbolo, timestamp y `kind` textual no vacío. No define
  condiciones de entrada, parámetros de Alternativa B ni metadata arbitraria.
- **StrategyResult**: resultado inmutable con únicamente `signals: tuple[Signal, ...]`
  y `order_intents: tuple[OrderIntent, ...]`, ambas vacías por defecto e independientes.
  Rechaza contenedores mutables y elementos de tipos distintos de Signal/OrderIntent.
  StrategyEngine → StrategyResult → signals + order_intents. La coordinación podrá
  registrar/notificar señales y someter intenciones a RiskManager. No implementa
  generación de señales, coordinación ni temporización; todos los modos compartirán
  el mismo StrategyEngine.
- **OrderIntent**: petición del motor con IntentId, RoundId, símbolo, Side,
  OrderType, Quantity y timestamp. Contrato basado únicamente en cantidad:
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
normalizarán a UTC antes de construir. Order requiere intent.timestamp <=
created_at <= updated_at. filled_quantity nunca supera la solicitada.
PENDING/OPEN/REJECTED requieren cero; PARTIALLY_FILLED exige 0 < filled < requested;
FILLED exige igualdad; CANCELED permite cero o parcial, nunca cantidad completa.
Son invariantes de snapshots, no una máquina de transiciones ni un reconciliador.

Round requiere created_at <= opened_at/closed_at cuando existen y opened_at <=
closed_at cuando ambos existen. PENDING_ENTRY no admite apertura/cierre;
OPEN/CLOSING requiere apertura sin cierre; CLOSED requiere ambos.
FAILED permite fallo antes o después de apertura y cierre opcional, respetando
siempre el orden temporal. No se validan transiciones entre snapshots.

## Límites

OrderIntent expresa lo deseado, Order lo reportado por un adaptador y Fill una
ejecución individual. Round identifica el ciclo; Position describe exposición,
sin deducir que un snapshot equivale al estado de una ronda completa.

Implementaciones externas → Protocols centrales → modelos de dominio.
Ni modelos ni contratos importan Binance, Replay, Shadow, Live, Telegram o
persistencia. Los fakes de tests solo devuelven datos prefijados para probar
compatibilidad estructural, ausencia de intención, rechazo y eventos parciales.

No existe MarginAdjustmentIntent: ningún contrato actual lo necesita. No hay
política Add Margin, PnL, liquidación, leverage, funding, fees ni señales reales.
