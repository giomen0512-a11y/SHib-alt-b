# Simulador causal de órdenes y fills — Fase 4A-2B

Implementación exclusivamente offline, **opt-in**, sin transporte, adaptador LIVE
ni resultados financieros. Runtime STOPPED. Base aprobada:
`7fbc249ab532d9c21b4b72e67436caa1ce14e7ee`.

## Arquitectura y activación

```text
SQLiteHistory read-only → ReplayRunner → ModeledQuotes / ExecutionPolicy
                                  ↓
                    StrategyCoordinator(AltBEngine)
                                  ↓ OrderIntent / OrderCancelRequest
                        SimulatedExecution
                                  ↓ ReplayFact MODELED_EXECUTION
                    StrategyCoordinator(AltBEngine)
```

`replay/simulated_execution` depende solo de dominio, puertos de replay, encoding
y políticas de precios. No importa estrategia ni execution adapters; no recalcula
P0, sizing, DCA, VWAP o TP. El mismo motor gestiona Base, DCA1–DCA7, reemplazos
y cierre. No se modifican leverage 4, TP 0.008, filtros, presupuesto o fórmulas.

El constructor de ReplayRunner añade `simulator=None`. Sin simulador, NoQuotes y
FixtureQuotes conservan comportamiento y fingerprints anteriores. Activar requiere
construir explícitamente SimulationPolicy y SimulatedExecution con **la misma
instancia QuoteSource** entregada al runner. Si es ModeledQuotes, ambas políticas
de precios deben coincidir y se mantiene la misma sesión SQLiteHistory.
FactSource sigue compatible en el modo anterior. Con simulador se admiten sus
controles, pero se rechaza mezclar Order/Fill externos con hechos modelados:
impediría garantizar volumen y exposición del simulador.

## Configuración obligatoria y escenarios

SimulationPolicy frozen contiene ExecutionPolicy de 4A-2A y requiere:

| Campo | Contrato |
| --- | --- |
| acceptance_latency_ms | Entero positivo; demora emisión→aceptación/rechazo |
| execution_delay_ms | Entero positivo; demora mínima aceptación→elegibilidad |
| cancellation_latency_ms | Entero positivo; solicitud recibida→confirmación terminal |
| delivery_latency_ms | Entero no negativo; ocurrencia→disponibilidad al motor |
| queue | WITHHOLD_WITHOUT_QUEUE_EVIDENCE o ASSUME_SHARED_VOLUME_BUDGET_FIFO |
| acceptance | ACCEPT_SUPPORTED_OFFLINE o REJECT_ALL_MODELED |
| assumptions | Explicación obligatoria de supuestos y límites |
| rules_version | Versión implementada closed-minute-shared-budget-v1 |

No hay defaults numéricos ni supuestos calibrados de Binance. Se rechazan bool,
float, negativos, duraciones no representables y versiones no implementadas.
Las transiciones de aceptación/cancelación son estrictamente posteriores:
el contrato aprobado de conciliación rechaza dos snapshots Order distintos
con el mismo updated_at. Esto también evita ciclos de aceptación/cancelación
instantáneos. Delivery puede ser cero; no cambia instantes de ocurrencia.
La configuración se construye desde objeto validado; ExecutionPolicy mantiene
su carga estricta desde objeto/JSON. No se leen credenciales ni entorno.

Los tests usan latencias **sintéticas**: aceptación 100ms, demora ejecución 100ms,
cancelación 120000ms y entrega 0/500ms. No son valores de producción o Binance.
Los tres perfiles reutilizan spread/slippage/vigencia/participación de 4A-2A;
ninguno equivale a mercado observado. No hay aleatoriedad.

WITHHOLD abstiene por cola insuficiente aunque haya candidato de precio/volumen.
BUDGET_FIFO autoriza únicamente una **hipótesis modelada de asignación**:
FIFO por emisión e IntentId, con cota compartida y demás restricciones. No
afirma prioridad real de cola ni profundidad histórica. Es obligatorio elegir
la hipótesis al activar; el escenario por sí solo no autoriza ejecución.

