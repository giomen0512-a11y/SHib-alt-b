# Entrega 4A-4B — reservas y transferencias simuladas

Feature: `phase-4a-4b-reserve-transfer-simulation`.
Base aprobada: `ab4604b40ba66d837bed9114ecdf585b2961db72`.
Runtime STOPPED. Sin merge, despliegue, datos externos ni trading.

## Implementación y contratos

Nuevo módulo offline `reserve_transfer_simulation`: políticas explícitas registradas en el journal con capital y límites,
evidencia de capital/precios/acciones, particiones, OrderMarginReservation,
solicitudes/cola, conciliación y controller. Consume cálculos de ledger,
performance y riesgo existentes; no modifica AltB, Base/DCA/TP, fills, mantenimiento
ni simulador. Reservas MODELED por remanente/notional dividido por4. MARKET exige
referencia causal explícitamente seleccionada; reduce_only SELL no retiene
margen de apertura. Cancelar una intención no libera: el terminal necesita FillIds
exactos. El margen efectivo de fills puede exigir libre adicional; insuficiencia
no fabrica fondos ni altera la ejecución confirmada.

El wallet se divide entre libre, aislado, holds de órdenes/transferencias y otras
partidas declaradas. Todas deben ser conocidas antes de publicar libre. Fees,
funding y PnL son los de las capas previas; unrealized no financia transferencias.
Se concilian saldos en precisión50 con compromisos agrupados, evitando acumulación
de redondeos de deltas. No se añade una partida desconocida para cuadrar balances.

AutoPolicy desactivada por defecto; límites/target/latencias/timeout obligatorios.
Sólo aporte completo, NO_RETRY y EXACT_DECIMAL (precisión50, sin cuantización
externa). Estado PROPOSED/FUNDS_HELD/REQUEST_PENDING/CONFIRMATION_PENDING y terminal
CONFIRMED_SIMULATED/REJECTED/EXPIRED. Confirmar genera ADD modelado con links a
request/hold; rechazo/timeout libera una vez. El wallet total no cambia por mover
collateral. Una ronda cerrada impide confirmar.

Fuente/journal de riesgo extendidos opt-in mediante ConfirmedMarginSource; no se
eluden los validadores. ReplayRunner integra ticks/orden causal, snapshot e informe
opcionales. Recovery reconstruye desde fuentes originales antes de validar, incluso
ante checksum recalculado. IDs causales no dependen de un rechazo futuro.

## Ejemplos sintéticos A–J

| Caso | Resultado |
|---|---|
| A | Wallet1000 = libre650 + aislado250 + holdorden100. |
| B | Confirmar100: libre550, aislado350, holdorden100, holdtransfer0; wallet1000, PnL0. |
| C | Pendiente100: libre550, aislado250, holdorden100, holdtransfer100; wallet1000. |
| D | Rechazar: libre650, aislado250, holdorden100; libera una vez, wallet1000. |
| E | Mark80: unrealized-200, margen50, MMR8, buffer42. Mark75 a+60s: margen0, MMR7.50, buffer-7.50. Confirmar100 a+120s deja buffer92.50 y conserva primer breach a+60s. |
| F | Confirmar100 a+30s: con Mark75 a+60s, aislado350, margen100, MMR7.50, buffer92.50; sin breach en muestras, intraminuto UNKNOWN. |
| G | Libre60/necesidad100: REJECTED INSUFFICIENT_FREE_BALANCE; no hold ni wallet negativo. |
| H | LIMIT4@100 ejecuta1: aislado250→275, hold100→75, libre650 sin doble débito. Cancelar remanente deja libre725, aislado275, hold0, exposición11. |
| I | cancel_request mantiene100; CANCELED conciliado libera100: libre750, aislado250, hold0. |
| J | Base+DCA7, DCA parcial, TP previo parcial/cancelado, TP sustituto y cierre: exposición0, holds0, principal asignado=liberado, ADD interno100 único; wallet idéntico al baseline financiero. |

En J, wallet final = `3232.4364564442078651979651598917383035341219514255`,
libre = `3144.0806367476221039162358883780550942980050232814`,
aislado = `88.355819696585761281729271513683209236116928144118`.
Principal asignado y liberado =
`1200.0124583333270767943115747423419586981333540750` cada uno.
Ese principal usa precios de fills confirmados; no redefine el presupuesto de
dimensionamiento de órdenes1200. Holds finales0 y aporte100 confirmado una vez.

