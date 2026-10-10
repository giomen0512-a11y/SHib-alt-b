# Reservas y transferencias de margen simuladas — 4A-4B

Base contractual: `ab4604b40ba66d837bed9114ecdf585b2961db72` (4A-4A).
Implementación offline, opt-in y determinista. Runtime **STOPPED**.
No hay cliente, comando ni conexión de transferencias reales.

## Arquitectura y activación

`ReplayRunner(..., reserve_controller=ReserveTransferSimulation(...))` activa el
módulo `replay/reserve_transfer_simulation`. No hay activación por CLI ni por una
preferencia de Auto-Add Margin del exchange. El argumento ausente conserva los
informes, snapshots y fingerprints de 4A-4A: los campos nuevos `None` se omiten del
encoding canónico. Crear el controller exige políticas explícitas; su presencia
activa la auditoría de reservas. `AutoMarginPolicy.enabled` es `False` por defecto:
las decisiones automáticas requieren `True`. Las acciones de fixtures sólo se
aplican cuando una fuente de fixtures se proporciona expresamente.

El controller consume órdenes **recibidas**, solicitudes de cancelación, ledger,
performance y riesgo. No consulta entregas futuras del simulador. No calcula
fills, estrategia, PnL, exposición, mantenimiento o umbrales alternativos. El
motor AltB y Base/DCA1–DCA7/TP0.80% permanecen intactos. El contrato es LONG,
ONE-WAY, ISOLATED, 1000SHIBUSDC, USDC, 4x. El presupuesto estratégico de 1200 USDC
no se utiliza como wallet ni como reserva. No se reaplican 4x ni el factor1000.

`policy.py` declara supuestos; `evidence.py` capital/precios/acciones de fixtures;
`orders.py` concilia órdenes y remanentes; `accounting.py` conserva las partidas;
`models.py` contiene estados inmutables; `core.py` coordina transferencias,
latencias, cola y decisiones. No añade dependencias.

## Evidencia y capital inicial

Los eventos usan `RiskStamp`: identidad, hora efectiva, recepción, secuencia,
símbolo, moneda, versión y `Reference` con fuente, dataset, procedencia y supuestos.
Cada evento de posición lleva su RoundId; las observaciones agregadas de cuenta
no requieren uno. El journal registra recepciones, conciliaciones, decisiones,
retenciones y todas las transiciones económicas. El tiempo de una observación es
su disponibilidad causal; el hecho original conserva sus timestamps en el payload.

Se admiten hipótesis MODELED, fixtures SYNTHETIC_FIXTURE y evidencia UNKNOWN que
no acredita disponibilidad. OBSERVED continúa prohibido por `Reference`.
`FixtureReserveSource` exige SYNTHETIC_FIXTURE, secuencias únicas y orden estable;
no se acepta como un adaptador real. No hay valores oficiales implícitos.

`CapitalPartitionEvidence` exige wallet, saldo libre, asignaciones por ronda,
holds por OrderId, otras obligaciones nombradas y FillIds iniciales explícitos.
El controller no tiene capital predeterminado. Una posición inicial exige
partición que coincida con el contexto confirmado de 4A-4A y sus FillIds; no se
acredita dinero por declarar únicamente una posición. Las órdenes iniciales
pendientes necesitan snapshots explícitos. En ReplayRunner, la evidencia inicial
se recibe exactamente en `request.start`; las órdenes iniciales no pueden ser
futuras. La disponibilidad permanece desconocida hasta conciliar el contexto.

## Contabilidad entre particiones

La modalidad sintética implementada es `RECONCILED_WALLET_DELTAS`:

```
committed = isolated_wallet_allocated + pending_order_holds
            + pending_transfer_holds + other_accounted_wallet_buckets
account_wallet = free_balance + committed
```

Las partidas son partes del wallet, no fondos independientes. La primera
conciliación compara exactamente la declaración de capital con wallet,
asignaciones, órdenes y FillIds confirmados. Después, sólo cuando **todas** las
partidas son conocidas, se concilia el libre desde wallet menos compromisos
reconocidos. Es conservación equivalente a los movimientos de esas partidas;
no añade una obligación residual para suplir datos ausentes. Conciliar saldos
reconocidos evita acumular redondeos de diferencias entre valoraciones.