## Tiempo causal y orden de eventos

Se separan intent.created_at, emitted_at real del replay, acceptance_at,
eligible_from, Fill.timestamp, confirmación de cancelación y received_at.
Una intención que el motor deriva de evidencia tardía puede tener created_at
anterior a emitted_at; la orden solo existe desde emitted_at, nunca retrospectivamente.

```text
acceptance_at = emitted_at + acceptance_latency_ms
eligible_from = acceptance_at + execution_delay_ms
cancel_confirm_at = cancel_received_at + cancellation_latency_ms
received_at = ocurrencia + delivery_latency_ms
```

La primera ejecución posible se evalúa **al cierre de una vela completa cuyo
open_time >= eligible_from**. Aplica a MARKET y LIMIT: una orden aceptada o creada
durante una vela no utiliza su recorrido anterior. Una MARKET espera esa ventana
de evidencia; no se afirma ejecución instantánea. Fill.timestamp es el cierre
de la evaluación modelada, no una transacción observada dentro del minuto.
OHLCV solo se usa en ese cierre; su disponibilidad inmediata sigue siendo el
supuesto explícito de 4A-2A. No se lee ningún precio/cierre futuro.

Se conserva el orden anterior de controles/hechos externos, cierre/gap y acciones
T+120. Nuevos puntos explícitos:

1. Entregar controles y hechos externos como antes.
2. Procesar transiciones de aceptación/cancelación y entregas modeladas ya vencidas,
   antes del cierre. Cancelación precede a aceptación si coinciden para una orden.
3. Procesar Candle/gap con el motor; evaluar órdenes existentes sobre la vela cerrada.
4. Entregar evidencia recién generada si su delivery ya venció.
5. Procesar acciones estratégicas y Quotes como antes; drenar entregas PENDING nuevas.

Orden/Fill generados para entrega futura permanecen en deliveries. facts archiva
**evidencia generada**, no hechos ya recibidos; el motor solo recibe lo vencido.
Los batches se ordenan por prioridad Order antes de Fill y sequence estable.
Un FillEvidence contiene su snapshot Order actualizado, para conciliación atómica.
No se añade retrospectivamente un evento ni se cambia una Candle procesada.

## Estados y hechos modelados

Intención recibida → Order PENDING → aceptación OPEN o REJECTED →
PARTIALLY_FILLED/FILLED, o solicitud/pending de cancelación → CANCELED.
La aceptación se registra en audit; OPEN no demuestra ningún fill.
Cancelación antes de aceptar deja terminal la orden, sin reapertura posterior.
Una orden ya FILLED permanece FILLED al confirmar una solicitud pendiente.
REJECT_ALL y órdenes incompatibles tienen rechazo y causa explícitos.

Todas las ReplayFact y ExecutionAudit llevan MODELED_EXECUTION. Se reutilizan
Order, Fill y FillEvidence sin cambios al dominio. La etiqueta no acredita que
se observó Binance ni que se validó un backtest financiero real.

## MARKET Base

Solo se recibe una intención que AltBEngine emitió después de su acción T+120.
Se exige aceptación, ventana temporal completa, Quote del instante evaluado,
símbolo correcto, vigencia/política coincidentes para ModeledQuoteEvidence,
cota de volumen y hipótesis de cola permitida. Sin Quote válida, no hay Fill.
La ausencia conserva la orden abierta y motivo; se reevalúa en otra vela completa,
sin trasladar ni reactivar una acción estratégica que el runner invalidó.

```text
precio BUY MARKET = Quote.ask × (1 + slippage_bps/10000)
```

Spread ya pertenece al Quote; no se vuelve a aplicar. Slippage se aplica una vez
al precio modelado. No se usan low/high para mejorar una MARKET. BUY es el único
MARKET soportado; no se simulan shorts u órdenes LIVE.

## LIMIT: candidatos y protección

