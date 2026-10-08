# Alternativa B — especificación canónica

Estado: **Fase 3A — especificación de estrategia**. Este documento es la fuente
de verdad matemática y operativa para la implementación posterior. Una
implementación que lo contradiga constituye una regresión, salvo cambio aprobado
de esta especificación. Esta fase no implementa estrategia, ejecución ni las
correcciones arquitectónicas indicadas al final.

Implementación matemática: **Fase 3B-1A**. La configuración canónica y la capa matemática
pura de sizing (pesos, márgenes y nocionales), desviaciones/precios teóricos DCA,
promedio ponderado de fills y TP teórico están implementadas y testeadas en
`strategy/alt_b`. Las fórmulas y parámetros de esta especificación se conservan.
**Fase 3B-1B** implementa la evaluación pura de los filtros y vetos de entrada,
agregación 1m→15m y RV con taker-buy real. El motor concreto, señales, acciones y
ejecución de órdenes siguen pendientes. `EntryEvaluation` no emite una señal.
**Fase 3B-2A** añade la ingesta incremental con historial explícito e inmutable;
reutiliza esa evaluación sin cambiar fórmulas ni parámetros.

## 1. Identidad y alcance

| Concepto | Valor canónico |
| --- | --- |
| `strategy_id` | `B_DCA7_4X_TP080_AMPLIADO_VPS` |
| Mercado de referencia | Binance USDⓈ-M Futures |
| Contrato de referencia | `1000SHIBUSDC` |
| `direction` | `LONG` |
| `margin_mode` | `ISOLATED` |
| `position_mode` | `ONE_WAY` |
| `asset_mode` | `SINGLE_ASSET` |
| `leverage` | `4` |

El símbolo es configurable. No convertir silenciosamente `1000SHIBUSDC` a
`SHIBUSDC`, a un contrato USDT ni a Spot. Como máximo puede existir una ronda
activa por cuenta/símbolo. Replay, Shadow y Live deberán utilizar el mismo
`StrategyEngine` y estas mismas reglas estratégicas.

## 2. Parámetros congelados

| Parámetro | Valor |
| --- | --- |
| `additional_dca_orders` | `7` |
| `take_profit_fraction` | `0.008` (0.80%) |
| `first_deviation` | `0.015` (1.50%) |
| `deviation_multiplier` | `1.15` |
| `amount_multiplier` | `1.40` |
| Peso de Base | `59` |
| Peso inicial de DCA | `69` |
| `strategy_stop` | `OFF` |
| `trailing` | `OFF` |
| `reinvest_order_sizes` | `false` |

Los cocientes y desviaciones de las fórmulas son fracciones, no valores
porcentuales enteros. `strategy_stop = OFF` no resuelve la política pendiente
de stop de emergencia LIVE.

## 3. Capital de órdenes y distribución

El presupuesto de órdenes por defecto es **1,200.00 USDC**. Es configurable
únicamente con el bot `PAUSED`, posición cero, ninguna ronda activa, ninguna Base
pendiente y ninguna entrada programada pendiente. Cambiar el capital no cambia
los parámetros de estrategia.

El presupuesto se distribuye exclusivamente entre Base y DCA1–DCA7. Las reservas
de Add Margin y las utilidades retenidas no aumentan automáticamente los tamaños:
`reinvest_order_sizes = false`.

Sea `C` el presupuesto de órdenes en USDC; se distingue de `C[i]`, el precio de
cierre 15m utilizado más adelante. Para `k = 1..7`:

```text
W = 59 + 69 × sum(1.40^j, j=0..6)
W = 1704.882944

Margen_Base  = C × 59 / W
Margen_DCA_k = C × 69 × 1.40^(k-1) / W
Nocional_k  = Margen_k × 4
```

La fórmula de nocional aplica tanto a Base como a cada DCA. La suma de los
márgenes asignados es `C`. Los cálculos monetarios deben usar `Decimal`, sin
redondeo interno a centavos. Las fórmulas son la fuente de verdad; los importes
de la tabla son solamente presentación aproximada para `C = 1,200 USDC`.

| Orden | Margen aproximado (USDC) |
| --- | ---: |
| Base | 41.53 |
| DCA1 | 48.57 |
| DCA2 | 67.99 |
| DCA3 | 95.19 |
| DCA4 | 133.27 |
| DCA5 | 186.57 |
| DCA6 | 261.20 |
| DCA7 | 365.68 |

