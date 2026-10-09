# Arquitectura mínima — Fase Datos 2

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

Fase 3B-1A implementa únicamente la capa matemática pura en `strategy/alt_b`.
`AltBConfig` es un snapshot frozen de los parámetros canónicos; solo Symbol y
order_budget son configurables y el resto se valida contra los valores de
[Alternativa B](STRATEGY_ALT_B.md). `math.py` calcula pesos, márgenes, nocionales,
desviaciones y precios teóricos DCA anclados a P0, promedio ponderado de fills
y TP teórico. El llamador debe seleccionar los fills de entrada correctos.
No se genera Quantity, IDs, señales, acciones ni intenciones y no hay motor concreto.

Cada cálculo usa un Context Decimal nuevo dentro de `localcontext`, con
`MATH_PRECISION = 50`, `ROUND_HALF_EVEN`, límites de exponente `MIN_EMIN`/`MAX_EMAX`
y traps explícitos para InvalidOperation, DivisionByZero y Overflow. No hereda ni
modifica el contexto global, ni cuantiza a centavos, ticks o steps. Los cocientes
no terminantes se calculan a esa precisión; ningún nivel absorbe un residuo del
presupuesto. Solo depende de biblioteca estándar, AltBConfig y modelos necesarios;
no hace I/O ni accede a reloj, adaptadores, coordinación o servicios externos.

Fase 3B-1B añade `strategy/alt_b/market.py` y `entry.py`: minutos UTC normalizados
→ bloques exactos 15m → filtros y vetos A/B → `EntryEvaluation` frozen. No conecta
este resultado con el contrato StrategyEngine ni emite Signal, Round,
ScheduledAction u OrderIntent. Solo Symbol y order_budget siguen configurables;
los umbrales y el warmup de 1560 minutos son canónicos y congelados.

La evaluación requiere 26h consecutivas hasta un checkpoint UTC de 15m y rechaza
cualquier vela con cierre posterior, incluso si no pertenece a la ventana útil.
Dentro de la ventana no ordena, rellena ni interpola datos; la historia anterior
no participa en las fórmulas. Taker-buy se exige únicamente en el bloque actual
y nunca se sustituye por volumen total, quote o proxy. Las medianas de volumen
usan 16/96 bloques anteriores; RVref24h usa 96 snapshots anteriores, todos
excluyen el actual. RV30m usa 31 cierres para 30 log returns, ddof=0 y sin anualizar.
Todos los cálculos reutilizan el Context local de 50 dígitos de la matemática pura.

Los errores de tipos, checkpoint, símbolo configurado o datos futuros producen
TypeError/ValueError. La calidad de mercado insuficiente produce data_valid=False,
entry_allowed=False y una razón estable, con métricas/flags no calculados en None.
Consulta [Alternativa B](STRATEGY_ALT_B.md) para razones, precedencia y fórmulas;
[modelos](DOMAIN_MODEL.md) describe el nuevo dato opcional de Candle.

Fase 3B-2A añade `strategy/alt_b/state_market.py`: la transición pura
`ingest_closed_1m(state, candle, config) → AltBMarketStep` devuelve un nuevo
`AltBMarketState`, EntryEvaluation opcional y MarketIngestResult. El estado frozen
almacena únicamente `candles_1m: tuple[Candle, ...]`, continuas, ordenadas y 1m UTC
normalizadas de un solo símbolo. Su límite procede de AltBConfig.warmup_minutes,
canónico y congelado en 1560 para cualquier configuración válida. No guarda
derivados, cursor duplicado, caché RV ni estado oculto mutable.

El llamador suministra cada evento ya cerrado; la transición no consulta reloj,
no añade is_closed y no normaliza timestamps del proveedor. Valida símbolo contra
config y acepta la siguiente vela solo si empieza donde termina la anterior.
El último intervalo idéntico retorna `duplicate`, el mismo estado y evaluación
None; el mismo intervalo con contenido distinto o cualquier vela anterior produce
ValueError. Un salto hacia adelante retorna `gap_reset` con únicamente la nueva
vela, sin fabricar minutos: vuelve a exigir el warmup completo. Los demás eventos
válidos retornan `accepted` y conservan las últimas warmup_minutes velas.

Cada cierre UTC 00/15/30/45 aceptado (incluido tras gap) llama exactamente una vez
al evaluador aprobado de 3B-1B con el historial causal del nuevo estado; antes de
26h conserva su resultado fail-closed. Fuera de checkpoint devuelve evaluación
None. Candle y taker-buy, incluido None, se conservan sin modificaciones.
La prueba de equivalencia cubre todos los checkpoints de una secuencia de 27h
frente al historial causal completo; no cambian fórmulas ni parámetros.

## Estado explícito pendiente para la siguiente fase

