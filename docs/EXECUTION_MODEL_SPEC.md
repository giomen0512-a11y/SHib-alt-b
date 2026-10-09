# Modelo de precios y políticas de ejecución — Fase 4A-2A

Estado: primera capa offline de precios/supuestos. Comparar sensibilidad no
calibra un mercado. **No implementa fills históricos, Order, PnL ni ejecución
LIVE**. El runtime continúa STOPPED. Los resultados siguen marcados
FINANCIAL_RESULTS_NOT_AVAILABLE cuando el ledger opt-in de 4A-3A está desactivado.

Base aprobada: `01888d2758c231ffc51a82e7c0ea99f26136031e`.
Rama: `phase-4a-2a-execution-policy`; un commit de implementación, sin merge.
La estrategia conserva íntegros símbolo 1000SHIBUSDC, LONG, leverage 4,
presupuesto 1200 USDC, Base+DCA1–DCA7, TP 0.008, SL OFF y trailing OFF;
no se modifican filtros, vetos, IDs ni fórmulas Alt B.

## Arquitectura y fuentes

```text
ExecutionPolicy / ExecutionProfiles ← objeto validado o JSON estricto
                     ↓
SQLiteHistory read-only → ModeledQuotes (QuoteSource)
                     ↓ ModeledQuoteEvidence
ReplayRunner → StrategyCoordinator → el mismo AltBEngine
                     ↓
intenciones teóricas + snapshot/report con identidad y evidencia de precios
```

`replay/execution_model` depende de modelos de dominio, lectura SQLite y los
puertos de replay. No depende del motor, fórmulas estratégicas, transporte HTTP,
riesgo o ExecutionAdapter. Los contratos futuros de liquidez/ambigüedad devuelven
análisis, sin autorizar fills. FactSource y sus hechos manuales siguen separados.

Datos de entrada: Candle Futures 1m normalizadas, Decimal exacto, UTC aware,
mercado BINANCE_USDM_FUTURES y símbolo 1000SHIBUSDC. Las unidades de volumen y
Quantity son unidades del **contrato 1000SHIBUSDC**, no SHIB individuales.
No se transforma el símbolo ni se multiplica volumen/cantidades por 1000.
Una vela de trades no contiene necesariamente cotizaciones bid/ask ejecutables.

## Procedencia de precios

`PriceOrigin` y `price_origin()` distinguen cuatro categorías:

| Categoría | Representación | Alcance |
| --- | --- | --- |
| Real observada | QuoteEvidence con REAL_OBSERVED | Evidencia bid/ask explícita del proveedor, independiente de OHLC; el llamador debe acreditar fuente/tiempo. No hay conector de cotizaciones reales en esta entrega. |
| Derivada | ModeledQuoteEvidence con MODELED_FROM_1M | Estimación usando un cierre ya disponible, spread configurado y supuestos identificados. |
| Manual de fixture | QuoteEvidence con SYNTHETIC_FIXTURE | FixtureQuotes conserva sus valores y su comportamiento anteriores. |
| No disponible | None, clasificada UNAVAILABLE | No existe Quote utilizable; jamás se sustituye por cero o una interpolación. |

El label REAL_OBSERVED no es una certificación automática de autenticidad.
ModeledQuoteEvidence exige MODELED_FROM_1M y conserva Candle de referencia,
available_at, expires_at, escenario, identity de política y dataset_identity.
Nunca identifica una estimación como cotización histórica real de Binance.
Un mercado de entrada sintético conserva su identificación explícita en dataset_identity.

## Modelo causal y disponibilidad

Para Candle `[T-1m, T)`, OHLCV solo puede usarse al cierre T. **La disponibilidad
instantánea del cierre normalizado en T es un supuesto del modelo**, no una
latencia demostrada de Binance. quote_latency_ms expresa la latencia adicional
modelada de producción/entrega de Quote; no demuestra una latencia real del feed.

Para consulta en el instante UTC `t`:

```text
reference_close = floor_minute(t - quote_latency_ms)
latest_closed = floor_minute(t)
available_at = reference_close + quote_latency_ms
expires_at = reference_close + freshness_limit_ms
```

