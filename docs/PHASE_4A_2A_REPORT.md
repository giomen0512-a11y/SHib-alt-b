# Entrega Fase 4A-2A — modelo de precios y políticas

Base aprobada: `01888d2758c231ffc51a82e7c0ea99f26136031e`.
Rama de entrega: `phase-4a-2a-execution-policy`.
Especificación: [EXECUTION_MODEL_SPEC.md](EXECUTION_MODEL_SPEC.md).

## 1. Arquitectura implementada

Nuevo módulo `replay/execution_model`: políticas inmutables, configuración
estricta desde objeto/JSON, contratos analíticos de liquidez/ambigüedad y fuente
ModeledQuotes sobre la sesión SQLiteHistory de solo lectura. Se conservan
ReplayRunner, StrategyCoordinator y AltBEngine; no se modifica estrategia,
filtros, vetos, presupuesto ni fórmulas. No se añaden dependencias.

## 2. Modelo de Quote y procedencia

Cuatro categorías explícitas: REAL_OBSERVED, MODELED_FROM_1M,
SYNTHETIC_FIXTURE y UNAVAILABLE. No se implementa conector real observado.
ModeledQuoteEvidence conserva vela de referencia, disponibilidad, vencimiento,
perfil e identidades de política/dataset. Quote.timestamp es el instante de
valoración; la edad del dato se conserva separadamente. Un gap/dato inválido
produce ausencia, sin interpolación ni sustitución por una referencia anterior.

## 3. Parámetros y fórmulas

Todos obligatorios: spread_bps, slippage_bps, quote_latency_ms,
freshness_limit_ms, participación máxima, políticas LIMIT/parciales/secuencia,
perfil y explicación de supuestos. Validados desde objeto o JSON estricto;
sin secretos ni valores de mercado implícitos.

Con P igual al cierre disponible y spread completo s en bps:

```text
bid = P × (1 - s/20000)
ask = P × (1 + s/20000)
available_at = cierre + quote_latency_ms
expires_at = cierre + freshness_limit_ms
```

Decimal local de 50 dígitos, independiente del contexto global; precios positivos
y bid <= ask. Spread aplicado una vez; slippage reservado para futuros fills,
sin aplicarlo ahora a Quote ni sizing. La disponibilidad instantánea de OHLCV
en su cierre es un supuesto explícito, no una propiedad probada de Binance.

## 4. Escenarios

Los perfiles requieren parámetros propios; no hay valores de producción por
defecto. Se valida orden de costes/restricciones directamente comparables.
Estos números existen **solo en el fixture de tests**, sin calibración Binance:

| Fixture | Spread bps | Slippage bps pendiente | Latencia ms | Vigencia ms | Participación máxima |
| --- | ---: | ---: | ---: | ---: | ---: |
| FAVORABLE | 2 | 1 | 0 | 120000 | 0.10 |
| BASE | 10 | 3 | 250 | 90000 | 0.05 |
| ADVERSO | 30 | 8 | 1000 | 60000 | 0.01 |

LIMIT: toque como candidato / cruce estricto como candidato / evidencia adicional.
Parciales: requieren evidencia en favorable/base; adverso retiene ejecución
si se desconoce la necesidad de parciales. Ninguna regla autoriza fills.
BASE no representa comportamiento observado. No hay aleatoriedad.

## 5. Liquidez propuesta

Cota modelada = volumen base real de la vela cerrada × participación configurable.
Una cantidad dentro de la cota no acredita liquidez a su precio LIMIT.
Cola/profundidad desconocidas, fills no autorizados. Volumen y cantidades
conservan unidades 1000SHIBUSDC, sin convertirlas a SHIB individuales.
La futura cota debe compartirse entre todas las ejecuciones del minuto;
asignación, reservas, parciales y órdenes no realizadas quedan pendientes.

## 6. Ambigüedad intraminuto