Estos importes son margen, no cantidad del activo ni nocional. La cantidad de
Base no se fija en el instante de señal si depende del precio ejecutable futuro.

## 4. Escalera DCA

`P0` es el precio promedio efectivo de los fills de Base **una vez resuelta**.
No existe un `P0` definitivo obtenido del precio de señal o de una ejecución
supuestamente instantánea. Para `k = 1..7`:

```text
Caida_k      = 0.015 × sum(1.15^j, j=0..k-1)
Precio_DCA_k = P0 × (1 - Caida_k)
```

| Nivel | Caída acumulada desde P0 (presentación) |
| --- | ---: |
| DCA1 | 1.500000% |
| DCA2 | 3.225000% |
| DCA3 | 5.208750% |
| DCA4 | 7.490063% |
| DCA5 | 10.113572% |
| DCA6 | 13.130608% |
| DCA7 | 16.600199% |

Los porcentajes mostrados están redondeados para presentación; calcular los
precios con la fórmula. Todos los DCA permanecen anclados a `P0`. No recalcular
la escalera desde el DCA anterior, el nuevo precio promedio ni el precio actual.
La política definitiva de Base shortfill permanece pendiente para LIVE.

## 5. Take profit

Después de cada fill efectivo de entrada, el promedio se obtiene de las
cantidades y precios realmente ejecutados:

```text
AveragePrice   = sum(qty_i × fill_price_i) / sum(qty_i)
TP_theoretical = AveragePrice × 1.008
```

El TP normal es una salida LONG `SELL`, con `reduce_only = true`, por la cantidad
realmente abierta. La intención reduce-only nunca debe aumentar exposición ni
crear una posición contraria. Los fills efectivos determinan el promedio y la
cantidad; las cantidades solicitadas o pendientes no sustituyen a los fills.

Fees y funding se contabilizan separadamente y no se incorporan al objetivo
de 0.80%. Este objetivo de precio no es una rentabilidad neta garantizada.

## 6. Datos de mercado, agregación y warmup

La fuente estratégica son velas **cerradas Futures de 1 minuto**. Los bloques
15m se construyen en UTC con inicio en los minutos `00`, `15`, `30` y `45`.
Cada bloque abarca 15 minutos y requiere exactamente 15 velas 1m consecutivas y
válidas, todas cerradas. Su apertura es la de la primera vela, su máximo el
máximo de los 15 máximos, su mínimo el mínimo de los 15 mínimos, su cierre el de
la última vela y su volumen base la suma de los 15 volúmenes base.

Si falta un minuto, el bloque no es válido para producir señal: **NO SIGNAL**.
No fabricar ni interpolar OHLC. Se requieren como mínimo **26 horas completas**
de warmup, además de todas las referencias válidas de los filtros y vetos.
El tiempo transcurrido por sí solo no valida un historial incompleto.

Contrato implementado en 3B-1B: intervalos UTC semiabiertos
`[open_time, close_time)`; aperturas al minuto exacto y cierre un minuto después.
`aggregate_15m_block` exige una tuple de exactamente 15 Candle del mismo Symbol,
ordenadas y consecutivas desde 00/15/30/45; rechaza duplicados y gaps sin repararlos.
Suma `taker_buy_base_volume` únicamente si los 15 minutos lo contienen; si falta
uno, el agregado conserva None. None representa ausencia, no volumen cero.

`evaluate_entry_checkpoint(candles_1m, checkpoint_at, config)` exige checkpoint
UTC aware alineado a 15m y una tuple de Candle. Rechaza con ValueError toda vela
que cierre después del checkpoint. Consume únicamente los 1560 minutos que
terminan exactamente allí; puede recibir historia anterior que no participa
en las fórmulas. Un símbolo diferente del configurado es error de input.
No ordena, interpola, busca datos futuros ni normaliza timestamps del proveedor.

### Ingesta incremental — Fase 3B-2A

`AltBMarketState` guarda solo una tuple de hasta AltBConfig.warmup_minutes velas
normalizadas del mismo símbolo, ordenadas, sin duplicados ni gaps. El límite
canónico es 1560; cada incorporación expulsa el exceso más antiguo.
`ingest_closed_1m(state, candle, config)` es puro y retorna `AltBMarketStep` frozen
con estado, evaluación opcional y resultado `accepted`, `duplicate` o `gap_reset`.

