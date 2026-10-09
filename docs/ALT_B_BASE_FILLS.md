# Fase 3B-2C-1 — intención Base y hechos Order/Fill

AltBEngine sigue siendo un único motor frozen con solo config. Todos los estados
se reciben y devuelven explícitamente mediante StrategyResult y CoordinatorState.
No cambian interfaces genéricas, fórmulas, filtros ni módulos de datos. Runtime
STOPPED; pruebas con datos sintéticos offline. No hay envío de órdenes, despliegue,
servicios, persistencia financiera, Replay completo ni acceso privado Binance.

```text
Signal → ScheduledAction (T+120s) + reserva PendingEntry
                          ↓ acción válida + permiso + Quote explícito
                   AltBEngine.on_scheduled_action
                          ↓
            OrderIntent BUY MARKET + snapshot BaseState
                          ↓ salida al llamador, sin ejecución
                 Order y Fill explícitos posteriores
                          ↓ AltBEngine.on_order/on_fill
             evidencia Base + conciliación + VWAP/P0
```

## Intención y reserva

El handler exige reserva pendiente propia, identidad completa de acción y símbolo,
Quote.timestamp >= T+120s, permiso operativo y contexto compatible. Una Position
bloquea la entrada. Una Round activa distinta o OPEN/CLOSING bloquea; PENDING_ENTRY
con la misma identidad reservada es compatible si sus tiempos son verificables.
No se recalculan los filtros durante la espera. Antes de consumir, PAUSE,
invalidación, gap o contexto incompatible cancela PendingEntry y su cola.

La referencia teórica BUY es el **ask** del Quote entregado. Se reutiliza
`order_notionals(config)[0]`, sin repetir el factor de leverage:

```text
Margen_Base = C × 59 / 1704.882944
Nocional_Base = Margen_Base × 4
Quantity_teórica = Nocional_Base / Quote.ask
```

Contexto local Decimal de 50 dígitos, ROUND_HALF_EVEN, independiente del llamador.
Sin redondeo a centavos, ticks o steps. Quantity es exclusivamente teórica;
no está cuantizada para Binance ni constituye una cantidad ejecutada.
OrderIntent tiene BUY, MARKET, reduce_only=False, limit_price=None y
created_at=Quote.timestamp. El Quote no produce Fill, exposición ni P0.

IntentId se deriva con make_intent_id, revision 0, RoundId reservado,
purpose `ALT_B_BASE`, event_at=Quote.timestamp y causal_ref=str(ActionId).
Mismo snapshot y mismos inputs producen el mismo output. Cambiar el instante
causal del Quote cambia la identidad; no se usa reloj real ni UUID aleatorio.

Al emitir se retira la acción de la cola y se reemplaza PendingEntry por BaseState.
BaseState conserva la acción consumida y la intención exacta. Retry, otras acciones,
PAUSE/RESUME, invalidación posterior y gaps **no liberan esa reserva ni reemiten**.
Ninguna nueva señal se permite mientras exista BaseState. En esta subfase incluso
una orden terminal conserva la reserva: la finalización de ronda y autorización
de otra ronda requieren implementación posterior. No se inventa esa política.
Un PendingEntry ya evaluado por la fase anterior se bloquea para reconciliación;
no se convierte automáticamente en una entrada tras actualizar el código.

## Snapshot de Base

`AltBStrategyState.base: BaseState | None` es frozen y contiene solo:

- action consumida (incluye RoundId).
- reference_quote que justifica el sizing.
- intent emitida (incluye IntentId y Quantity solicitada).
- order opcional: último snapshot de la única Order vinculada.
- fills: tuple canónica por FillId, con todos los fills confirmados de esa Base.
- reconciliation_reason opcional: conflicto de snapshots de orden, conservado
  hasta una reconciliación explícita futura.

Las cantidades y precios se **derivan**, evitando duplicar valores financieros:
executed_quantity suma únicamente Fill.quantity; average_fill_price reutiliza
weighted_average_fill_price sobre esos fills. order_pending expresa que la orden
no se ha resuelto o que quedan cantidades/conflictos pendientes de conciliar.
reconciliation_required y block_reason explican el bloqueo.
P0 solo aparece con orden terminal conciliada y fills efectivos. Una Base parcial
no tiene P0 definitivo; se conserva su VWAP observado. Un cancel/reject parcial
exige reconciliación y no fija P0 ni activa DCA. Quote jamás altera VWAP o P0.