Se selecciona exclusivamente la vela que termina en reference_close. La fuente
consulta un rango streaming desde esa vela hasta latest_closed y exige todos los
minutos consecutivos, válidos y cerrados <= t. Esta comprobación de continuidad
impide reutilizar una referencia anterior a través de un gap conocido. Los
minutos posteriores a reference_close se usan para validar continuidad, no para
reemplazar el precio por un cierre cuyo Quote todavía estaría bajo latencia.
No se leen ni decodifican velas abiertas/posteriores a latest_closed.

La evidencia es utilizable únicamente si `available_at <= t <= expires_at`.
La vigencia se mide desde **el cierre de referencia**, no desde su entrega; la
latencia consume parte de esa vigencia. Una referencia más antigua que la vigencia
no se reetiqueta como fresca. No se buscan velas anteriores para rellenar la
referencia esperada ausente. Un gap, duplicado, conflicto o dato inválido produce
None. El diagnóstico directo `estimate()` conserva un reason explícito.

Una latencia positiva en una frontera de minuto puede seleccionar el minuto
anterior, exclusivamente con continuidad y vigencia verificadas. Si la vigencia
es corta para esa edad, no habrá Quote. No se retrasa la acción ni se espera un
precio posterior. Los costos/latencias pueden cambiar qué referencia es utilizable;
no existe garantía de ordenar el PnL o los precios finales cuando cambia el mercado.

Quote.timestamp es **t, el instante de valoración de una estimación**, y la edad
real de sus datos queda en reference_candle.close_time/available_at/expires_at.
No se cambia el timestamp de una cotización observada o de fixture para hacerla
pasar por actual. Las consultas son funciones deterministas sin caché ni reloj
interno; no alteran ni reconstruyen retroactivamente decisiones anteriores.

## Parámetros y fórmula bid/ask

Todos los parámetros de mercado son **obligatorios**; no hay escenarios con
números por defecto. ExecutionPolicy frozen exige:

| Campo | Unidad / validación |
| --- | --- |
| scenario | FAVORABLE, BASE o ADVERSO |
| spread_bps | Decimal finito, 0 <= spread < 20000; spread bid/ask **completo** |
| slippage_bps | Decimal finito, 0 <= slippage < 10000; coste futuro, no aplicado a Quotes |
| quote_latency_ms | int no negativo, sin float/bool |
| freshness_limit_ms | int no negativo, >= latencia; duración representable |
| limit_eligibility | Regla futura explícita, nunca una confirmación de fill |
| liquidity | Participación máxima y regla futura de parciales explícitas |
| intraminute | Hipótesis acorde al escenario |
| assumptions | Texto obligatorio que explica el origen/limitaciones de los supuestos |

Los límites superiores de bps garantizan positividad matemática del bid y del
futuro factor SELL; no son máximos observados ni calibrados del exchange.

Sea `P = reference_candle.close` y `s = spread_bps / 10000`:

```text
1 bps = 0.01% = 0.0001
bid = P × (1 - s/2)
ask = P × (1 + s/2)
```

El cierre de trades se usa como **referencia central estimada**; no se afirma que
sea el mid del libro. Ask estima el lado comprador BUY y bid el lado vendedor
SELL. Se valida bid > 0, ask > 0 y bid <= ask. Spread se aplica una sola vez.
No se usan high/low para mejorar un precio, no se fabrica trayectoria intraminuto.

Cálculos con Context Decimal nuevo de 50 dígitos, ROUND_HALF_EVEN, límites de
exponente extendidos y traps explícitos. No heredan ni cambian contexto global,
no redondean a ticks/steps y no cuantizan cantidades LIVE.

Slippage permanece separado: esta fase **no lo aplica** al bid/ask, al sizing ni
a un fill. Para 4A-2B queda un coste de ejecución futuro (conceptualmente, BUY
respecto al ask y SELL respecto al bid, en bps), sujeto a límites de orden y
revisión. Aplicarlo al Quote y volver a aplicarlo al fill duplicaría el coste.
Cambiar slippage modifica la identidad de ejecución aunque el Quote sea igual.