El evento es una vela cerrada entregada por el llamador, sin consultar reloj.
La primera inicia historial; cada siguiente debe empezar al cierre de la anterior.
Reenviar la última vela idéntica conserva el estado y no evalúa otra vez. El mismo
intervalo con contenido diferente o una vela anterior produce ValueError; no
sobrescribe historia. Un gap hacia adelante descarta el historial anterior e
inicia uno con la nueva vela, reiniciando también el warmup; no inventa minutos.
Taker-buy se conserva literalmente, incluido None.

Solo en cierre UTC 00/15/30/45 de un evento incorporado se llama exactamente una
vez a evaluate_entry_checkpoint sobre el nuevo historial causal. Fuera de esos
cierres o ante duplicado, evaluation=None. Antes de 26h se conserva la evaluación
fail-closed de 3B-1B. La secuencia incremental de 27h se prueba contra evaluación
directa del prefijo completo en cada checkpoint, con igualdad exacta.

El estado puede reconstruirse desde velas validadas; no se implementa persistencia
ni serialización. Un futuro runner deberá conservar historial, cursor de última
vela, estado de estrategia/ronda y acciones/órdenes pendientes. La transferencia
explícita de estado a través del motor por eventos sigue pendiente; no se cambia
StrategyEngine, StrategyResult ni StrategyCoordinator ni se acopla estado financiero
a CoordinatorState. No hay señales, órdenes ni garantía de una orden por evento.

En las fórmulas siguientes, `i` es el bloque 15m recién cerrado; `C`, `H` y `L`
indexados son cierre, máximo y mínimo 15m. Los intervalos de índices incluyen
ambos extremos. Todas las referencias usan únicamente datos ya cerrados al
instante de evaluación. Para medianas de un número par de observaciones se usa
el promedio de los dos valores centrales ordenados.

## 7. Filtro ampliado

Los tres filtros deben aprobar simultáneamente:

```text
Cambio24h = C[i] / C[i-96] - 1
PASS: -0.045 <= Cambio24h <= 0.08

Rango4h = (max(H[i-15..i]) - min(L[i-15..i])) / min(L[i-15..i])
PASS: Rango4h >= 0.022

Retroceso1h = 1 - C[i] / max(H[i-3..i])
PASS: Retroceso1h >= 0.011
```

Todos los límites son inclusivos. Rango4h utiliza los 16 bloques que terminan
en `i`; Retroceso1h utiliza los cuatro bloques que terminan en `i`.

## 8. Veto A

```text
V15[i]   = sum(base_volume_1m del bloque i)
MedVol4h = mediana(V15[i-16..i-1])
Caida1h  = 1 - C[i] / C[i-4]

VetoA = (V15[i] >= 8 × MedVol4h) AND (Caida1h >= 0.01)
```

La mediana utiliza los 16 bloques anteriores y excluye el bloque actual.
Ambas condiciones deben cumplirse para activar el veto.

## 9. Veto B — taker buy real

La fuente exclusiva del Buy Ratio es **`REAL_TAKER_BUY_BASE_VOLUME`**.
No usar `BuyRatioProxy`, volumen quote como sustituto del volumen base ni la
antigua regla de 10%.

```text
BuyRatioReal = sum(taker_buy_base_volume_1m del bloque i) / V15[i]
MedVol24h   = mediana(V15[i-96..i-1])
```

Para cada minuto cerrado:

```text
r[t] = ln(Close1m[t] / Close1m[t-1])

r_promedio = sum(últimos 30 retornos) / 30
RV30m      = sqrt(sum((r[t] - r_promedio)^2, últimos 30 retornos) / 30)
```

`RV30m` es la desviación estándar **poblacional**, `ddof = 0`, de los últimos
30 retornos logarítmicos, sin anualización. Esos retornos necesitan 31 cierres
1m consecutivos válidos. Se toma un snapshot de RV30m al cierre de cada bloque
15m. `RVref24h` es la mediana de los **96 snapshots anteriores**, excluyendo el
snapshot actual; la mediana de volumen también excluye el bloque actual.

```text
RVref24h = mediana(RV30m[i-96..i-1])

VetoB = (BuyRatioReal <= 0.25)
        AND (V15[i] >= 2.5 × MedVol24h)
        AND (RV30m[i] >= 1.5 × RVref24h)
```

El umbral de Buy Ratio es **0.25**, inclusivo. Las tres condiciones deben
cumplirse simultáneamente para activar Veto B.

