# Fase 3B-2C-2 — ronda LONG, DCA1–DCA7 y TP

AltBEngine permanece frozen con solo config, compartido por Replay, Shadow y Live.
Recibe snapshots explícitos y devuelve StrategyResult; no consulta reloj, fabrica
fills ni hace I/O. Runtime STOPPED. No hay adaptador operativo, órdenes enviadas,
servicios, credenciales, descarga histórica ni despliegue. La configuración y
matemáticas canónicas conservan exactamente sus fórmulas y validaciones.

```text
Base FILLED + todos sus fills conciliados → P0 efectivo
                         ↓ mismo AltBEngine
        siete BUY LIMIT teóricas + TP SELL LIMIT reduce_only
                         ↓ Order / Fill explícitos
             promedio real y cantidad neta abierta
                         ↓ cambio de objetivo
     solicitar cancelación TP → esperar terminal + fills conciliados
                         ↓
              reemplazo único con nueva IntentId
                         ↓ salida ejecutada completa
     cancelar entradas pendientes → conciliación total → CLOSED
```

## Inicio, sizing y ancla

Solo una Base con block_reason=base_confirmed y P0 conciliado puede iniciar gestión.
OPEN, fills parciales, FILLED sin evidencia suficiente, shortfill cancelado,
conflictos o contexto ajeno bloquean DCA y TP. El P0 de ronda queda congelado desde
el VWAP efectivo de Base; Quote y fills DCA no lo modifican.

Se reutilizan order_notionals y dca_theoretical_price, que a su vez conservan
order_margins y dca_cumulative_deviation. Para k=1..7:

```text
Margen_k = C × 69 × 1.40^(k-1) / 1704.882944
Nocional_k = Margen_k × 4
Caida_k = 0.015 × sum(1.15^j, j=0..k-1)
Precio_k = P0 × (1 - Caida_k)
Cantidad_k = Nocional_k / Precio_k
```

Se emiten exactamente siete BUY LIMIT, reduce_only=False, una vez al iniciar la
ronda. Todos usan el mismo P0 y el presupuesto original. Decimal a 50 dígitos,
contexto local independiente del llamador; sin cuantización LIVE, redondeo a
centavos, reinversión ni segundo leverage. Las intenciones permanecen teóricas.
Su emisión no demuestra aceptación ni ejecución. Una vela que toque un LIMIT no
produce Fill. Si high/low contiene DCA y TP, su orden intraminuto sigue desconocido.

## Estado y evidencia

AltBStrategyState conserva BaseState y añade round_state opcional e historial
round_history. AltBRoundState frozen guarda config de la ronda, P0, opened_at,
siete OrderLeg en orden DCA1..7, cadena take_profits, solicitudes cancel_requests,
closing, closed_at histórico y bloqueo explícito de reconciliación.
Cada OrderLeg tiene la intención exacta, Order opcional, tuple de Fill únicos
canónicos por FillId y flag persistente de contradicción de Order.
RoundId deriva de sus intenciones; status se calcula como OPEN, CLOSING, CLOSED o
RECONCILIATION. Las cantidades y precios efectivos se derivan de fills, evitando
copias financieras que puedan divergir.

Base, DCA y TP reutilizan la misma validación y conciliación de evidencia en
execution_state.py. Se preservan las interfaces y semántica de BaseState.
Order debe conservar la intención completa, ronda, símbolo, lado, Quantity,
reduce_only y propósito. Cada intención admite un único OrderId; los IDs de
Order no pueden repetirse entre niveles/rondas. No se generan OrderId ni FillId.

Order.filled_quantity se contrasta con la suma de Fill.quantity, sin sustituirla.
Order y Fill pueden entregarse fuera de orden; el watermark toma el máximo de
los tiempos conocidos, nunca retrocede y no se aplica a decisiones de entrada
pasadas. Un snapshot Order antiguo compatible no hace rollback. Un reporte de
cantidad antes de sus fills bloquea nuevos outputs ejecutables hasta conciliarlos.
Una discrepancia transitoria no elimina la escalera. Contradicciones persistentes,
identidades incompatibles, exceso de ejecución o contexto ajeno producen bloqueo
explícito y solicitudes de cancelación de órdenes potencialmente activas.

FillId idéntico con contenido idéntico es idempotente. Precio, cantidad, timestamp
u orden distintos para el mismo ID constituyen error. Se comprueba unicidad
entre Base, todos los niveles, salidas e historial. Los overfills conservan
la evidencia completa; no se recortan ni se usan para abrir un SHORT.

## TP inicial, parciales y objetivo dinámico

```text
Cantidad_entrada = sum(Fill.quantity de Base y DCA)
AveragePrice = sum(qty × fill_price de entradas) / Cantidad_entrada
TP_target = AveragePrice × 1.008
Cantidad_abierta = Cantidad_entrada - sum(Fill.quantity de todos los TP)
```

El TP inicial es SELL LIMIT reduce_only=True por la cantidad Base realmente
comprada. Tras cada fill efectivo se conserva la evidencia y se deriva el objetivo
actual; no se usan cantidades solicitadas, pendientes o Quote como promedio.
TP no aplica leverage. Cada nueva salida tiene cantidad igual a la posición
LONG neta confirmada en el instante de su emisión; nunca mayor. No se inventan
tolerancias para declarar cero un residuo. Un exceso de salida queda bloqueado.

