# Margen aislado y riesgo offline — Fase 4A-4A

Componente opt-in de evaluación; runtime **STOPPED**. No implementa liquidación,
Add Margin, transferencias, órdenes, retenciones de órdenes pendientes ni acceso
a red. Base aprobada: `f2f61330c2b66276af1235c506358d7b2a12eb2a`.

## Arquitectura

```text
SQLiteHistory → ReplayRunner → único StrategyCoordinator(AltBEngine)
                                  ↕ SimulatedExecution
                            FinancialLedger (FillIds confirmados)
                                  ↓
            FinancialPerformance ← FinancialSource / MarkPriceSource
                                  ↓ RiskContext causal
                     IsolatedMarginRisk ← RiskSource
                                  ↓
                 RiskEvaluation / RiskReport / ReplaySnapshot
```

`replay/isolated_margin_risk/` separa `policy`, `evidence`, `models`, `valuation`,
`solver` y `core`. No depende del motor estratégico, de adaptadores de ejecución
ni del cliente Binance. FinancialLedger y FinancialPerformance permanecen
intactos: no se modifica ni duplica la lógica de fills, costo promedio, P0, TP,
fees, funding o wallet general. Sólo consume sus estados confirmados.

Activar requiere entregar explícitamente `risk=IsolatedMarginRisk(policy, source)`
al ReplayRunner, junto con `ledger` y `performance`. El componente está
desactivado por defecto; sus campos opcionales `None` se omiten del encoding.
NoQuotes, FixtureQuotes y fingerprints anteriores se conservan. El fixture de
compatibilidad se obtuvo ejecutando el main aprobado archivado, antes de editar.

## Contrato, unidades e identidad

Contrato único 1000SHIBUSDC, USDC, LONG, ISOLATED, ONE-WAY, leverage Decimal 4.
Los Fill quantities son unidades contractuales: no se multiplican por 1000 ni
por leverage otra vez. El motor Alt B conserva Base+DCA1–DCA7, TP 0.80% y el
presupuesto canónico 1200 USDC. Presupuesto de órdenes, capital de cuenta,
reserva externa, collateral asignado y equity son conceptos separados.

Todas las operaciones financieras usan el contexto contable independiente de
50 dígitos, ROUND_HALF_EVEN, exponentes amplios y traps explícitos; el contexto
global de Decimal no puede alterar resultados. No se aceptan float ni infinitos.

La identidad SHA-256 incluye todos los parámetros de MarginAllocationPolicy,
referencia/dataset/supuestos y la identidad de RiskSource. ReplaySnapshot además
fija dataset/sesión SQLite, QuoteSource, ejecución, ledger y performance, cuya
identidad ya contiene MarkPriceSource. Cambiar cualquiera impide reanudar.

## Evidencias

Cada evidencia contiene un `RiskStamp` obligatorio: event_id estable, símbolo,
moneda, effective_at UTC, received_at UTC, sequence no negativa, Reference
(fuente, dataset, procedencia, supuestos) y versión de reglas de su fuente.
effective_at <= received_at. Los contratos canónicos son validados al construir.

| Contrato | Contenido / significado |
| --- | --- |
| IsolatedMarginEvidence | Balance absoluto de wallet aislado, asignación inicial declarada, lista exacta y ordenada de FillIds a su instante económico; importes cumulativos fees/funding ya incluidos |
| MarginAdjustmentEvidence | ADD, WITHDRAW o SELL_RELEASE explícito, importe positivo, ronda; SELL_RELEASE exige FillId SELL causal |
| MaintenanceBracketSchedule | Brackets de ese símbolo/USDC, vigencia y fórmula/fronteras declaradas; la metadata del schedule aplica a cada bracket |
| RiskValuationEvidence | Solicitud explícita de sensibilidad: aporte, buffer objetivo, reserva disponible y posibilidad hipotética de transferencia opcionales |
| MarginAllocationPolicy | Reglas de asignación, liberación, costos, PnL realizado y sensibilidad, identidad y versión |