Si cualquier referencia requerida es inválida, inexistente o cero, el resultado
es **NO SIGNAL**. Esto incluye las medianas de referencia, RVref24h y los
denominadores requeridos. Datos taker buy faltantes no se sustituyen por un
proxy. No interpretar un veto incalculable como un veto desactivado.

### Evaluación pura y fail-closed — Fase 3B-1B

`EntryEvaluation` es un resultado frozen con checkpoint, data_valid, entry_allowed,
reason, todas las métricas anteriores, tres flags PASS y veto_a/veto_b. Para datos
válidos, entry_allowed es únicamente la conjunción de los tres PASS y ausencia
de ambos vetos. La capa no evalúa rondas ni entradas pendientes.

Todas las operaciones estadísticas/financieras usan Decimal a 50 dígitos con
contexto local independiente del llamador. La mediana par promedia los dos
centrales. RV usa `.ln()` y `.sqrt()` Decimal, 30 retornos/31 cierres, ddof=0;
solo se toma snapshot en cierre 15m. No anualiza ni utiliza retornos simples.
Taker-buy se exige únicamente en los 15 minutos actuales, donde se consume;
BuyRatioReal=0 es válido. Las medianas y RVref24h requieren referencia positiva.

Los errores de tipos/checkpoint/config y las velas futuras producen
TypeError/ValueError. Los problemas normales de calidad producen data_valid=False,
entry_allowed=False y métricas/flags no calculados en None. Razones estables:

| Razón | Condición |
| --- | --- |
| `insufficient_history` | Historia vacía o no alcanza el inicio del warmup de 26h. |
| `checkpoint_not_available` | Ninguna vela cierra exactamente en checkpoint. |
| `missing_or_nonconsecutive_minute` | Ventana incompleta, duplicada, desordenada, desalineada, de duración incorrecta o símbolos mezclados. |
| `missing_current_taker_buy` | Falta el dato real en algún minuto actual. |
| `zero_current_volume` | Volumen agregado actual cero. |
| `zero_med_vol_4h` | Mediana de los 16 bloques anteriores cero. |
| `zero_med_vol_24h` | Mediana de los 96 bloques anteriores cero. |
| `rv_snapshot_unavailable` | No puede obtenerse un snapshot requerido de 31 cierres consecutivos. |
| `zero_rv_ref_24h` | Mediana de los 96 snapshots anteriores cero. |

Precedencia: errores de input/futuros, historia vacía, disponibilidad del
checkpoint, alcance del warmup, continuidad, símbolo configurado, taker-buy
actual, volumen actual, medianas 4h/24h, snapshots RV y referencia RV. La validación
de continuidad de toda la ventana detecta antes los minutos que también impedirían
un snapshot. No se interpreta ningún dato inválido como veto False.

Para datos válidos, reason es `veto_a` si está activo, después `veto_b` si está
activo, `entry_allowed` si se permite entrada o `filters_failed` en otro caso.
Estos códigos solo describen el resultado puro, sin generar IDs, señales u órdenes.

## 10. Permiso de entrada

Una nueva señal puede existir únicamente si se cumple toda la conjunción:

```text
Cambio24h PASS
AND Rango4h PASS
AND Retroceso1h PASS
AND NOT VetoA
AND NOT VetoB
AND no existe ronda activa
AND no existe entrada pendiente
AND todos los datos requeridos son válidos
```

Los vetos bloquean exclusivamente nuevas rondas; no cancelan por sí mismos
DCA de una ronda ya abierta. Se mantiene el máximo de una ronda activa por
cuenta/símbolo. PAUSE bloquea nuevas rondas según la sección de control operativo.

## 11. Temporización causal

La señal se confirma al cierre completo de la vela 15m. Para una señal en `T`:

```text
signal_time = T
created_at  = instante causal de señal
not_before  = created_at + 120 segundos
```

La entrada se programa para `signal_time + 120 segundos` y no puede ejecutarse
antes de ese instante. La estrategia no usa `sleep`; el coordinador trabaja
con tiempo explícito. Los filtros no se recalculan durante esos 120 segundos.
Condiciones operativas o de seguridad sí pueden invalidar la entrada.

Esta temporización expresa la acción futura requerida. La arquitectura de
Fase 2.1 permite representarla mediante ScheduledAction sin fijar anticipadamente
una Quantity; OrderIntent queda reservado para una orden ya concreta.
La estrategia y su temporización específica todavía no están implementadas.

## 12. PAUSE, RESUME y cambio de capital

Posteriormente el operador podrá solicitar PAUSE desde Telegram. Esta fase
especifica su semántica y no implementa esa integración.