Se conserva exactamente quantity y limit_price estratégicos. BUY requiere
reduce_only=False; SELL LIMIT requiere reduce_only=True. Un toque/cruce es candidato,
sin demostrar cola o liquidez. Se evalúa únicamente la vela completa posterior a
aceptación/demora, con hipótesis de cola, volumen y secuencia permitidos.

La política de slippage es un **buffer conservador de elegibilidad** en LIMIT:

```text
BUY: candidato ajustado = low × (1 + slippage_bps/10000)
SELL: candidato ajustado = high × (1 - slippage_bps/10000)
```

TOUCH permite <= limit BUY / >= limit SELL; STRICT_CROSS exige < / >.
ADDITIONAL_EVIDENCE exige además Quote válida y ask ajustado <= límite BUY o
bid ajustado >= límite SELL al cierre. Un cruce solo tampoco autoriza un Fill.

Si todas las condiciones permiten asignar cantidad, **el Fill usa el límite
exacto**, sin mejora de precio. Slippage no se suma otra vez al límite.
Así BUY nunca supera el límite y SELL nunca cae por debajo. El buffer reduce
elegibilidad; no se presenta como un coste financiero cobrado adicionalmente.
No se fabrica una trayectoria o una hora de toque intraminuto.

## Liquidez compartida y parciales

```text
cap del minuto = candle.volume × max_volume_participation
cap restante = cap - suma(asignaciones ya modeladas)
cantidad candidata = min(restante orden, cap restante)
```

La misma cota se comparte entre Base, todos los DCA y TP de ese minuto, sin
reutilizarla por orden. Cada asignación conserva IntentId y cantidad. Las unidades
son del contrato 1000SHIBUSDC; no se convierten a SHIB individual. No se inventa
profundidad ni tiempo/posición de cola; ambos siguen marcados desconocidos.

SELL reduce-only se limita también a exposición neta derivada de los Fill BUY
y SELL canónicos de la misma ronda, nunca de nocionales o cantidades solicitadas.
Cada lado se suma por FillId en Decimal de 50 dígitos; no se reagrupa por orden
ni se toleran residuos para fingir neto cero. El simulador no deriva VWAP ni TP.

ALLOW_PARTIAL_ONLY_WITH_EVIDENCE puede asignar un parcial bajo la hipótesis
explícita de presupuesto, evidencia de vela y condiciones anteriores.
WITHHOLD_IF_PARTIAL_REQUIREMENT_UNKNOWN abstiene si la capacidad implica parcial.
Cero volumen/capacidad, cola insuficiente, ambigüedad o evidencia insuficiente
se registran como no_execution. No se supone fill completo por tocar precio.
Parciales actualizan cantidad reportada, restante, status y evidencia; el motor
recalcula VWAP/TP. Ningún Fill excede la solicitud.

## Cancelación y reemplazo TP

El simulador correlaciona por intención original y recibe la solicitud una vez.
El TP anterior sigue activo hasta cancel_confirm_at; puede ejecutar en velas
completas cuyo cierre preceda a esa confirmación. Solicitar no equivale a cancelar.
Si la confirmación cae dentro de una vela, no se adjudican después ejecuciones de
esa vela parcialmente activa: su secuencia es desconocida. Confirmación que
coincide con el cierre precede a la evaluación de esa vela, de forma conservadora.

Al confirmar se conserva el total ejecutado final y status terminal. Todas las
ejecuciones previas ya están generadas; delivery uniforme preserva su entrega
antes de la confirmación terminal. El motor recibe ese hecho y decide el único
reemplazo. Un DCA posterior al reemplazo puede pedir otra cancelación, conservando
un único TP potencialmente activo. No se crea un reemplazo desde el simulador.
Una orden cancelada no genera más fills. Contradicciones se bloquean, nunca se
sobrescriben; no se implementa ingestión tardía de hechos LIVE.

## Ambigüedad intraminuto