J utiliza el capital **explícito3200** del fixture aprobado de performance; no es
un default del programa. Las otras pruebas declaran1000 o el capital de su caso.
El collateral adicional/costos retenidos al cierre siguen la política previa:
no se fabrica retiro de margen. Shortfill Base con volumen0/1 conserva exactamente
estrategia, ejecución, ledger, performance y riesgo del baseline; .05 de exposición
confirmada permanece aun con estrategia bloqueada, y datos financieros faltantes
mantienen libre NOT_AVAILABLE. No se fabrica SELL ni se asigna toda la cantidad
solicitada como margen.

## Seguridad, causalidad y compatibilidad

Se prueban duplicados/propuestas contradictorias, confirmación temprana/fuera de
orden/mayor o menor al hold, timeout y NO_RETRY, rechazo fixture futuro,
latencia de envío separada, solicitudes incompatibles, terminal sin fills,
mercado sin precio/futuro, cancelación pendiente, fill parcial y exceso de margen
sobre hold, fondos insuficientes, cierre mientras espera, Mark/bracket/costos
faltantes u obsoletos, buffer cero y breach previo, reparto único de fees/funding,
partición inicial falsa/ausente/UNKNOWN, tipos estrictos, contexto Decimal hostil,
fuentes incompatibles, rechazo de latencias no representables, wallet financiero reconocido
preservado ante declaración divergente, y ausencia de imports de red/clientes/comandos.

Evidencia de breach previo permanece después de confirmar. Se etiqueta continuación
contrafactual; CONFIRMED_BEFORE_SAMPLED_BREACH no certifica liquidación evitada.
Los snapshots comparan continuo/fragmentado antes y después de confirmar; adulterar
particiones, requests, ajustes, queue, FillIds, cursor, journal o riesgo falla aun
con checksum recalculado. Se conservan todas las pruebas anteriores.

El fingerprint activo de 4A-4A se congeló **antes de editar código** en
`tests/unit/replay/fixtures/reserve_disabled_4a4a_baseline.json`:
reporte, snapshot, event digest, ledger, performance, simulador y estrategia.
Sin controller los siete fingerprints permanecen idénticos. Con controller,
reserva/riesgo pueden diferir y una latencia añade ticks de auditoría si procede;
los hechos estratégicos/de ejecución y los cálculos financieros no cambian.

## Validación

Resultados finales:

| Comando | Resultado |
|---|---|
| pytest específico de reservas/replay | 61 passed; 215.83s. Casos numéricos/seguridad solos:39 passed. |
| pytest completo | 2125 passed; 706.90s. Las2064 anteriores +61 nuevas. |
| Ruff format --check . | 156 files already formatted; exit0. |
| Ruff check . | All checks passed; exit0. |
| mypy | Success: no issues found in156 source files; exit0. |
| git diff --check | Sin errores; exit0. |
| Docker tests --network none | 2125 passed; 938.46s; exit0. |

Docker usa la imagen de dependencias ya disponible y copia src/tests actuales;
build --network none --pull=false y ejecución --network none --read-only con /tmp
como tmpfs. No necesita descarga de paquetes o imágenes ni inicia runtime/servicios.

## Archivos, Git y límites

Nuevos: siete archivos en `src/trading_engine/replay/reserve_transfer_simulation/`,
dos archivos de tests, un fixture de fingerprint y los dos documentos de esta fase.
Extensiones localizadas: replay/models.py, replay/runner.py,
isolated_margin_risk/core.py e isolated_margin_risk/evidence.py. No se editan otras
fases, dependencias, estrategia, ejecución, ledger o performance.

Un único commit de implementación se publica sólo en feature mediante push normal.
Su SHA se obtiene con `git rev-parse phase-4a-4b-reserve-transfer-simulation`; el
informe final lo incluye sin crear un commit adicional para autorreferenciarlo.
Comparación contra `origin/main` aprobado: un commit de implementación, sin merge.
La referencia local main estaba desactualizada al iniciar y permanece intacta;
no se movió ni publicó main. No se realizó despliegue.

No hay problemas económicos silenciados: datos o asignaciones desconocidas y
conflictos bloquean disponibilidad/autorización. No se implementan aportes
parciales, reintentos, extrapolación EVIDENCE_ONLY, ni liberación automática de
collateral adicional. Reanudar con controller reconstruye todo el prefijo: costo
intencional de validación, sin promesa de rendimiento ni recovery LIVE.

Faltan calibración/verificación independiente de balances, reservas, latencias,
prioridad de liquidación, MMR histórico 1000SHIBUSDC, transferencias bajo volatilidad
y precios intraminuto. No hay backtest2024–2026, rentabilidad histórica validada ni
prueba de supervivencia Binance. Revisión posterior antes de cualquier ampliación.
**Detenerse para revisión; NO MERGE, NO DESPLIEGUE, NO TRADING.**