Con candidatos DCA y TP simultáneos: favorable plantea TP primero solo como
cota de sensibilidad; base conserva secuencia desconocida; adverso plantea
DCA primero como hipótesis conservadora. No se genera trayectoria, no se afirma
secuencia observada y no se obliga a ningún fill.

## 7. Evidencia de ausencia de lookahead

Tests impiden leer rangos posteriores al instante simulado, rechazan velas
abiertas y comprueban que un cierre posterior no cambie una decisión anterior.
Una vela futura corrupta no se lee hasta cerrar. Latencia, vigencia, gaps y
límites temporales se prueban explícitamente. No se fabrica precio T+120 antes
de la consulta del runner ni se generan fills por high/low.

## 8. Integración y restauración

QuoteSource permanece compatible; ExecutionQuoteSource es una extensión opcional.
Snapshot/report incorporan identidad y evidencia de precios; la huella incluye
política incluso antes de obtener Quotes. Se rechaza restauración con otro
perfil/parámetro/dataset/sesión SQLite. Misma configuración: replay continuo y
fragmentado producen idénticos estados, emisiones, evidencia y huella.
NoQuotes/FixtureQuotes conservan sus huellas anteriores. FactSource no cambia.

## 9. Tests

106 tests nuevos, 158 específicos de replay y 1658 totales (1552 anteriores).
Los anteriores se conservan. Incluyen ausencia de red/ejecución, Quotes causales,
políticas, configuración, restauración y compatibilidad.

## 10. Verificaciones

- Pytest específico de replay: **158 passed** (140.64s).
- Pytest completo local: **1658 passed** (225.82s).
- Ruff format --check: **117 files already formatted**.
- Ruff check: **All checks passed**.
- Mypy estricto: **Success: no issues found in 117 source files**.
- git diff --check: sin errores.
- Pytest completo Docker offline: **1658 passed** (280.48s).
- Bootstrap Docker sobre código actual: **bootstrap_ready, STOPPED**.

Docker usa imagen de tests disponible, fuente actual montada read-only,
`--network none --read-only --tmpfs /tmp --cap-drop ALL`, usuario no root y
`no-new-privileges`; no se descarga ni reconstruye imagen. Son tests con fixtures,
sin replay histórico completo ni acceso al mercado.

## 11. Archivos modificados

- `src/trading_engine/replay/execution_model/{__init__,policy,config,contracts,quotes}.py`
- `src/trading_engine/replay/{ports,models,runner}.py`
- `tests/unit/replay/{test_execution_policy,test_modeled_quotes}.py`
- `tests/unit/replay/fixtures/execution_profiles.json`
- `docs/EXECUTION_MODEL_SPEC.md`
- `docs/PHASE_4A_2A_REPORT.md`
- `docs/REPLAY_SPEC.md`, `docs/ARCHITECTURE.md`, `README.md`

## 12. Commit de entrega

Un único commit en feature; el SHA exacto se comunica después de publicar y
verificar GitHub. No se incluye un SHA autorreferente dentro del propio commit.

## 13. Comparación con main

Feature parte exclusivamente del main aprobado; entrega un commit ahead,
cero behind y cero merge commits. Main permanece en el SHA aprobado.
Publicación exclusivamente de feature, sin merge ni force push.

## 14. Decisiones pendientes para 4A-2B

Calibrar parámetros con fuentes verificables; decidir elegibilidad LIMIT,
cola desconocida, asignación compartida de volumen, parciales, no ejecución,
aceptación/cancelación y latencias, aplicación única de slippage y límites de
orden. Definir política operativa ante secuencia desconocida sin transformar
las cotas de sensibilidad en hechos. Para PnL faltan también fees, funding,
mark/margen y auditoría de costes/exposición.

Runtime STOPPED. Sin fills históricos, PnL, órdenes LIVE, descargas, API privada,
servicios activados, Telegram, VPS o despliegue. Se detiene para revisión.
