# Ledger financiero offline — Fase 4A-3A

Base aprobada: `866e335a122e17ac1a328c03ecb2e7d2152e3e15`.
Runtime STOPPED. Solo contabilidad de ejecuciones confirmadas/modeladas recibidas;
sin transporte, órdenes nuevas, fees, funding, equity, margen ni liquidación.

## Arquitectura y activación

```text
SQLiteHistory → ReplayRunner → StrategyCoordinator(AltBEngine)
                    ↑                 ↑ FillEvidence recibido
             SimulatedExecution       │
                    └──── ReplayFact ─┴─→ FinancialLedger
```

`replay/financial_ledger` depende únicamente de biblioteca estándar, dominio,
puertos de evidencia y encoding. Es puro, frozen e independiente de estrategia,
ejecución, riesgo, margen y persistencia. No lee Candle, Quote, datos históricos,
variables de entorno o secretos; no calcula P0 ni TP.

Activación explícita: `ReplayRunner(..., ledger=FinancialLedger(LedgerPolicy()))`.
Por defecto `ledger=None`: se conservan fingerprints, checksum y JSON anteriores.
Los nuevos campos opcionales se omiten del encoding cuando son None; no se
reinterpretan campos anteriores. Tres vectores congelados desde el main aprobado
comprueban NoQuotes, FixtureQuotes y ModeledQuotes+SimulatedExecution.

El runner entrega FillEvidence al mismo coordinador y aplica el asiento en ese
mismo instante de recepción. Los hechos futuros en `simulation.facts` o
`deliveries` no se contabilizan. Orders, intenciones, cancelaciones, velas y
Quotes no son asientos. Un Fill conflictivo detiene la sesión y deja el prefijo
financiero válido y el conflicto íntegro para revisión.

## Contrato y unidades

Contrato inicial: 1000SHIBUSDC, LONG y liquidación USDC. Quantity y volumen usan
unidades del contrato ya aprobado. No hay factores 1000, conversiones a SHIB,
segundo leverage, presupuesto convertido a equity ni capital inventado.

LedgerPolicy valida símbolo, moneda, versión y procedencias permitidas. Se usa
costo promedio móvil, cantidad canónica por FillId/lado y orden económico estricto.
Identidad SHA-256 determinista de todos sus parámetros; no aleatoriedad/reloj real.
Los importes/precios/cantidades requieren Decimal finito; UTC timezone-aware.
Context local nuevo de precisión 50, ROUND_HALF_EVEN, exponente extendido y traps
InvalidOperation/DivisionByZero/Overflow; no hereda el contexto global.
Sin redondeo interno a centavos, ticks o steps; las divisiones no terminantes
quedan acotadas por la precisión declarada, no se afirman racionales infinitos.
Errores numéricos bloquean sin crear un asiento parcial. Un nocional o promedio
positivo que se redondee a cero por underflow también bloquea; no fabrica costo cero.

## Asientos, compras y costo promedio móvil

Un LedgerEntry enlaza FillId, OrderId, IntentId, RoundId, Symbol, Side, cantidad,
precio efectivo, nocional USDC, executed_at, received_at, sequence, procedencia,
costo liberado, PnL del asiento, exposición/costo posteriores y FillEvidence original.

Para BUY, con Q/C cantidad y costo abiertos previos, q/p del Fill:

```text
N = q × p
Q_nueva = Q + q
C_nuevo = C + N
promedio_abierto = C_nuevo / Q_nueva
```

Base y DCA usan el mismo procedimiento. CANCELED no revierte compras: Base
shortfill conserva posición/costo aunque el motor permanezca bloqueado y no
fije P0 ni autorice DCA/TP. Order.filled_quantity no sustituye Fill.quantity.

## Ventas, parciales y PnL bruto

SELL solo reduce exposición LONG de esa RoundId, con reduce_only=True. BUY
requiere reduce_only=False. Se bloquea cualquier exceso de salida o de cantidad
solicitada; no se recorta evidencia ni abre SHORT.

```text
ingreso_bruto = q × precio_efectivo_SELL
costo_salida = q × promedio_contable_abierto_previo
PnL_bruto_del_asiento = ingreso_bruto - costo_salida
C_nuevo = C - costo_salida
Q_nueva = Q - q
promedio_abierto = C_nuevo / Q_nueva  # None si Q_nueva == 0
```

La cantidad abierta se deriva siempre de sumas canónicas por FillId de BUY menos
SELL, a precisión 50, como la ejecución/estrategia aprobada; no se reagrupa por
Order ni se introduce tolerancia para declarar cero. Los nocionales por lado
se suman también canónicamente por FillId. El costo se transforma en la secuencia
económica aceptada, porque vender y volver a comprar altera el promedio móvil.