PAUSE bloquea nuevas rondas y no significa cerrar una ronda con pérdida. Si se
solicita durante los 120 segundos de espera, invalida la entrada pendiente.

Para modificar el presupuesto deben cumplirse simultáneamente:

- Bot `PAUSED`.
- Posición igual a cero.
- Ninguna ronda activa.
- Ninguna Base pendiente.
- Ninguna entrada programada pendiente.

RESUME no abre una Base inmediatamente: vuelve a esperar una **nueva** señal.
Nunca ejecutar una señal antigua después de pausa o reinicio. Ni las utilidades
retenidas ni las reservas aumentan automáticamente el presupuesto configurado.

## 13. Add Margin — política de evaluación Replay/Paper

El controlador canónico es **`LOCAL`**. No utilizar simultáneamente el Auto-Add
nativo como segundo controlador. Para LONG:

```text
LiquidationDistance = (MarkPrice - LiquidationPrice) / MarkPrice
Trigger: LiquidationDistance <= 0.10
Target:  LiquidationDistance >= 0.15
Cooldown: 10 segundos
Delay de referencia para Replay: 2 segundos
```

El controlador deberá calcular el menor aporte permitido que busque restaurar
el target. La fuente máxima utilizable es:

```text
FreeFuturesBalance
- margen reservado para DCA pendientes
- otras reservas obligatorias
```

Add Margin no aumenta los tamaños Base/DCA, no consume silenciosamente el
margen reservado para DCA, no utiliza fondos inexistentes y no retira margen
durante una ronda. Las reservas y el presupuesto de órdenes permanecen
conceptualmente separados.

Esta política queda aprobada para especificación y pruebas **Replay/Paper**;
no autoriza su activación LIVE. Para LIVE se requieren fórmula exacta validada
con brackets vigentes, pruebas de integración, límites máximos, reconciliación
Binance y autorización LIVE específica. No asumir una fórmula de liquidación
o un aporte numérico no validados. **El objetivo 10% → 15% no garantiza evitar
una liquidación.**

## 14. Separación de responsabilidades

| Capa | Responsabilidades |
| --- | --- |
| A. Estrategia B | Filtros, vetos, sizing, DCA, TP y señal |
| B. Modelo de ejecución Replay | Fees, slippage, fills parciales, trayectoria intraminuto, participación de volumen y funding |
| C. Política de capital/riesgo | Reservas, Add Margin y límites LIVE |

Los supuestos de ejecución no alteran las fórmulas estratégicas. La política
de capital/riesgo no reinvierte automáticamente ni cambia los tamaños de
órdenes. Este documento no fija parámetros nuevos para el modelo de ejecución
Replay ni implementa ninguna de las tres capas.

## 15. Decisiones pendientes para LIVE

Permanecen sin valores aprobados:

- Límites máximos de Add Margin.
- Reserva mínima obligatoria.
- Acción cuando se agote la reserva.
- Stop de emergencia.
- Pérdida máxima por ronda.
- Drawdown máximo total.
- Duración máxima.
- Tolerancias de slippage.
- Política definitiva de Base shortfill.
- Límites LIVE de spread/liquidez.

No inventar valores ni interpretar su ausencia como límites ilimitados o
autorización LIVE. Los requisitos adicionales de activación de Add Margin
de la sección anterior también siguen pendientes.

## 16. Prerequisitos de implementación

Una señal a `T` no debe fijar prematuramente la Quantity de Base cuando depende
del precio ejecutable en `T + 120s`. `P0` solo existe después de resolver los
fills de Base; DCA y TP dependen de fills reales.

En la base revisada de Fase 3A, `StrategyEngine` solo reaccionaba a `Candle`.
La corrección arquitectónica necesaria debía permitir:

- Programar una acción futura sin fijar prematuramente una `OrderIntent`
  completa.
- Reaccionar a eventos `Fill`/`Order`.

Fase 2.1 incorpora esos contratos: ScheduledAction sin Quantity y un único
StrategyEngine con handlers de Candle, Quote, Order, Fill y ScheduledAction.
Esta corrección no implementa Alternativa B, P0, DCA ni TP. Los contratos y la
coordinación se describen en [DOMAIN_MODEL.md](DOMAIN_MODEL.md),
[ARCHITECTURE.md](ARCHITECTURE.md) y [ORCHESTRATION.md](ORCHESTRATION.md).
Replay, Shadow y Live conservarán el mismo motor.