Se usa `ledger_context()`: Decimal precisión50, HALF_EVEN, sin depender del
contexto Decimal del llamador. La igualdad se evalúa agrupando los compromisos
como arriba. `EXACT_DECIMAL` significa sin redondeo adicional a centavos/ticks ni
ajustes del exchange; las operaciones siguen la precisión50 del contrato previo.
No se afirma igualdad de aritmética infinita entre agrupaciones distintas.

Wallet viene de FinancialPerformance. Los wallets aislados, incluyendo costos y
realizados según la política seleccionada, vienen de IsolatedMarginRisk. Fees,
funding y realizados no se vuelven a calcular ni se descuentan dos veces. PnL
flotante pertenece a equity y no se acredita como saldo transferible. El estado
retiene la última partición conciliada para recuperar disponibilidad cuando
llegue evidencia faltante; no publica ese saldo como disponibilidad actual.

Si faltan capital, costos, holds o asignación, existe conflicto, falta conciliación
inicial o el saldo libre/asignación resulta negativo:
`PARTITION_NOT_RECONCILED`, libre `NOT_AVAILABLE`. No se modifica una ejecución
confirmada para hacerla financiable. Otras obligaciones permanecen explícitas.

## Retenciones y liberaciones de órdenes

`OrderMarginReservation` contiene la orden y su intención (OrderId, IntentId,
RoundId, Side, OrderType, cantidad y reduce_only), cantidad ejecutada por FillId,
remanente, precio, aceptación, cancelación solicitada, conciliación y hold.

Hipótesis MODELED única: `remaining_notional / leverage`, con el leverage4 de
4A-4A. LIMIT usa el límite bajo esa hipótesis. MARKET requiere elegir explícitamente
`EXPLICIT_PRICE_EVIDENCE` o `CAUSAL_FRESH_MARK`. El primero exige evidencia de precio
por OrderId con recepción y vigencia; el segundo sólo utiliza un Mark conocido,
continuo, vigente y de procedencia conocida. No usa Close ni un Mark futuro.
No modela fees futuras ni otras retenciones del exchange.

PENDING no aceptada y REJECTED no reservan margen de apertura. OPEN y
PARTIALLY_FILLED aceptadas retienen sólo el remanente. SELL reduce_only retiene
cero margen de apertura LONG; otras semánticas de exposición quedan desconocidas.
El estado acumulado de la orden debe coincidir con los FillIds del ledger: un
FILLED/CANCELED sin todos sus fills deja el hold `NOT_AVAILABLE`, sin liberación
prematura. Una intención por sí sola no asigna margen ni produce OrderId.

Cancel_request sólo marca la solicitud. FILLED, REJECTED o CANCELED conciliados
liberan el remanente. Fill parcial mueve únicamente el margen correspondiente al
fill confirmado según 4A-4A; el resto mantiene su hold. Una diferencia de precio
puede requerir libre adicional: si no existe, la partición queda no conciliada.
Una cancelación posterior conserva la exposición ejecutada. Estados tardíos o
contradictorios tras un terminal se bloquean; retransmisiones idénticas son inocuas.

Las ventas confirmadas liberan principal conforme a la política de 4A-4A. No se
inventa una venta, una liberación de collateral adicional ni un retiro al cerrar.
El saldo aislado adicional/costos que 4A-4A conserve sigue en esa partición; si
resulta negativo, no se publica libre disponible. Liberar holds o principal no
crea PnL ni cambia por sí mismo el wallet.

## Política automática

Todos los campos económicos y temporales son obligatorios, salvo `enabled=False`:

| Campo | Semántica |
|---|---|
| trigger_buffer_usdc | Solicitar sólo cuando buffer <= trigger. |
| target_buffer_usdc | Necesidad completa = max(0,target-buffer). |
| minimum_transfer_usdc | Importe mínimo admisible; no se eleva silenciosamente una necesidad menor. |
| maximum_transfer_usdc | Límite por solicitud; exceso se rechaza, no se fracciona. |
| max_per_round_usdc | Suma de aportes confirmados y solicitudes pendientes de la ronda. |
| max_simultaneous_requests | Capacidad global de solicitudes no terminales. |
| request_latency_ms | Desde autorización/hold hasta envío simulado. Puede ser cero. |
| confirmation_latency_ms | Desde envío hasta confirmación modelada. La suma de latencias debe ser positiva. |
| timeout_ms | Desde propuesta; si coincide con confirmación, prevalece vencimiento. |
| retry_policy | Sólo NO_RETRY. Tras rechazo/vencimiento no se genera otro aporte automático para esa ronda. |
| rounding_policy | Sólo EXACT_DECIMAL bajo precisión50; sin cuantización externa. |
| source_reference | MODELED, con fuente/dataset/supuestos declarados. |

Antes de autorizar se exige posición abierta, contexto causal, margen conocido,
Mark vigente, bracket aplicable, buffer, libre, holds conciliados, moneda USDC,
ausencia de conflictos, límites y ausencia de otra solicitud pendiente de la ronda.
Una necesidad de100 con libre60 se rechaza íntegramente. No hay aportes parciales.
La asignación EVIDENCE_ONLY no se extrapola al confirmar: esta modalidad no
habilita transferencias automáticas. La confirmación requiere asignación por fills
y costos reconciliados de 4A-4A; desconocimiento no se convierte en autorización.

Las razones distinguen ALLOCATION_UNKNOWN, INSUFFICIENT_EVIDENCE,
INSUFFICIENT_FREE_BALANCE, PENDING_ORDER_HOLDS y TRANSFER_LIMIT_EXCEEDED /
TRANSFER_NOT_ALLOWED. Políticas no implementadas se rechazan al construirlas.
Mientras exista una petición pendiente no se crea otra para la misma ronda.
`BREACH_ALREADY_OBSERVED` registra una activación con buffer<=0; no implica rescate.

## Transferencias, cola y causalidad

```
PROPOSED → FUNDS_HELD → REQUEST_PENDING → CONFIRMATION_PENDING
                                          ├─ CONFIRMED_SIMULATED
                                          ├─ REJECTED
                                          └─ EXPIRED
```

Una propuesta inválida termina REJECTED sin crear hold. Autorizar mueve libre al
hold de transferencia; no cambia margen. Enviar no mueve collateral. La
confirmación programada es consecuencia de la política de confirmación modelada
explícitamente configurada, no una prueba de que el exchange respondió. Fixtures
pueden proponer/rechazar/expirar o confirmar bajo las mismas validaciones.

Confirmar exige petición en CONFIRMATION_PENDING, latencias cumplidas, timeout no
alcanzado, posición abierta, partición conciliada e importe completo igual al hold.
Se mueve exactamente ese hold a margen aislado y se emite un único
`MarginAdjustmentEvidence(kind=ADD, provenance=MODELED)` con request_id/hold_id,
RoundId y timestamp de confirmación. No se emite TransferEvidence DEPOSIT ni se
utiliza RiskValuationEvidence como dinero. Rechazar/vencer libera el hold una vez,
sin aumentar margen. Cerrar la posición mientras espera obliga a rechazar el aporte.

Cada petición conserva importe original/retenido/confirmado, intento, timestamps,
latencias derivables, motivo terminal, confirmación y balances antes/después.
Los terminales no pueden reconfirmarse ni liberarse nuevamente. Propuestas del
mismo ID y contenido son idempotentes; cambiar la economía del ID se bloquea.

Cola ordenada por `(scheduled_at, sequence, request_id)`. Los IDs se derivan de
políticas/capital, ronda y datos ya recibidos; no hay UUID aleatorios ni reloj real.
La identidad de sesión incluye también la fuente completa para validar recovery,
pero un rechazo futuro no cambia el ID causal de la solicitud anterior.