Un TP parcial descuenta cantidad neta, conserva su VWAP de ejecución como hecho,
y no cierra la ronda. Si su cantidad restante y precio aún coinciden con el
objetivo, no se reemplaza innecesariamente. Un DCA efectivo puede cambiar tanto
cantidad abierta como promedio y objetivo; el P0 y la escalera siguen intactos.

## Cancelación y reemplazo sin solapamiento

Extensión genérica mínima: OrderCancelRequest(intent, created_at), frozen,
correlacionada por IntentId original. Puede solicitar la cancelación antes de
conocer OrderId; la capa externa futura deberá resolver esa correlación. No
representa una cancelación exitosa. StrategyResult y CoordinationResult añaden
cancel_requests tuple vacía por defecto. StrategyCoordinator valida causalidad
y las expone al llamador; no llama ExecutionAdapter. Se rechazan duplicados en
un output y emitir/cancelar la misma intención simultáneamente.

Al cambiar el objetivo se registra una sola solicitud para el TP anterior.
Su Order y sus Fill se conservan íntegros; se considera potencialmente activo
hasta recibir un estado terminal verificable. Los retries no generan otra solicitud
ni otro TP. Los objetivos posteriores se derivan de toda la evidencia disponible;
no se encolan reemplazos intermedios obsoletos.

El reemplazo solo se emite cuando **todos** los TP anteriores tienen Order
terminal (CANCELED, REJECTED o FILLED), cantidades conciliadas exactamente y
ningún conflicto. REJECTED sin una transición de cancelación solicitada queda
bloqueado: no se inventa una política de retries LIVE. Durante cancelación se
procesan fills tardíos y se recalcula cantidad abierta antes de sustituir.
El nuevo IntentId incluye el TP predecesor y las identidades causales de fills.

La confirmación terminal de dominio afirma el total final ejecutado. Si el
proveedor no puede garantizar esa semántica, la capa externa debe mantener la
confirmación pendiente; no basta un timeout ni asumir éxito al solicitar cancelar.
No se ha implementado esa garantía LIVE. Un fill que contradiga una confirmación
terminal ya conciliada se conserva, bloquea la ronda y solicita cancelar el TP
nuevo si existe; nunca se emite un tercer reemplazo ante esa incertidumbre.

## Cierre, PAUSE e historial

Con salida completa se inicia CLOSING y se solicita cancelar todas las DCA que
puedan aumentar exposición, incluidas intenciones pendientes sin OrderId conocido.
También un TP totalmente ejecutado inicia cierre aunque hayan llegado entradas
adicionales. No se libera reserva con órdenes desconocidas o potencialmente activas.
Si un DCA se ejecuta durante cierre, sus fills se conservan. La salida remanente
espera a que las entradas se resuelvan, usando cantidad y promedio efectivos.

CLOSED requiere cantidad neta exactamente cero, Base resuelta, todos los niveles
y TP terminales y conciliados, sin conflictos ni salidas incompatibles. Solo
entonces se mueve Base y gestión a RoundRecord en round_history y se libera la
reserva activa. El historial conserva config original y todos los fills; una
nueva ronda no borra la anterior. No se inicia otra ronda automáticamente: sigue
siendo necesaria una señal nueva, permiso y filtros válidos.

Duplicados históricos son idempotentes. Un hecho nuevo que contradiga un cierre
archivado conserva la evidencia y cambia su status a RECONCILIATION; closed_at
queda como el instante del cierre previamente observado, sin afirmar que el
cierre siga válido. Se bloquean nuevas entradas y se solicitan cancelaciones de
órdenes activas de la ronda actual. PAUSE/RESUME no elimina este bloqueo. Una
Base actual pendiente puede tener base_cancel_request para conservar esa solicitud.

PAUSE bloquea nuevas rondas y cancela solo la espera de una nueva Base; no elimina
rondas abiertas, posiciones u órdenes ni impide recibir fills. Filtros y vetos
siguientes no alteran DCA/TP de una ronda existente. Un gap solo reinicia historial
de mercado; no fabrica ejecuciones ni borra evidencia financiera.

## Identidad y límites

DCAk: make_intent_id(RoundId, `ALT_B_DCA_k`, opened_at, str(Base.IntentId), revision=0).
TP: purpose `ALT_B_TP`, instante causal de emisión y causal_ref canónica
`Base.IntentId:entries=<FillId ordenados por valor UUID>:exits=<FillId ordenados>:previous=<IntentId anterior o initial>`.
Las listas usan coma, UUID lowercase con guiones y orden numérico; no se incrementa
revision por retries. Misma secuencia causal e inputs produce el mismo snapshot.
Permutar la entrega de los mismos hechos conciliados conserva su evidencia,
cantidades y cálculos; solicitudes pueden reflejar cuándo se observó por primera
vez un cambio. No se pretende borrar causalidad operativa de las entregas.

El llamador separa snapshots por cuenta/símbolo, conserva siempre el estado
retornado y entrega solo hechos de ejecución verificables. Hay un único motor,
sin registro global de cuentas ni persistencia. No hay exactly-once externo,
recovery financiero, cuantización LIVE, frescura de Quote, slippage, filtros de
exchange ni validación de riesgo LIVE. Base shortfill y errores contradictorios
requieren reconciliación explícita futura, sin inventar políticas operativas.
Add Margin, liquidación, funding, fees, Telegram, Replay histórico completo,
adaptadores privados y servicios siguen fuera. LIVE no se habilita.