Procedencias: OBSERVED, MODELED, SYNTHETIC_FIXTURE y UNKNOWN. Se reutiliza Reference
de 4A-3B: OBSERVED está **rechazado**, exige un contrato futuro independiente de
autenticidad, cobertura y vigencia. Cambiar una etiqueta no habilita observaciones
de Binance. UNKNOWN se conserva en evidencia pero no autoriza importes de margen,
Mark, mantenimiento o hipótesis. Una política debe declarar hipótesis conocida.

FixtureRiskSource es un port offline ordenado por `(received_at, sequence)`,
permite sólo SYNTHETIC_FIXTURE y no repite secuencias. Su identidad incluye todos
sus hechos. Una implementación de RiskSource puede aportar MODELED o UNKNOWN,
pero conserva las mismas reglas y límites; no se incluye transporte de red.

Los hechos de posición requieren una ronda con fills confirmados y no pueden
tener instante económico anterior al primer Fill. No se implementa asignación
previa a una ronda inexistente ni recovery LIVE.

## Políticas obligatorias, sin defaults económicos

| Campo | Opciones implementadas |
| --- | --- |
| initial | EVIDENCE_ONLY / EXECUTED_NOTIONAL_OVER_LEVERAGE |
| release | EXPLICIT_ONLY / PROPORTIONAL_INITIAL_ON_SELL |
| costs | UNKNOWN / RECONCILED_TO_ISOLATED / INCLUDED_IN_BALANCE_EVIDENCE |
| realized | UNKNOWN / TO_ISOLATED / OUTSIDE_ISOLATED |
| threshold_solver | bool explícito |
| linear_additional_margin | bool explícito |
| reference | Referencia y explicación obligatoria de supuestos |
| rules_version | isolated-long-50-v1; otras versiones rechazadas |

Objeto frozen validado; no lee entorno, archivos secretos ni configuración del
exchange. No contiene tasas de mantenimiento, latencias de transferencia,
reservas ni capital implícitos. EVIDENCE_ONLY exige EXPLICIT_ONLY y no admite
RECONCILED_TO_ISOLATED: el balance absoluto ya tiene sus costos incluidos.

### Asignación modelada por fills

```text
asignación de cada BUY = notional efectivamente ejecutado / 4
initial_assigned = suma de esas asignaciones
principal restante = asignaciones menos liberaciones modeladas
```

Es una **hipótesis MODELED**, no una transferencia acreditada ni una reproducción
completa de Binance. No acredita fondos libres suficientes ni retenciones,
redondeos o reservas para órdenes pendientes. Siempre pending_order_reserve y
free_capital = NOT_AVAILABLE. No se asignan 1200 USDC antes de llenar órdenes.

PROPORTIONAL_INITIAL_ON_SELL libera sólo principal inicial, por la fracción de
exposición realmente vendida (`q_sell / q_open_before`). Con cantidad cero libera
exactamente el principal restante; el acumulado se liga al principal para evitar
residuos por reagrupación de parciales. MarginTransition conserva FillId, ronda,
disponibilidad, asignación, liberación y principal después de cada fill.
EXPLICIT_ONLY no libera principal automáticamente.

ADD/WITHDRAW son evidencia de un ajuste ya declarado por la fuente, no acciones
emitidas por el motor. SELL_RELEASE requiere venta confirmada y sólo EXPLICIT_ONLY;
la misma venta no puede liberar dos veces ni coexistir con liberación proporcional.
No se libera collateral por una cancelación solicitada o confirmada.

### Wallet aislado, PnL y costos

Con asignación modelada y RECONCILED_TO_ISOLATED:

```text
isolated_wallet_balance = initial_assigned - initial_released
                       + ADD - WITHDRAW - SELL_RELEASE
                       + realized_assigned - fees_assigned + funding_assigned
```

Los costos se toman una sola vez de las métricas **por ronda** ya conciliadas en
FinancialPerformance. Funding conserva su signo. Datos incompletos, fee de moneda
no convertida o cobertura funding pendiente impiden producir wallet conocido.
UNKNOWN para costos impide asignar fees/funding y producir wallet aislado.

TO_ISOLATED acredita el bruto realizado ya conciliado; OUTSIDE_ISOLATED lo mantiene
fuera de la partición aislada bajo hipótesis explícita. UNKNOWN impide valorar
wallet después de una venta. Ninguna opción cambia el wallet general.