StrategyEngine por eventos no recibe AltBMarketState y StrategyResult no devuelve
estado de estrategia. Antes de Signal, ScheduledAction y Base debe definirse una
transferencia explícita de estado que preserve pureza, determinismo, persistencia
futura y recovery, manteniendo separado StrategyCoordinator genérico del estado
financiero de Alternativa B. No se incorpora AltBMarketState en CoordinatorState
ni se modifica ninguno de esos contratos en 3B-2A.

Un snapshot inmutable podrá guardarse y restaurarse posteriormente; esta fase no
implementa persistencia ni serialización. El futuro runner deberá conservar
historial validado, cursor de última vela, estado lógico de estrategia/ronda y
acciones/órdenes pendientes. Una repetición del último minuto no reevalúa; los
minutos anteriores al cursor requieren recovery/backfill explícito, sin corrección
implícita. No hay estado de rondas, deduplicación de fills, cancel/replace de TP
ni garantía de exactamente una orden por evento.

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
- `market_data`: contrato de provisión y coordinación manual del histórico público;
  no toma decisiones ni ejecuta órdenes.
- `strategy`: contratos, matemáticas y evaluación pura de entrada de Alternativa B; el núcleo de decisión
  concreto sigue pendiente. `signals` sigue reservado. Sin llamadas a Binance,
  Telegram, persistencia o adaptadores específicos de un modo.
- `risk`: futura validación de intenciones antes de ejecutar.
- `execution`: contrato mínimo de ejecución. Los futuros adaptadores de modo cumplirán
  esos contratos y no contendrán una estrategia duplicada.
- `orchestration`: coordinación y scheduling deterministas compartidos; sin
  ejecución, llamadas a riesgo ni efectos externos.
- `replay`, `shadow`, `live`: futura coordinación de cada modo y sus adaptadores.
- `add_margin`: futura política compartida, pendiente de especificación.
- `persistence`: histórico SQLite exacto, separado de las reglas de decisión.
- `binance`: HTTPS público USDⓈ-M Futures y normalización de datos, sin SDK,
  API keys, endpoints privados ni órdenes. `telegram` sigue reservado.
- `utils`: herramientas generales sin reglas financieras.

Fase Datos 1 añade un flujo externo y manual separado del núcleo:
archivos oficiales monthly/daily + REST público → normalización 1m UTC →
SQLite histórico completo → lectura cronológica Candle. Auditoría y recovery
detectan gaps/conflictos sin interpolar ni sobrescribir datos. La CLI termina
tras cada invocación; no llama StrategyEngine ni modifica el runtime STOPPED.
La integración offline valida esas Candle con ingest_closed_1m y el evaluador
aprobado; AltBMarketState conserva su ventana de 1560 minutos. No se persiste
estado financiero, rondas, acciones u órdenes. Consulta [DATA_PIPELINE.md](DATA_PIPELINE.md).

Fase Datos 2 orquesta ese mismo pipeline mediante `market_data/collector.py`.
`collector_state`, `collector_gaps` y `collector_errors` conservan solo progreso
operativo, gate de histórico validado, gaps y errores por job.
Todas las conexiones de escritura comparten flock;
datos y checkpoints usan transacciones. La actualización finita audita un tramo
acotado desde el primer minuto pendiente; la auditoría completa va separada.
`data_safety.py` añade reserva de disco y backup SQLite consistente sin reemplazo.
Los ejemplos systemd permiten operar en VPS tras aprobación; siguen sin instalar.
No se modifica ni conecta el estado financiero/StrategyEngine. Ver [DATA_COLLECTOR.md](DATA_COLLECTOR.md).

## Invariantes

Python 3.12. Precios, cantidades, dinero, PnL, margen y fees utilizarán `Decimal`;
no `float` en cálculos financieros críticos. `to_decimal` admite texto, enteros
y Decimal finitos; rechaza float/bool y no inventa precisión, redondeo o tamaños
de lote. La matemática de Alternativa B usa la precisión local indicada arriba;
las políticas de cantidades ejecutables siguen pendientes.

Timestamps internos UTC timezone-aware. `require_utc` rechaza fechas naive y
offsets no UTC; la normalización de fuentes externas corresponderá a sus adaptadores.

Configuración separada del código y secretos fuera del repositorio. La carga
rechaza modos no implementados o desconocidos en lugar de aceptar una operativa
incompleta. `OperatingMode` enumera estados futuros sin definir transiciones.
También en Fase 3B-2A solo `STOPPED` es válido. No existe un interruptor que habilite Live.

El comando `python -m trading_engine` solo valida configuración y registra un
evento JSON con timestamp UTC, nivel, evento y modo. No registra el entorno
ni secretos. Los errores se propagan; no se sustituyen por resultados ficticios.

Tests localizados, cambios pequeños y dependencias justificadas. Los paquetes de
implementaciones futuras siguen reservados; solo está implementada la matemática
pura de sizing, DCA, promedio y TP, la evaluación de filtros/vetos de entrada y
la ingesta incremental con estado de mercado explícito.
No hay motor concreto, señales, órdenes DCA/TP, replay funcional, Shadow, Live,
Add Margin ni Telegram.
