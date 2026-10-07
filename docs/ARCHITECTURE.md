# Arquitectura mínima — Fase 2

Un único núcleo de decisión, con dependencias externas detrás de adaptadores:

```text
MarketDataProvider
    ↓
Candle
    ↓
StrategyEngine
    ↓
StrategyResult
    ↓
StrategyCoordinator
    ├─ signals → Signal
    └─ pending OrderIntent
                          ↓
                  not_before alcanzado
                          ↓
                     due OrderIntent
                          ↓
                     RiskManager
                          ↓
                    ExecutionAdapter
```

Este flujo define límites mediante Protocols y coordinación temporal pura.
RiskManager → ExecutionAdapter es un paso operativo futuro que el coordinador
no invoca en Fase 2.
Los futuros adaptadores serán `ReplayExecutionAdapter`, `ShadowExecutionAdapter`
y `BinanceLiveExecutionAdapter`. Los contratos actuales son:

- `market_data/contracts.py`: `MarketDataProvider.candles(symbol)` expone un
  iterador de Candle; no conoce la estrategia ni fija intervalos/orígenes.
- `strategy/contracts.py`: `StrategyEngine.on_candle(candle, position, round_state)`
  recibe datos y snapshots opcionales y retorna StrategyResult inmutable con
  `signals: tuple[Signal, ...]` y `order_intents: tuple[OrderIntent, ...]`.
  Ambas tuplas pueden estar vacías de manera independiente. No implementa
  condiciones de entrada. Posteriormente la coordinación podrá registrar/notificar
  Signal y procesar las intenciones elegibles mediante RiskManager; esos efectos
  operativos no se implementan aún.
- `risk/contracts.py`: `RiskManager.evaluate(intent, balance, position)` retorna
  RiskDecision con aprobación explícita o rechazo con motivo, sin reglas de riesgo.
- `execution/contracts.py`: `ExecutionAdapter.submit(intent)` retorna un snapshot
  Order; `poll()` retorna una tupla finita de los Order/Fill actualmente disponibles
  sin bloquear. Aceptar una intención no presupone su llenado inmediato.

La dirección es implementaciones externas → contratos → modelos. Los modelos
solo dependen de biblioteca estándar y utilidades centrales; los contratos solo
de biblioteca estándar y modelos. `orchestration` depende de modelos y del contrato
StrategyEngine: incorpora pending, expone señales y determina due con tiempo
inyectado. No usa reloj interno ni fija delays. Consulta
[coordinación temporal](ORCHESTRATION.md) para el estado y sus resultados.
Consulta [modelos de dominio](DOMAIN_MODEL.md) para sus invariantes.

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
También en Fase 2 solo `STOPPED` es válido. No existe un interruptor que habilite Live.

El comando `python -m trading_engine` solo valida configuración y registra un
evento JSON con timestamp UTC, nivel, evento y modo. No registra el entorno
ni secretos. Los errores se propagan; no se sustituyen por resultados ficticios.

Tests localizados, cambios pequeños y dependencias justificadas. Los paquetes de
implementaciones futuras siguen reservados; no hay lógica de trading, DCA, TP,
filtros, vetos, replay funcional, Shadow, Live, Add Margin ni Telegram.