Con EVIDENCE_ONLY se exige balance absoluto con instante económico y recepción
exactamente en el instante evaluado, ligado al conjunto exacto de FillIds actuales
y fees/funding incluidos coincidentes con los reconciliados. Los costos
no se restan otra vez. Si faltan importes, cobertura o nueva evidencia después de
un cambio de exposición, wallet = NOT_AVAILABLE. Costos explícitos contradictorios
son conflicto. Un balance puntual no prueba ausencia de ajustes o transferencias
posteriores: requiere nueva evidencia para otro instante, incluso con los mismos
fills y costos. Un ajuste posterior requiere un nuevo balance absoluto; no se
extrapola el snapshot. Ajuste/balance simultáneos sin conciliación se rechazan.

Account wallet/equity se muestran sin cambios desde FinancialPerformance. El
wallet aislado es una **partición modelada**, no un segundo balance sumable al
capital general. No se vuelve a descontar collateral ni costos de account_wallet,
ni se calcula una reserva como wallet menos presupuesto. No se concilian
transferencias automáticas entre particiones en esta fase.

## Brackets de mantenimiento

Cada MaintenanceBracket exige Decimal floor, cap, rate, maintenance_amount/cum y
max_leverage opcional. La metadata de símbolo, vigencia, procedencia, fuente,
dataset y versión se hereda de su schedule. Validación:

- floor >= 0, cap > floor; 0 <= rate < 1, cum >= 0.
- Mantenimiento no negativo desde floor; max_leverage positivo si existe.
- Orden y continuidad de **rangos**, sin solapamientos o gaps entre brackets.
- Frontera floor inclusivo / cap exclusivo; cap final no está cubierto.
- Vigencia `[valid_from, valid_until)` y schedules sin vigencias solapadas.
- Cobertura del notional evaluado y leverage compatible, sin extrapolación.

Puede existir cobertura limitada (floor inicial > 0 / cap final finito) o un salto
de la función de mantenimiento. Ninguna regla inventa cobertura fuera del schedule.
Saltos pueden impedir una raíz única; el solver entonces se abstiene.

Única fórmula admitida, explícitamente contratada:

```text
position_notional = open_quantity × Mark
maintenance = position_notional × maintenance_rate - maintenance_amount
```

El schedule sólo se usa después de recibirlo y dentro de su vigencia; no se
reutilizan condiciones actuales en fechas anteriores ni brackets BTCUSDT/USDT.
No hay MMR ni brackets oficiales incluidos. El rate 0.01 de tests es **sintético**.
Sin schedule aplicable o cobertura = maintenance NOT_AVAILABLE, condición UNKNOWN.

## Mark, buffer y disponibilidad

Único precio: MarkEvidence reconocido por FinancialPerformance desde su
MarkPriceSource, recibido hasta el reloj simulado. Se exige símbolo correcto,
procedencia conocida, continuous=True y vigencia en milisegundos explícita.
No se sustituyen datos faltantes por Last/OHLC/Quote/P0/TP ni se interpolan futuros.

```text
unrealized = open_quantity × Mark - open_cost_basis
isolated_margin_balance = isolated_wallet_balance + unrealized
buffer = isolated_margin_balance - maintenance
margin_ratio = maintenance / isolated_margin_balance  # sólo balance > 0
```

Metric distingue AVAILABLE, MODELED, NOT_AVAILABLE y sus causas; posición añade
INCOMPLETE cuando sólo algunos insumos son conocidos y BLOCKED ante conflicto.
Las fórmulas y asignaciones modeladas conservan procedencia MODELED. El buffer
clasifica ABOVE_MAINTENANCE (>0), AT_MAINTENANCE_THRESHOLD (=0), BELOW_MAINTENANCE
(<0) o UNKNOWN. Balance cero/negativo nunca produce un ratio positivo engañoso.

Cuando cantidad abierta es exactamente cero: NO_OPEN_POSITION, notional/PnL
flotante/mantenimiento cero y umbral NOT_AVAILABLE, aunque falte Mark. Esto no
prueba liberación total del margen adicional, conciliación estratégica CLOSED
ni una liquidación. Wallet desconocido puede seguir desconocido tras el cierre.