## Tres escenarios y sensibilidad

| Perfil | Ambigüedad DCA/TP | Futuro uso |
| --- | --- | --- |
| FAVORABLE | Hipótesis TP primero como cota favorable | Solo sensibilidad; ni el toque ni la hipótesis prueban un fill. |
| BASE | NO_SEQUENCE_ASSUMED | Con datos insuficientes se conserva la incertidumbre, sin secuencia automática. BASE no representa mercado real calibrado. |
| ADVERSO | Hipótesis DCA primero como cota conservadora | Evalúa sensibilidad al riesgo; no fuerza un fill DCA ni una pérdida inventados. |

assess_ambiguity recibe candidatos explícitos, no genera candidatos/fills desde
OHLC. Devuelve hipótesis, ambiguous, sequence_observed=False y fill_authorized=False;
los dos últimos flags no son configurables para autorizar una ejecución.

Cada perfil define spread, slippage, latencia, vigencia, elegibilidad LIMIT,
participación, parciales, hipótesis y assumptions. Las reglas LIMIT disponibles
son TOUCH_IS_CANDIDATE_ONLY, STRICT_CROSS_IS_CANDIDATE_ONLY y
REQUIRE_ADDITIONAL_EVIDENCE. Todas siguen sin demostrar profundidad/cola/ejecución.
Las reglas parciales son ALLOW_PARTIAL_ONLY_WITH_EVIDENCE y
WITHHOLD_IF_PARTIAL_REQUIREMENT_UNKNOWN. Cola y profundidad permanecen desconocidas
para todos los perfiles; ningún escenario autoriza un fill por sí mismo.

ExecutionProfiles exige los tres escenarios exactamente una vez y los ordena
canónicamente. Valida costes comparables: spread/slippage/latencia no decrecientes
FAVORABLE→BASE→ADVERSO; vigencia y participación no crecientes; restricciones
LIMIT/parciales no menos estrictas en los perfiles más adversos. Es un orden de
supuestos comparables, no una promesa de mejores/peores resultados financieros.
No hay aleatoriedad, ninguna semilla oculta ni reinversión.

## Liquidez: contrato propuesto para 4A-2B

LiquidityPolicy exige `0 < max_volume_participation <= 1` y regla de parciales.
assess_liquidity necesita volumen de una vela ya cerrada y cantidad solicitada.

```text
modeled_volume_cap = candle.volume × max_volume_participation
```

Este valor es una **cota de participación modelada**, no volumen garantizado al
precio LIMIT ni cantidad ejecutada. within_volume_cap clasifica una cantidad
como dentro/fuera de esa cota; no demuestra que una orden pequeña se ejecute.
Volumen cero da capacidad cero. Una orden grande puede quedar parcial o no
realizada en el futuro simulador; no se escoge esa ejecución todavía.

Se conservan queue_unknown=True, price_depth_unknown=True y fill_authorized=False.
No se inventa libro de órdenes, posición en cola ni prioridad frente a otros
operadores. El volumen total de trades no equivale a liquidez al precio LIMIT.
La futura participación debe ser una cota **compartida por todas las ejecuciones
modeladas del minuto**, no una capacidad completa reutilizada por cada orden.
Asignación de esa cota, reservas, prioridad y evidencia de parciales quedan pendientes.
Una hipótesis favorable/adversa nunca sustituye evidencia mínima de elegibilidad.

## Configuración e identidad de ejecución

Se puede construir ExecutionPolicy/ExecutionProfiles directamente o cargar JSON
con `profiles_from_object()` / `load_profiles(Path)`. Schema version 1 exige
`schema_version` y lista `profiles`; cada perfil exige todos los campos de la tabla.
Decimal se expresa como **texto** en JSON; los tiempos como enteros. Se rechazan
campos faltantes/desconocidos, claves duplicadas, secretos, float/bool, enums
inválidos y perfiles incoherentes. No se cargan variables de entorno ni credenciales.

Ejemplo completamente documentado y **solo de tests**:
`tests/unit/replay/fixtures/execution_profiles.json`. Sus números son fixtures de
sensibilidad sin fuente de calibración, no parámetros definitivos ni observaciones
de Binance. El modelo de producción/biblioteca no usa ese archivo por defecto.

