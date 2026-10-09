# Contabilidad, costos y valoración offline — Fase 4A-3B

Base aprobada: `5e8821b3f109f69d47f504c2f5d5a3bb337ba57d`.
Runtime STOPPED. Fuentes offline explícitas, fixtures y sensibilidad modelada;
ningún dato sintético se presenta como histórico observado de Binance.

## Arquitectura y activación

```text
ReplayRunner → StrategyCoordinator(AltBEngine) → SimulatedExecution opcional
    │                ↑ Order / FillEvidence recibidos
    ├─ FinancialLedger 4A-3A intacto: cantidad/costo promedio móvil/PnL bruto
    └─ FinancialPerformance opt-in
           ├─ FinancialSource: fees, funding, transferencias, cobertura
           ├─ MarkPriceSource: evidencia Mark independiente
           ├─ journal contable y vinculación por FillId/OrderId/RoundId
           └─ wallet, neto cash, valoración, serie equity y drawdown
```

`FinancialPerformance` es frozen e independiente de estrategia, ejecución,
riesgo, APIs, persistencia y margen. Reutiliza exclusivamente el ledger aprobado;
no deriva fills, P0, DCA, TP ni un segundo promedio estratégico. FinancialLedger,
AltBEngine, dominio Order/Fill y SimulatedExecution permanecen intactos.

Activación de biblioteca:

```python
from decimal import Decimal
from trading_engine.replay.financial_ledger import FinancialLedger
from trading_engine.replay.financial_performance import FinancialPerformance, PerformancePolicy
from trading_engine.replay.financial_performance.evidence import (
    Reference, Origin, FixtureFinancialSource, FixtureMarks,
)
from trading_engine.replay.financial_performance.policy import CapitalEvidence

reference = Reference("fixture", "synthetic-case", Origin.FIXTURE,
                      "Prueba sintética; no calibración de Binance")
policy = PerformancePolicy(
    scenario="BASE",
    assumptions="Sensibilidad explícita, sin tasas/Mark/calendario implícitos",
    capital=CapitalEvidence(Decimal("3200"), request.start, reference),
)
performance = FinancialPerformance(policy, FixtureFinancialSource(cost_events),
                                   FixtureMarks(mark_events))
runner = ReplayRunner(history, request, ledger=FinancialLedger(), performance=performance)
```

3200 es un ejemplo externo de fixture, nunca un default. `performance=None` es el
modo anterior; requiere `ledger=` explícito al activarse. Ningún modo operativo
nuevo se habilita. Sin capital se puede auditar bruto/costos, pero wallet/equity
son NOT_AVAILABLE. El capital del policy es evidencia preconfigurada, conocida al
inicio evaluado, con effective_at exactamente request.start.

Campos nuevos de snapshot/report llevan `omit_if_none`; no se agregan eventos,
JSON o checksum al modo desactivado. Cuatro vectores comprueban compatibilidad:
tres anteriores sin ledger y uno congelado desde la fuente exacta del main de
4A-3A con ledger activo. NoQuotes y FixtureQuotes conservan su contrato.

## Contrato, unidades y precisión

1000SHIBUSDC, LONG, futuros lineales USDC. Quantity usa unidades contractuales;
notional es cantidad × precio USDC. No se vuelve a multiplicar por 1000 ni por
apalancamiento 4. Presupuesto de órdenes, reserva de capital, wallet, equity,
margen y margen libre siguen siendo conceptos separados.

Todo importe, cantidad, tasa y precio exige Decimal finito; float/bool/texto no
se convierten silenciosamente. Context local nuevo: 50 dígitos, ROUND_HALF_EVEN,
exponentes extendidos y traps de InvalidOperation/DivisionByZero/Overflow.
No hereda ni altera el contexto global ni cuantiza a centavos. Nocionales
positivos o fees/funding no nulos redondeados a cero por underflow bloquean.
UTC timezone-aware. Ningún reloj real, aleatoriedad o acceso de red.