Base shortfill mantiene cantidades/costo derivados de sus FillIds, incluso si
la estrategia bloquea P0/DCA/TP. DCA/TP parciales alteran exposición y principal
únicamente por fills reales del simulador. Cancelaciones y reemplazos TP no
alteran exposición por sí solos. No se calculan cantidades solicitadas como fills.

## Solver opcional y aporte hipotético

Nombre exclusivo: **MODELED_MAINTENANCE_THRESHOLD_PRICE**. Con wallet W fijo,
cantidad Q, costo C y cada bracket r/cum:

```text
W + Q*p - C = Q*p*r - cum
p = (C - W - cum) / (Q × (1 - r))
```

Se recalcula Q*p y se rechazan raíces no positivas, fuera de su bracket, sin
leverage aplicable o no verificables. Se recorren todos los brackets, incluidas
sus fronteras; sólo una raíz distinta es aceptable. Validación del residuo lineal
equivalente en Decimal50: cero representable o signos que encierran cero entre
vecinos representables. No hay tolerancia económica oculta ni interpolación
histórica. Si no hay solución única verificable, NOT_AVAILABLE. Distancia =
Mark actual válido menos umbral, en USDC/precio por unidad contractual.

No representa precio oficial de liquidación: faltan retenciones/redondeos,
liquidation fees, reglas operativas del exchange y trayectoria temporal. Puede
evaluar sensibilidad del contrato sin Mark actual; eso no habilita buffer actual.

RiskValuationEvidence se evalúa al instante de su **disponibilidad** y sólo aparece
en ese punto; no se reaplica silenciosamente sobre una exposición futura distinta.
Requiere linear_additional_margin=True para asumir W' = W + aporte sin otros
efectos pendientes. Registra buffer y umbral antes/después, balance hipotético y si el aporte alcanza
el objetivo (`target_met_by_hypothesis`), además de:

```text
required_for_target = max(0, target_buffer - current_buffer)
reserve_required = required_for_target  # si se especificó objetivo
                   o importe hipotético # si sólo se especificó aporte
```

Feasibility UNKNOWN si reserva desconocida o transferencia no supuesta posible;
INSUFFICIENT_MODEL_RESERVE si reserva explícita menor al mínimo;
POSSIBLE_UNDER_EXPLICIT_ASSUMPTIONS si ambas hipótesis lo permiten. Estas etiquetas
no acreditan llegada oportuna del capital. La factibilidad de la reserva mínima
no afirma que un aporte explícito menor alcance el objetivo; el indicador de
objetivo y el buffer posterior distinguen ese caso. El aporte no genera eventos de ajuste,
no modifica wallet, no autoriza transferencias y no cambia órdenes.

## Tiempo, conflictos y reanudación

Se conserva el orden causal aprobado del runner: controles/hechos, transiciones
de simulador, cierre/gap, acción estratégica, entregas pendientes; después
FinancialPerformance reconoce costos/Marks y valora, y finalmente riesgo reconoce
su contexto, eventos `(received_at, sequence)` y una evaluación. Un evento de
riesgo puede añadir un punto temporal explícito al replay financiero sin cambiar
órdenes, fills ni decisiones estratégicas. Cortar un fragmento no crea muestras.

El calendario offline pendiente puede contener datos futuros, como en 4A-3B;
no son contexto disponible ni autorizan decisiones antes de su recepción. No se
leen OHLC o Marks futuros para valorar el instante actual.

EventId económico repetido con idéntico contenido es idempotente; retransmisión
puede cambiar recepción/sequence pero no el hecho. Cambiar contenido bloquea.
Hechos económicos duplicados bajo otro ID, schedules solapados, liberación doble,
hipótesis simultáneas, contextos contradictorios o hechos nuevos en un instante
ya valorado también bloquean. Una evidencia monetaria tardía con instante <= una
asignación ya publicada bloquea y conserva el prefijo válido; no corrige la serie.