ExecutionPolicy.identity incluye cada parámetro/contrato y assumptions, con
normalización Decimal exacta sin pérdida por el contexto global. ExecutionProfiles
es estable ante permutación de perfiles. ModeledQuotes.identity incorpora versión
del algoritmo, símbolo, policy.identity y dataset_identity explícito.
El operador debe identificar un conjunto de datos auditado; dataset_identity no
es un checksum automático ni una certificación de integridad de años de SQLite.
La vista efectiva sigue protegida por la transacción estable de SQLiteHistory.

## Integración con ReplayRunner y reanudación

ModeledQuotes implementa QuoteSource y la extensión opcional ExecutionQuoteSource.
Exige la misma sesión SQLiteHistory que el runner. NoQuotes y FixtureQuotes
siguen funcionando sin esa extensión; sus huellas históricas 4A-1 se conservan.

Se mantienen acción exactamente T+120, timestamp de Quote igual al reloj/acción,
PAUSE/cierre/gap antes de la acción, advance_time compartido e invalidación
fail-closed si no hay Quote. AltBEngine recibe la misma Quote de dominio y solo
puede producir intenciones teóricas. Sin hechos externos no hay Order, Fill,
P0 efectivo, escalera activada ni resultado financiero.

La identidad modelada se registra al iniciar el replay, incluso sin usar Quotes.
ReplaySnapshot conserva execution_identity y price_evidence en su checksum
integral; ReplayReport expone ambos y modeled_intents, separado de fixture_intents.
La evidencia de precios incluye el cierre usado y sus tiempos de validez.
Restaurar con otro perfil, parámetros o dataset se rechaza por la identidad;
usar otra sesión SQLite también se rechaza. Con mismos inputs, el continuo y
fragmentado son exactamente equivalentes en estado, evidencia, emisiones y huella.
No se añade recovery persistente, deserialización, CLI operativa ni modo LIVE.

## Límites, calibración y siguiente fase

Faltan para simular fills: motor de elegibilidad/cola, asignación de volumen
compartido, aceptación/cancelación y sus latencias, parciales, shortfill y aplicación
única de slippage, precios LIMIT protegidos, política explícita de incertidumbre y
tests de reconciliación con el mismo motor. No basta tocar high/low.

Faltan para PnL realista: fills verificables/modelados auditables, fees, funding,
mark y margen, tratamiento de exposición/errores, costes completos y comparación
de sensibilidad. Add Margin/liquidación tampoco se simulan en esta fase.

Calibrar requiere fuentes verificables de bid/ask/trades, timestamps de evento y
recepción, unidades, período/mercado comparables, calidad/gaps, justificación de
spread/slippage/latencias, validación fuera de muestra y versionado de supuestos.
Separar observaciones de hipótesis; documentar incertidumbre y revisiones sin
reescribir resultados previos. No calificar BASE como observado por su nombre.

La entrega se detiene para revisión antes de 4A-2B. No se descarga histórico,
se conecta Binance privado, se despliega VPS, se instalan servicios ni se ejecuta trading.

## Continuación implementada — Fase 4A-2B

Los límites anteriores describen 4A-2A y su módulo de precios, que sigue sin crear
fills. Un componente separado y opt-in implementa ahora ejecución modelada,
latencias explícitas, presupuesto compartido y evidencia MODELED_EXECUTION:
[SIMULATED_EXECUTION_SPEC.md](SIMULATED_EXECUTION_SPEC.md). El slippage MARKET se
aplica una vez sobre ask; LIMIT usa un buffer de elegibilidad y precio igual al
límite, sin cargarlo de nuevo. AltBEngine conserva todas sus fórmulas y conciliación.
Las reglas no son observaciones calibradas; no se implementa PnL.

La contabilidad posterior de los fills recibidos pertenece exclusivamente al
ledger opt-in de 4A-3A: [FINANCIAL_LEDGER_SPEC.md](FINANCIAL_LEDGER_SPEC.md).
El módulo de precios conserva sus contratos y no calcula resultados financieros.