**Resolución numérica de la última venta:** únicamente cuando la cantidad canónica
queda exactamente cero, la última venta libera todo el costo remanente. Esto
asigna a esa salida el residuo de división a precisión 50 y evita dejar costo
sin cantidad; no perdona exposición ni modifica cantidades/precios/fills.
No se usa una tolerancia ni se cuantiza a centavos.

Una compra nunca realiza ganancias: conserva el PnL previamente reconocido y
su asiento tiene PnL cero. Antes de la primera venta, el costo abierto es la suma
canónica de los nocionales BUY; el orden de redondeo no fabrica PnL. Después de
cada SELL, el acumulado se obtiene por conservación contable a la misma precisión:

```text
costo_total_liberado = nocional_BUY_total - costo_abierto
PnL_bruto_acumulado = nocional_SELL_total - costo_total_liberado
```

Así una ronda plana concilia contra ingresos SELL menos nocionales BUY. Diferentes
reagrupaciones de sumas a precisión finita pueden variar en el último dígito;
la versión fija el orden canónico para totales y el orden económico para costo.

El ledger usa precios efectivos de Fill, nunca P0 o TP teórico. Después de ventas
parciales y nuevas compras, su promedio puede diferir del promedio estratégico
sobre todos los BUY históricos. AltBEngine conserva su regla y TP dinámico
íntegros; el ledger no modifica decisiones por esa diferencia.

## Cancelaciones, posiciones y rondas

Múltiples fills por Order y múltiples TP reemplazados cuentan cada FillId una
vez. Una confirmación de cancelación o un cierre estratégico no vende nada.
Al terminar el intervalo se conserva cualquier exposición abierta, incluso Base
cancelada parcial; no se fuerza un cierre ni se traslada costo entre RoundIds.

RoundLedger conserva cantidades BUY/SELL, nocionales por lado, cantidad/costo/
promedio abiertos, PnL bruto, conciliación y closed_at opcional. El runner observa
RoundRecords que el motor declara CLOSED y exige cantidad/costo exactamente cero.
Solo entonces registra LedgerClosure. Una posición plana sin esa declaración no
finge cierre estratégico. CLOSED con exposición o sin asientos bloquea; un Fill
nuevo posterior al cierre requiere conciliación, nunca reapertura silenciosa.
El historial y los hechos rechazados se conservan en memoria.

## Idempotencia, identidad y timestamps

FillId determina el único asiento económico. Repetir Fill idéntico con misma
vinculación inmutable/procedencia es idempotente, incluso si cambia received_at
o el snapshot acumulado de Order. Cambiar Fill, IntentId, RoundId, Symbol, Side,
OrderId, origen, created_at de Order o adapter_order_id produce conflicto.
OrderId/IntentId tienen vinculación uno-a-uno entre todos los asientos/rondas.
El vínculo Fill→Order se comprueba también en FillEvidence de dominio.

```text
order.created_at <= fill.timestamp <= received_at == reloj_del_runner
```

Fill.timestamp es ocurrencia económica; received_at es conocimiento. Delivery
latency se conserva. No hay atribución retroactiva ni consultas de mercado.
Una entrega prematura bloquea sin guardar el Fill futuro como asiento o journal.

Política `REJECT_LATE_USE_EXPLICIT_REPLAY_SEQUENCE_FOR_TIES`: para cada ronda, los
fills nuevos deben avanzar en `(fill.timestamp, ReplayFact.sequence)` respecto
al anterior. No se reordena el pasado, ni siquiera si hoy parecen conmutar dos
BUY. Una ocurrencia anterior o un empate sin secuencia creciente bloquean.
Para empates modelados, sequence conserva la hipótesis causal del simulador;
para fixtures expresa su orden sintético declarado. No prueba el orden real de
Binance. La etiqueta REAL_OBSERVED se reconoce pero esta política offline no la
acepta: requiere otro contrato futuro con evidencia verificable de secuencia.
Un duplicado idéntico no se convierte en evento económico tardío nuevo.

## Integración, snapshots e integridad

ReplayRunner añade únicamente `ledger=None`; mismo runner, coordinador, motor,
Quotes y simulador. FinancialLedger.receive se llama exclusivamente en `_fact`
al recibir FillEvidence. La conciliación de cierres ocurre después de integrar
cada resultado estratégico, incluyendo una Order terminal sin Fill nuevo.
Los fallos del ledger bloquean contabilidad y el replay, sin inventar conflictos
financieros dentro del estado de ejecución simulada.