## Fuentes, identidad y procedencia

`FinancialSource.read(cursor, until)` y `MarkPriceSource.read(cursor, until)`
proporcionan FinancialEvent frozen. Cada evento tiene received_at, sequence,
y un payload con event_id, effective_at y Reference(source, dataset, provenance,
assumptions). Los datasets/IDs son declaraciones del proveedor, no autenticación
criptográfica ni una certificación automática del mercado.

- SYNTHETIC_FIXTURE: ejemplo manual. FixtureFinancialSource/FixtureMarks exigen esta etiqueta.
- MODELED: hipótesis explícita (por ejemplo tasas configuradas o funding calculado).
- UNKNOWN: evidencia conservada pero no autoriza métricas monetarias dependientes.
- OBSERVED: categoría reconocida; construcción rechazada en esta fase. Exige un
  contrato futuro independiente de autenticidad, secuencia y fuente autorizada.

FixtureMarks solo admite MarkEvidence. El puerto de costos no admite Marks en
ReplayRunner. No se transforma OHLC Close, bid/ask, último trade ni TP en Mark.
No hay adaptador Binance, API, descarga de mercado o recuperación LIVE.

Identidad SHA-256 incluye PerformancePolicy completo, versión, supuestos,
capital, tasas, rol por FillId y las dos identidades de fuente/dataset. Scenario
es obligatorio FAVORABLE/BASE/ADVERSO; no trae valores de mercado por defecto.
Las identidades son deterministas, sin uuid4/random/datetime.now. Diferencias de
representación textual Decimal también pueden distinguir configuraciones.

## Comisiones

FeeEvidence enlaza event_id, FillId, OrderId, instante económico del Fill,
importe no negativo, moneda y Reference. Su disponibilidad es FinancialEvent.received_at.
La evidencia debe pertenecer a un Fill ya recibido por el ledger, con idéntico
OrderId y timestamp económico. Orders, intenciones y cancelaciones no generan fees.
Cada FillId acepta una comisión total única: IDs duplicados idénticos son retries;
otra identidad para el mismo fill o contenido incompatible bloquean.

Alternativa opcional FeeRatePolicy:

```text
fee_USDC = notional_ejecutado_USDC × tasa
```

Exige maker_rate, taker_rate y clasificación MAKER/TAKER explícita por FillId,
todos configurados externamente y con procedencia MODELED. LIMIT no clasifica
maker; MARKET no prueba una tasa real. Sin rol se conserva fee desconocida. La
política no trae tasas oficiales ni selecciona una tasa por tipo de orden.
No se permite FeeEvidence y tasa para el mismo fill: bloquear evita cobrar dos
veces. Los fees modelados se reconstruyen desde ledger + policy; no se crean
fills o hechos de exchange ni se modifican cantidades/precios efectivos.

Fees en BNB u otra moneda quedan sin conversión: importe USDC NOT_AVAILABLE,
con motivo y evidencia originales conservados. Esta fase no inventa FX ni rebates
negativos: estos últimos requerirían contrato adicional. Cero necesita evidencia
o tasa explícita cero. Sin fills recibidos, no existen comisiones de ejecución.

## Funding y exposición histórica

FundingEvidence identifica símbolo, evento efectivo, RoundId, cantidad de
exposición atribuible y Reference. Dos modalidades mutuamente excluyentes:

1. amount_usdc firmado explícito.
2. rate, reference_price, reference_price_at exactamente efectivo y
   positive_rate_pays=True, etiquetados MODELED.

```text
funding_LONG = -(cantidad_histórica × precio_referencia) × tasa
```

La posición se verifica contra sumas canónicas de BUY menos SELL confirmados
con executed_at < effective_at en esa ronda. Posición posterior no paga un
funding anterior. Una ocurrencia Fill exactamente en funding carece de orden
cruzado suficiente y bloquea; no se inventa prioridad. La modalidad modelada
exige precio correspondiente al instante efectivo, no Mark futuro ni OHLC proxy.
La referencia declara fuente/supuestos; no demuestra autenticidad por etiqueta.