Snapshot incluye policy/identity, contexto con ledger/performance cursor/Mark,
eventos (balances, ajustes, brackets, hipótesis), evaluaciones y transiciones de
principal, journal/conflictos, pending/cursor y checksum global. La restauración
reconstruye desde el **journal financiero y RiskSource originales**, no desde
un journal de riesgo auto-consistente pero reescrito. Compara exactamente estado,
muestreo, journal y número de entregas intentadas, incluso ante conflicto. Se
rechazan snapshots adulterados aunque recalculen checksum.

Continua == fragmentada: estado estratégico, ejecución, ledger, financiero,
margen, ajustes, MarkIds, FillIds, evaluaciones, bloqueos, emisiones y huella
SHA-256. Conflicto de riesgo no contamina el ledger ni performance; conflicto
financiero bloquea riesgo dependiente. No se permite reanudar con otra sesión
SQLite ni se añade serialización/recovery de procesos o LIVE.

## Ejemplos sintéticos exactos

BUY 10 @ 100; leverage4; margen inicial modelado250; fees/funding reconciliados0;
política explícita de asignación, schedule sintético1%/cum0:

| Caso | Mark | PnL flotante | Wallet aislado | Balance margen | Maintenance | Buffer |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A/B | 95 | -50 | 250 | 200 | 9.50 | 190.50 |
| C | 75 | -250 | 250 | 0 | 7.50 | -7.50 |
| D, sensibilidad +100 | 75 | -250 | 350 hipotético | 100 hipotético | 7.50 | 92.50 |

C es BELOW_MAINTENANCE; no se produce Fill SELL ni cierre. En D el estado real
modelado sigue W=250. Objetivo de buffer100 requiere107.50 bajo la hipótesis
lineal; factibilidad UNKNOWN sin reserva/posibilidad declaradas.
E sin Mark: buffer indisponible. F sin brackets: maintenance indisponible y
condición UNKNOWN. G prueba Base, siete DCA, DCA/TP parciales, TP viejo ejecutado
antes de cancelación y reemplazos/cierre, conservando FillIds. H plano: riesgo
NO_OPEN_POSITION aunque el collateral adicional pendiente de liberar sea distinto
del PnL realizado. Todos son fixtures, no resultados históricos certificados.

## Muestreo y límites

El informe separa incumplimiento evaluado AT/BELOW en muestras, posible
incumplimiento entre muestras y datos insuficientes. Informa sampled_breaches,
causas de métricas no disponibles, procedencias, hipótesis, IDs y conflictos.
Siempre liquidation=NOT_SIMULATED, survival=NOT_DETERMINABLE_FROM_SAMPLES,
interval_risk=POTENTIAL_BREACH_BETWEEN_SAMPLES_UNKNOWN. Incluso Mark continuo como
contrato de calidad no suministra una trayectoria intraminuto completa. Nunca se
concluye supervivencia histórica a partir de muestras1m.

No implementa fees de liquidación, prioridad del exchange, revaluación de órdenes
abiertas, capital libre, transferencias, garantías de solvencia o matching real.
No calcula otro backtest financiero ni descarga datos históricos.

## Referencias conceptuales y requisitos siguientes

Referencias públicas proporcionadas para el contexto conceptual (no se consultó
ni descargó contenido Binance en esta implementación):

- https://www.binance.com/en/support/faq/detail/b3c689c1f50a44cabb3a84e663b81d93
- https://www.binance.com/en/support/faq/detail/360038447311
- https://www.binance.com/en/support/faq/detail/360033525271

**No proporcionan por sí solas brackets históricos específicos de 1000SHIBUSDC**
ni acreditan las hipótesis implementadas. Se necesita futura evidencia autenticada
del contrato USDC, vigencia histórica de brackets, reglas de redondeo/retenciones,
reservas de órdenes y atribución de fondos/costos, Mark con cobertura suficiente.
Para 4A-4B deberán aprobarse límites/reservas, factibilidad, latencias, cola y
confirmación de transferencias, contabilidad entre particiones e idempotencia.
Auto-Add Margin ON no garantiza solvencia ni llegada de fondos a tiempo.
Nada de 4A-4B se implementa aquí; entrega para auditoría, NO MERGE/DEPLOY/TRADING.