En cada timestamp: hechos recibidos de órdenes/fills, ejecución ya autorizada,
ledger, fees/funding/Mark, contexto de riesgo y evidencia externa, conciliación de
holds, muestra **antes** de confirmar, terminales de fixtures, cola programada,
VALUE de 4A-4A, buffer posterior y nuevas propuestas. El envío de latencia cero se
procesa después de la nueva propuesta; nunca confirma inmediatamente. Fuente
externa de riesgo precede a ajustes generados. Fixtures terminales preceden a la
cola y timeout gana el empate. Otros solapamientos económicos siguen bloqueados
por las reglas previas; no se relajan los validadores.

Los eventos de cola añaden instantes causales de observación financiera cuando
no coincidían con ticks previos; no alteran los cálculos ni los hechos económicos.
Ante conflicto del controller se conserva el prefijo válido y se interrumpe el
replay. No se publica una transición de confirmación que falló su validación.

## Incumplimientos y límites de interpretación

Durante la espera se usa el margen anterior con Mark, fills y costos reconocidos.
La primera muestra de buffer<=0 se conserva como
`SAMPLED_BREACH_BEFORE_CONFIRMATION`, incluso si el aporte posterior hace positivo
el buffer. Se registran propuesta, confirmación prevista/efectiva, primer
incumplimiento, buffers y monto. Una caída y confirmación del mismo timestamp se
resuelven por la prioridad declarada: la muestra previa permanece en el controller,
aunque VALUE final de 4A-4A ya incluya collateral.

El resultado distingue CONFIRMED_BEFORE_SAMPLED_BREACH,
SAMPLED_BREACH_BEFORE_CONFIRMATION e INSUFFICIENT_EVIDENCE; conserva siempre
UNKNOWN_INTRAMINUTE_PATH y NO_LIQUIDATION_SIMULATED. Continuar tras una muestra de
incumplimiento se etiqueta trayectoria contrafactual condicionada. No se borra la
ronda, no se fabrica SELL/liquidación y no se certifica supervivencia Binance.

## Snapshots y compatibilidad

ReplaySnapshot incorpora identidad del controller y ReserveState: capital,
particiones actual/última conciliada, órdenes/holds, transferencias terminales y
pendientes, cola, evidencia de ajustes, IDs/cursor, muestras, journal/conflictos y
reloj. El snapshot conserva además riesgo, ledger, performance y ejecución.
Checksum SHA256 cubre los campos opcionales cuando están presentes.

Al restaurar un controller activo se reconstruye el replay desde las fuentes
originales y la misma sesión SQLite hasta el reloj declarado y se compara el
snapshot completo. Después se ejecutan los validadores previos. No se confía en
un checksum recalculado ni en un journal reescrito de modo auto-consistente.
Esta reconstrucción tiene costo proporcional al prefijo del replay; no es recovery
persistente ni una optimización para LIVE.

`ConfirmedMarginSource` extiende explícitamente la reconstrucción de 4A-4A con
ADD modelados y vinculados al controller. `IsolatedMarginRisk.validate` acepta el
argumento opcional `confirmations`, manteniendo la reconstrucción financiera y de
fuente original. Los links nuevos de MarginAdjustmentEvidence son opcionales y se
omiten cuando ausentes; no cambian fingerprints previos. Continúan/fragmentados
comparan informes, estados, balances, queue, recepciones, digest y checksum.

## Validación y datos pendientes

Fixtures A–J, fills parciales, cancelaciones, latencias, insuficiencia, costos,
confirmación tardía, breach cero/preexistente, rondas cerradas, fuentes y snapshots
adulterados, contexto Decimal hostil y compatibilidad activa 4A-4A. Ver resultados
y comandos en [PHASE_4A_4B_REPORT.md](PHASE_4A_4B_REPORT.md).

Binance real puede tener otras retenciones, reglas de asignación, comisiones,
latencias, prioridades de liquidación y límites. Faltan evidencia independiente de
reservas/órdenes/saldos reales, MMR histórico del contrato, transferencias durante
volatilidad y trayectoria intraminuto. No se descargan datos ni se calcula
rentabilidad histórica. También quedan fuera aportes parciales, reintentos,
políticas de precio/rounding adicionales, retiro automático de collateral,
liquidaciones y recovery persistente. Ninguna ausencia de breaches muestrales
prueba supervivencia real. Sin merge, despliegue, servicios, Telegram o trading.