No se cobra con exposición cero ni se mueve funding entre rondas. Referencia a
otra exposición, símbolo o ronda bloquea. Evento repetido no cambia cash; otra
ID para misma ronda/instante efectivo también bloquea. No hay calendario de ocho
horas ni tasas/intervalos constantes implícitos. Positivo y negativo están permitidos.

Un fill nuevo tardío que cambiaría exposición de un funding reconocido bloquea,
preservando el prefijo y sin reescribir series. Funding recibido tardíamente se
reconoce al recibirse si la exposición histórica ya puede demostrarse. Sin
Fill conocido suficiente no se calcula un funding ficticio.

## Cobertura y datos incompletos

CoverageEvidence declara inventario completo FUNDING o TRANSFERS en un intervalo
[start,end], effective_at=end, event_ids y Reference. Solo está disponible desde
received_at >= end. No puede autorizar una valoración anterior a su recepción.
Se exige cubrir todo el tramo pertinente, uniendo intervalos explícitos sin gaps;
IDs declaradas deben estar recibidas y tener origen utilizable. Un inventario
pendiente no equivale a cero; tipo/período/dataset/evento contradictorios bloquean.

Un inventario completo vacío justifica cero bajo esa evidencia/hipótesis. No recibir
eventos ni tener exposición actualmente cero demuestra cobertura del pasado.
Estas declaraciones no certifican autenticidad: fixtures son pruebas sintéticas;
los inventarios modelados solo autorizan sensibilidad etiquetada MODELED.

Fees cubren exactamente los FillIds recibidos mediante evidencia/tasa explícita.
Capital tiene Reference y fecha inicial. Marks tienen tiempo, frescura, continuidad
y procedencia independientes. Transferencias requieren inventario igual que funding.
Los informes/journal conservan cobertura recibida, fuentes, IDs y faltantes.
Completar un período más tarde puede habilitar el resultado actual, sin cambiar
las valoraciones anteriores que entonces carecían de cobertura.

## Wallet, neto y flujos externos

TransferEvidence distingue DEPOSIT/WITHDRAWAL, cantidad positiva USDC, identidad,
ocurrencia, recepción y procedencia. Duplicados idénticos son idempotentes;
retiros negativos o identidades conflictivas se rechazan. No hay transferencia real.

```text
neto_cash_acumulado = PnL_bruto_realizado - fees + funding_neto
wallet_USDC = capital_inicial + aportes - retiros + neto_cash_acumulado
```

No se resta nocional BUY como Spot ni se suma ingreso SELL completo como Spot.
No se añade margen/leverage al wallet. Un wallet negativo es una condición
explícita (`wallet_negative`), no liquidación o garantía de margen fingida.
Cada elemento permanece separado y conciliable contra su journal/fills originales.

RoundPerformance atribuye fees por FillId y funding por RoundId, cubriendo desde
su primer Fill hasta closed_at, o el instante actual si permanece abierta. El
neto_cash de una ronda abierta no es beneficio final. `net_realized_closed` exige
cierre conciliado en FinancialLedger, cantidad cero, ventas y costos completos;
una posición momentáneamente plana sin CLOSED no prueba finalización de ronda.
No se fuerza venta/cierre al finalizar el replay. Base shortfill mantiene exposición,
sin P0 definitivo ni DCA/TP; esa exposición puede valorarse con Mark válido.

Retorno porcentual general permanece NOT_AVAILABLE, incluso sin flujos. Aportes
no son PnL ni rendimientos; no se implementa TWR/MWR improvisado ni se usa el
presupuesto de órdenes como capital o denominador de rentabilidad.

## Mark, PnL flotante y patrimonio