Los candidatos BUY LIMIT y SELL LIMIT de una misma ronda y vela se clasifican
antes de asignar volumen. BASE abstiene ante ambos lados: secuencia desconocida.
FAVORABLE prioriza TP y ADVERSO DCA como **hipótesis de sensibilidad**, registradas
en audit. Dentro del lado se mantiene orden por emisión/IntentId.

La hipótesis solo ordena evaluación/asignación y entrega modeladas; no afirma
timestamps observados intraminuto. Cada fill sigue exigiendo su precio, ventana,
cola asumida, volumen y reduce-only compatibles. Si queda cota, el otro lado puede
recibir una asignación compatible: TP primero no finge cancelación instantánea de
DCA. Nuevos TP/DCA creados por el motor al recibir esos hechos no participan en
la vela anterior. No se genera trayectoria OHLC.

## Identidades, idempotencia y conflictos

Namespace exclusivamente simulado: `4d2fb073-3734-51d0-8a5a-a2c14bbb2f53`.
UUIDv5 OrderId incorpora identidad completa del simulador e IntentId; FillId añade
OrderId y cierre UTC del minuto. A lo sumo un Fill por orden/minuto; misma ejecución
fragmentada conserva IDs. No hay uuid4, datetime.now, reloj real ni random.
IntentId idéntica/contenido idéntico es idempotente; contenido distinto interrumpe.
Repetir una cancelación exacta o última vela exacta no duplica cantidades.
Cancelación/minuto conflictivos se rechazan sin sobrescribir. Los Fill no pueden
duplicarse/conflictuar en una restauración validada.

## Snapshots, huella y métricas

ReplaySnapshot añade simulation_identity y ExecutionState completo frozen:
política/perfil, órdenes/intenciones, aceptación/elegibilidad, fills, cancelaciones,
transiciones futuras, entregas futuras, archivo de hechos generados, audit,
cotas/asignaciones de liquidez, secuencia y cursor de minutos. Todo entra en su
checksum integral. La huella encadena identidad, hechos y decisiones técnicas.
Restaurar exige mismas reglas/parámetros, fuentes/dataset y sesión SQLite;
se validan además IDs, cantidades, timestamps, procedencia y cotas.

Continuo == fragmentado incluye estado estratégico y simulado, IDs, fills,
cancelaciones, emisiones, métricas, bloqueos y SHA-256. Se prueban checkpoints
subminuto, aceptación no entregada y cancelación pendiente. No hay serialización,
journal, recovery entre procesos o LIVE. La evidencia crece con la sesión en
memoria; no se presenta como almacenamiento persistente para varios años.

ReplayReport incluye simulation y execution_metrics: intenciones, aceptadas,
rechazadas, PENDING, PARTIALLY_FILLED, FILLED, cantidad bruta modelada ejecutada,
órdenes sin fills, cancelaciones solicitadas/confirmadas/pendientes, fills generados,
razones de no ejecución, ambigüedades, cota utilizada, conflictos, escenario y
procedencia. Cantidad bruta incluye BUY y SELL, no PnL o volumen neto de exposición.
PENDING significa pendiente de aceptación; OPEN se consulta en las órdenes.
Los conflictos interrumpen con causa explícita; no se completan rondas por fuerza.

## Límites y próxima contabilidad

FINANCIAL_RESULTS_NOT_AVAILABLE permanece obligatorio. No hay rentabilidad,
drawdown financiero, equity, fees, funding, margen disponible, liquidación ni
Add Margin. Las rondas sintéticas prueban contratos causales, no rentabilidad
2024–2026. No se descargan datos ni se conecta exchange.

Pendientes: calibración verificable de supuestos/latencias/cola, datos más finos,
validación fuera de muestra, filtros/ticks/steps, ledger de cash/posición/costes,
fees y funding con fuentes verificadas, mark/margen y reconciliación contable.
No convertir una hipótesis BASE en hecho real ni ocultar no ejecuciones al informar
contabilidad. La entrega se detiene para revisión, sin merge ni despliegue.