ReplaySnapshot añade ledger_identity y LedgerState completo: policy, entries,
FillIds derivados, RoundLedgers, journal de FillEvidence/closures, conflictos y
cursor de recepción. En modo activo todos entran en el checksum; la huella incluye
identidad y transiciones financieras. Sin ledger los campos nuevos None se omiten,
preservando las huellas/representaciones de 4A-2B.

Restaurar exige mismo ledger/política/versión, request/configuración, fuentes,
dataset/modelo y sesión SQLite. El ledger reconstruye la contabilidad desde su
journal y compara el estado entero: costos, cantidades, asientos, IDs, cierres y
bloqueos. Cambiar costos incluso con checksum recalculado se rechaza. El checksum
es integridad accidental, no firma criptográfica de autenticidad.
Continuo == fragmentado incluye informes completos, asientos, estado, costos,
PnL, IDs, métricas y huellas; se prueban entregas pendientes y cancelaciones TP.
No hay serialización, recovery persistente entre procesos ni recuperación LIVE.

## Procedencia y disponibilidad del informe

`ReplayReport.financial_ledger` expone:

- gross_realized_pnl_usdc, open_quantity y open_cost_basis_usdc.
- gross_buy_notional_usdc, gross_sell_notional_usdc y round_ledger.
- entries, processed_fill_ids, reconciliation_status, conflicts, provenances e identity.
- modeled_results=True si participa el simulador o se reconocen fills MODELED_EXECUTION.
- net_realized_pnl, unrealized_pnl, account_equity, drawdown, funding, fees y
  liquidation_price: todos **NOT_AVAILABLE**, nunca cero inventado.

MODELED_EXECUTION y SYNTHETIC_FIXTURE quedan diferenciados por asiento y reporte.
La procedencia declarada no demuestra autenticidad; fixtures y resultados del
simulador no son operaciones observadas ni rentabilidad financiera validada.

Sin ledger: `financial_results=FINANCIAL_RESULTS_NOT_AVAILABLE`, JSON anterior.
Con ledger reconciliado: `PNL_BRUTO_REALIZADO_DISPONIBLE`, incluso con exposición
abierta o cero fills (PnL bruto confirmado cero, ninguna valoración de posición).
Con bloqueo/interrupción: `PNL_BRUTO_REALIZADO_BLOQUEADO`; los importes conservados
son el último prefijo aceptado, no el resultado válido del período completo.

## Ejemplos reproducibles y límites

A: BUY 10×100, SELL 10×101 → PnL bruto 10, cantidad/costo abiertos cero.
B: BUY 10×100, BUY 10×90, SELL 10×97 → promedio previo 95, PnL 20,
cantidad 10, costo 950, promedio abierto 95.
C: BUY 10×100, SELL 5×110, BUY 5×80 → PnL 50, cantidad 10,
costo 900, promedio móvil 90; no reintroduce el costo ya vendido.
D: Base parcial cancelada conserva cantidad/costo y bloqueo estratégico.
E: TP viejo parcial, cancelación y sustituto cuentan solo los fills únicos.
F: Base+DCA1–DCA7+TP→CLOSED concilia cantidad/costo cero y PnL bruto contra
cálculo independiente de flujos de los fills sintéticos.

PnL bruto excluye fees/funding y no es PnL neto, retorno sobre capital, patrimonio
o beneficio certificado. No existe mark válido para valorar exposición, capital
inicial/saldos para equity, serie de equity para drawdown, ni brackets/garantías
de margen para liquidación. El presupuesto 1200 USDC sigue siendo presupuesto de
órdenes, no patrimonio. Rondas/asientos crecen en memoria; no es persistencia
operativa de varios años.

Pendiente para 4A-3B: contrato verificable de fees/funding y unidades/tiempos,
ledger de cash/saldos y capital, mark causal, contabilidad de costos/neto,
equity/drawdown y reglas de conciliación para evidencia externa tardía. Margen,
liquidación/Add Margin y LIVE requieren contratos/validación independientes.
No se habilita LIVE ni se descarga histórico para completar esta fase.

## Extensión opt-in — Fase 4A-3B

FinancialLedger y sus contratos permanecen intactos. Un componente independiente
`FinancialPerformance` consume su estado confirmado y evidencia offline de costos,
funding, capital, transferencias y Mark; publica métricas por disponibilidad sin
alterar el informe bruto ni fingerprints cuando está desactivado. Véase
[FINANCIAL_PERFORMANCE_SPEC.md](FINANCIAL_PERFORMANCE_SPEC.md).