MarkEvidence exige símbolo, price positivo, effective_at del precio, recepción,
Reference, freshness_limit_ms explícito, continuous y price_kind="MARK".
Su uso exige effective_at <= now <= effective_at + freshness y continuidad.
Nuevo Mark con ocurrencia anterior/igual a otro ya recibido bloquea; no sobrescribe
pasado. Un dato viejo recibido hoy no se rejuvenece. No hay interpolación.

```text
PnL_flotante_LONG = cantidad_abierta × Mark - costo_contable_abierto
Equity = wallet + PnL_flotante
```

Utiliza costo promedio móvil del ledger, no P0, TP, DCA teórico o liquidación.
Con posición exactamente cero, flotante cero no necesita Mark. Con exposición y
Mark faltante/obsoleto/discontinuo/UNKNOWN, flotante y equity NOT_AVAILABLE.
Capital, fees, funding o transferencias incompletos impiden wallet/equity dependientes,
pero no invalidan el ledger bruto. No se suma dos veces PnL realizado ni margen;
equity no se declara margen libre.

## Serie equity y drawdown

Se toma una valoración después de todos los eventos reales del reloj de replay:
cierres 1m, entregas, controles/acciones/transiciones y eventos financieros.
Solo muestras con equity válido actualizan pico/drawdown. La serie conserva
muestras inválidas, motivos y procedencia, sin inventar un cero monetario.

```text
peak = máximo equity válido conocido hasta la muestra actual
drawdown_USDC = peak - equity_actual
drawdown_pct = drawdown_USDC / peak × 100  # solo peak > 0
```

Se guardan pico/fecha, drawdown actual, máximos absolutos y porcentuales,
fechas del pico/valle del máximo absoluto y última valoración válida en series.
Las muestras usan todas las pérdidas flotantes conocidas, no solo rondas cerradas.
Máximo absoluto y porcentual pueden corresponder a momentos distintos.

Cobertura `COMPLETE_AT_SAMPLED_INSTANTS` solo cubre las muestras declaradas:
no demuestra máximo intraminuto ni histórico continuo. Una muestra inválida
conserva `PARTIAL` y métricas drawdown `INCOMPLETE` sobre el subconjunto válido.
Un reporte subminuto sin nuevos eventos calcula disponibilidad/frescura actual,
pero no añade una muestra ni reescribe pico/máximos al consultar. El drawdown
actual se acota a cero si equity supera el último pico muestreado; ese reporte
no incorpora un nuevo máximo a la serie. Flujos externos pueden mover equity:
drawdown es raw equity, etiquetado `EXTERNAL_FLOWS_NOT_RETURN_ADJUSTED`, no retorno
ajustado por flujos ni medición aislada del rendimiento de trading.

## Secuencia causal y reanudación

Se conserva íntegro el orden aprobado del runner/motor/simulador. Después de su
último drenaje en el timestamp se sincroniza la vista del ledger bruto, se entregan
los costos por sequence, luego Marks por sequence, y se valora una sola vez.
No hay cambios retrospectivos ni fills futuros contabilizados desde simulation.facts.

Fuentes son offline finitas. El runner carga su calendario configurado hasta end
para programar entregas; eventos futuros se mantienen **pendientes**, sin producir
asientos/fees/funding/Mark actual. Este calendario/identidad forma parte de configuración
y snapshot, no de evidencia disponible al cálculo. Los reportes financieros
contienen únicamente eventos recibidos. Idempotencia por event_id + payload inmutable;
cambiar received_at/sequence en un retry no duplica importes. Conflictos conservan
observación rechazada, prefijo aceptado y disponibilidad BLOCKED. Fees/transferencias
retrasados alteran efectivo al recibirse, sin reescribir la serie previa.