El estado valida identidad canónica de acción, checkpoint y cursor causal.
El motor valida además que la intención guardada coincide con su configuración y
Quote; cambiar presupuesto o adulterar la Quantity al reconstruir falla.
La cola no puede contener una acción consumida. Restaurar el snapshot íntegro es
determinista; restaurar únicamente velas no recupera reservas ni evidencia.

## Órdenes y fills independientes

Order debe conservar la intención completa emitida: IntentId, RoundId, símbolo,
lado, tipo, Quantity y tiempo. Solo puede vincularse un OrderId por Base. Cambiar
OrderId, created_at o una identidad externa ya vinculada produce ValueError.
Fill debe pertenecer a esa Order y no preceder a la intención. No se exige que
llegue después del snapshot Order: la entrega puede ser desordenada.

Una orden aceptada no demuestra ejecución. Incluso FILLED con filled_quantity
completa pero sin los Fill correspondientes conserva executed_quantity=0 y
bloquea por discrepancia. Un Fill puede llegar antes del reporte de cantidad;
se conserva su evidencia y se bloquea hasta conciliar. Nunca se usa la cantidad
solicitada o reportada como sustituto de cantidad realmente ejecutada.

FillId idéntico y contenido idéntico es idempotente. Mismo FillId con diferente
precio, cantidad o tiempo produce ValueError, conservando el snapshot previo.
Fills distintos se conservan una sola vez; su orden de entrega no cambia el
snapshot canónico. Un exceso de ejecución sobre la intención se conserva y
bloquea como overfill, sin recortar ni descartar evidencia.

Se conserva la Order con updated_at más reciente. Un snapshot antiguo compatible
no hace rollback. Cantidad reportada decreciente, reapertura de una orden terminal,
cambio de cantidad terminal o regresión de estado provocan bloqueo persistente
base_order_conflict. Dos snapshots distintos con el mismo updated_at se rechazan
explícitamente; no se elige silenciosamente uno.

Order/Fill tardíos válidos se procesan sin retroceder last_event_at; el cursor toma
el máximo de los tiempos ya observados, Fill.timestamp y Order.updated_at.
No generan nuevas señales, acciones ni intenciones. Candle, Quote y controles
mantienen la barrera causal anterior. Un Fill anterior a su intención o con
identidad ajena se rechaza; el llamador debe conservar los hechos rechazados para
reconciliación externa. No se ocultan errores ni se fabrican fills.

Cancelación parcial conserva cantidad y VWAP, bloqueando toda entrada o acción
que aumente exposición. Rechazo después de un Fill también conserva ese Fill y
bloquea por discrepancia. PAUSE no cierra exposición ni impide procesar evidencia.
Hechos Order/Fill sin Base emitida bloquean execution_without_base_reservation;
PAUSE/RESUME no elimina esa incertidumbre. No se atribuyen a otra ronda.

## Límites y siguientes subfases

La reserva es por snapshot lógico; el llamador debe separar estados por cuenta y
símbolo y entregar posición/ronda verificables. No hay catálogo global de cuentas.
Consumir el estado retornado evita reemisión y doble contabilización dentro de
ese estado. Reprocesar desde un snapshot anterior reproduce la misma IntentId,
pero **no garantiza exactly-once de efectos externos**. No hay envío, journal,
serialización ni recuperación financiera de producción.

Pendientes explícitos: validación de riesgo LIVE, filtros de exchange, frescura
máxima del Quote, slippage, shortfill LIVE y recuperación exactly-once. No se fijan
valores provisionales ni se habilita LIVE. DCA1–DCA7 y TP operativo no se implementan;
la siguiente subfase recibirá la identidad de ronda, fills, cantidad efectiva y
P0 conciliado. Add Margin, liquidación, funding, fees, Telegram y adaptadores de
trading siguen fuera del alcance. Replay, Shadow y Live utilizarán el mismo motor.

## Continuación implementada — Fase 3B-2C-2

Las limitaciones de 3B-2C-1 anteriores describen esa entrega. Ahora Base confirmada
inicia siete DCA teóricas y TP con el mismo AltBEngine. P0 solo procede de sus
fills conciliados. La reserva se libera únicamente tras cierre neto cero con
todas las órdenes terminales y conciliadas, conservando evidencia en historial.
No se añade ejecución real: [ALT_B_DCA_TP.md](ALT_B_DCA_TP.md).