Snapshot añade policy/identity, capital, fuentes, costos/eventos reconocidos,
Marks, cobertura, journal económico, serie, picos/drawdown, conflictos, cursor y
colas futuras. Journal incluye vistas del ledger únicamente cuando cambian;
valida cada vista reconstruyéndola con el ledger aprobado. Restore reconstruye
performance desde journal y compara estado completo, además de verificar cursor,
pendientes y enlace con ledger del runner. Un checksum recalculado no permite
borrar series/costos o anticipar eventos. No es firma de autenticidad.

Misma sesión SQLite abierta, período, fuentes, políticas y versión requeridos.
Continuo == fragmentado incluye órdenes/fills, fees/funding, capital/Mark, wallet,
equity/drawdown, series, informes y SHA-256, con cancelación TP y entregas pendientes.
Sin serialización/recovery entre procesos o recuperación LIVE. Calendario, vistas
de ledger y series crecen en memoria: esta implementación finita no promete
escalabilidad ni persistencia de varios años.

## Disponibilidad e informes

`ReplayReport.financial_performance` contiene métricas separadas (`value`, `status`,
`reasons`, `provenances`), rondas, series, cobertura, journal/IDs, fuentes e identidad.
AVAILABLE, MODELED, NOT_AVAILABLE, INCOMPLETE y BLOCKED son distinguibles.
No se emite un único éxito financiero ocultando faltantes. La etiqueta global
preexistente `financial_results` sigue describiendo bruto, no validación de neto.
El reporte 4A-3A queda intacto, incluidos sus campos reservados NOT_AVAILABLE;
resultados ampliados viven en financial_performance.

`compare_scenarios()` exige tres informes FAVORABLE/BASE/ADVERSO del mismo período,
instante y capital. Expone costos, neto cash, wallet/equity, drawdown y exposición
con sus estados y supuestos. Si las tres tasas son comparables, valida maker/taker
no decrecientes; no ordena PnL final ni presume que precio/ejecuciones fueron
iguales. Identidades de fuentes de cada informe quedan auditables; comparar exige
que el llamador también justifique datasets de mercado equivalentes. La comparación
no certifica esa equivalencia ni calibra Binance, y conserva faltantes.

## Pruebas numéricas reproducibles

| Caso sintético | Resultado |
| --- | --- |
| BUY 10×100; Mark 95 | costo 1000; flotante -50 |
| BUY 10×100; SELL 5×110; Mark 95 | bruto 50; cantidad 5; costo 500; flotante -25 |
| anterior + capital 1000, fees 2.10, funding -3 | wallet 1044.90; equity 1019.90; cash neto 44.90 |
| equity 1000 → 1100 → 990 | pico 1100; DD máximo 110 y 10% |
| fill sin fee/política | bruto válido; fee/neto desconocidos |
| exposición sin cobertura funding | funding/equity desconocidos |
| exposición sin Mark | flotante/equity desconocidos |
| exposición cero | flotante cero; neto exige todos los costos |

Regresión Base→DCA1–DCA7→TP→CLOSED conserva DCA/TP parciales, entregas demoradas,
TP anterior con fill antes de cancelación/reemplazo, fees explícitas, funding
positivo/negativo y Mark independiente. Bruto, costos, wallet, equity y neto final
se verifican contra ecuaciones independientes. Base parcial cancelada es otra
regresión de exposición abierta y bloqueo estratégico; no se completa artificialmente.

## Límites y requisitos pendientes

No hay fees/funding/tasas/Mark históricos oficiales, brackets de margen, margen
libre, liquidación, Add Margin, TWR/MWR, APIs, LIVE, Telegram, servicios o VPS.
OBSERVED necesita validación separada; un período Mark/costos parcial no es
backtest financiero validado 2024–2026. Pendientes: datasets auténticos con cobertura,
calendario de funding, evidencia de comisiones/rol maker-taker y conversión autorizada,
Mark causal validado, fuente de capital/flujos, secuencia cruzada de funding/fills,
recovery persistente, almacenamiento acotado y calibración fuera de muestra.
Margen/liquidación requieren contratos verificados y revisión independiente.
