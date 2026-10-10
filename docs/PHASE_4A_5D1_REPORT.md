# Informe Fase 4A-5D-1 — adaptador causal de histórico para Replay

Repositorio: `giomen0512-a11y/SHib-alt-b`.
Base y main preservada: `ab6a8ddeb2adfc3ad8bc4294d14a41d235b3b9c0`.
Rama de entrega: `phase-4a-5d1-historical-replay-adapter`.
Estado operativo: **STOPPED**, offline exclusivamente; sin merge ni despliegue.

## Implementación y diferencias con main

Preparación atómica de Trade mediante HistoricalQuery o sesión segmentada explícita,
conciliación completa, digest causal independiente y almacenamiento temporal SQLite acotado.
Sólo después del agotamiento exitoso —COMPLETE en QueryStream— se exponen Candle al Replay.
Una consulta incompleta, alteración, inconsistencia o conflicto bloquea la publicación.

Se conserva exactamente el contrato 1000SHIBUSDC PERPETUAL USDC 1m, Decimal local 50 y UTC.
Sin conversión por 1000, interpolación, cotizaciones derivadas, fills observados o autenticación.
La historia contiene warmup canónico 1560, cobertura, gaps y duplicados reconciliados.
Las lecturas exigen minuto cerrado y cutoff causal; el futuro no se trata como un gap.

Raíces SHA-256 por bloques de hasta 1440 filas se conservan en memoria independiente del cache.
Lectura por ordinales protegidos, máximo un bloque de Candle inmutables decodificado y SQLite
de sólo lectura. Las fuentes y el registro v1 permanecen intactos. Las cuotas predeterminadas
son 1.000.000 filas y 128 MiB de historia preparada; no son límites económicos ni defaults de capital.

La ampliación de ReplayRunner es sólo la anotación `CandleHistory`: `session`, `conflicts` y
generador `read`. Comparación AST de la lógica con main: **idéntica**. La huella previa de los
241 archivos rastreados confirmó que sólo cambian los dos archivos indicados abajo.
No cambia AltBEngine, Base/DCA/TP, configuración, simulador, ledger, performance, riesgo o reservas.

### Archivos añadidos

- `src/trading_engine/replay/historical_validation/replay_adapter.py`
- `tests/unit/replay/test_historical_replay_adapter.py`
- `tests/unit/replay/historical_replay_adapter_benchmark.py`
- `docs/HISTORICAL_REPLAY_ADAPTER_SPEC.md`
- `docs/PHASE_4A_5D1_REPORT.md`

### Archivos modificados

- `src/trading_engine/replay/ports.py`: protocolo mínimo CandleHistory.
- `src/trading_engine/replay/runner.py`: import y anotación de historia, sin cambios de lógica.

## Evidencia y contratos de bloqueo

STRICT_FULL es predeterminado y conserva verificación integral al preparar.
SEALED_SEGMENT_SESSION exige política, validación inicial y sesión vigente. Mantiene exactamente
SEGMENT_VERIFIED / SOURCE_NOT_REVALIDATED; no transforma una verificación parcial en global.

PreparedTradeEvidence conserva bindings, declaración/adquisición, auditoría, digests,
conciliación, checkpoint, cobertura y warmup. Autenticación NOT_AUTHENTICATED y autorización
financiera NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES. Mark, Funding, Fees, Brackets, Capital y ejecución
real no se adaptan y permanecen NO AUTORIZADOS; ejecución histórica NOT_AVAILABLE.
Los datos usados en pruebas y medición son SYNTHETIC_FIXTURE.

NoQuotes continúa como default. Cotizaciones/hechos sintéticos explícitos conservan sus
puertos y procedencia. No se activa ningún módulo financiero por validar Trade.
Gaps o warmup incompleto se informan sin fabricar señales; el motor aprobado conserva su propia
regla de recuperación de historia posterior. Un cutoff parcial bloquea consumo más allá de él.

## Equivalencia y recuperación

Tres fingerprints de main se congelaron **antes de editar**: sin quotes, con FixtureQuotes,
y con hechos de fill sintético e idempotencia. En ambos modos se comparan el reporte completo,
todos los eventos ordenados, timestamps, estado e integridad de snapshot, además de Candle exactas.

| Caso | Digest de eventos aprobado e idéntico |
|---|---|
| NoQuotes | `1de51441efde90d5059802f62aac9fb21926a16be4dd23b1327109f352d96746` |
| FixtureQuotes | `2ca54f97b544a9f06248d4f5144eaa58dc2f90573ad2f57204c371b1fa074b47` |
| Hechos sintéticos | `281e69e645499ff683d854d47844292a026279bacf5902e12d11be986746f484` |

Reanudación dentro de la misma historia preparada, con cortes durante warmup, inicio, señal,
antes de T+120, en T+120 y después del fill: ReplayReport y ReplaySnapshot idénticos a ejecución
continua en ambos modos. Una preparación nueva rechaza el snapshot vinculado a la historia anterior.
No se ofrece recovery de Replay entre procesos: requiere futura revisión compatible con su
contrato opaco de sesión y reconstrucción independiente de fuentes/raíces.

## Validación ejecutada

- Python 3.12.14 en host; Python 3.12.15 en Docker.
- Pruebas nuevas específicas: **59 aprobadas**, 117,73 s; sin modificar tests anteriores.
- Regresión específica de HistoricalValidation, Registry, FastQuery y ReplayRunner:
  **298 aprobadas**, 168,50 s.
- Suite completa host: **2.447 aprobadas**, 1.694,13 s (28 min 14 s):
  las 2.388 anteriores más las 59 nuevas.
- Suite completa Docker: **2.447 aprobadas**, 2.061,16 s (34 min 21 s), exit code 0;
  imagen local, build `--network=none --pull=false`, ejecución `--network=none --read-only`
  con `/tmp` temporal. NetworkMode `none` y ReadonlyRootfs `true` comprobados.
- Ruff format --check: **187 archivos conformes**.
- Ruff lint: **aprobado**.
- mypy estricto: **187 archivos aprobados**.
- git diff --check: **aprobado**.

Las pruebas específicas cubren equivalencia de velas y Replay, ambos modos, snapshots, estados
PROVISIONAL/FAILED/INTERRUPTED, digest final discordante, fuentes modificadas antes/durante lectura,
cache/registro/segmentos adulterados, conflictos, duplicados equivalentes, gaps, warmup,
cutoff, cierre, expiración, límites, limpieza, lectura sólo disponible tras preparación,
inmutabilidad, contexto Decimal hostil, sockets bloqueados y fuentes no Trade rechazadas.

Docker utiliza dependencias e imagen ya disponibles; ninguna descarga ni cambio de TLS/CA/proxy.
Buildx necesitó su carpeta de metadatos en `/tmp` por permisos del entorno; no se cambió DockerConfig
ni credenciales. Los contenedores son de pruebas, no servicios operativos del bot.

## Rendimiento sintético

Mediciones por proceso, fixtures generados localmente; warmup de 1560 adicional en todos los casos.
Los costes son wall-clock con otras validaciones concurrentes en el mismo host; no son promesas
de rendimiento ni una comparación controlada de velocidades cold/warm. Registro previo e inicial
verificación integral de sesión se muestran aparte. Preparación **incluye** su validación;
conciliación es un subconjunto, no otro importe a sumar al total.

| Intervalo | Modo | Registro s | Sesión inicial s | Preparación s | Conciliación s | Replay s | Restore s | Replay restante s |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 20 min | STRICT_FULL | 1,590 | — | 4,488 | 1,389 | 4,688 | 0,096 | 0,080 |
| 20 min | SEALED_SEGMENT_SESSION | 2,534 | 1,039 | 1,652 | 0,781 | 2,478 | 0,063 | 0,039 |
| 1 día | STRICT_FULL | 1,685 | — | 4,311 | 1,383 | 7,146 | 0,064 | 2,333 |
| 1 día | SEALED_SEGMENT_SESSION | 2,173 | 2,788 | 4,463 | 2,315 | 7,313 | 0,089 | 2,618 |
| 7 días | STRICT_FULL | 6,825 | — | 24,558 | 9,611 | 50,275 | 0,053 | 17,082 |
| 7 días | SEALED_SEGMENT_SESSION | 8,012 | 6,946 | 13,051 | 6,276 | 37,173 | 0,054 | 17,034 |

| Intervalo | Modo | Filas | RSS máximo bytes | SQLite preparado bytes | SQLite sesión bytes |
|---|---|---:|---:|---:|---:|
| 20 min | STRICT_FULL | 1.580 | 30.830.592 | 962.560 | 0 |
| 20 min | SEALED_SEGMENT_SESSION | 1.580 | 31.211.520 | 962.560 | 1.363.968 |
| 1 día | STRICT_FULL | 3.000 | 31.752.192 | 1.818.624 | 0 |
| 1 día | SEALED_SEGMENT_SESSION | 3.000 | 32.055.296 | 1.818.624 | 2.568.192 |
| 7 días | STRICT_FULL | 11.640 | 32.882.688 | 7.024.640 | 0 |
| 7 días | SEALED_SEGMENT_SESSION | 11.640 | 33.394.688 | 7.024.640 | 9.826.304 |

En las seis ejecuciones, Replay continuo y fragmentado son exactamente iguales. Entre ambos
modos también coinciden digest de eventos, SHA-256 de reporte e integridad de snapshot:

| Intervalo | Digest de eventos |
|---|---|
| 20 min | `87075a0e6ea820e122da0c42676346e3a80ba0bcbc19c9bbaa28588ff085eef6` |
| 1 día | `68c77d28f62aa6809e971e1b265c813157e6cd77c1cf24a47a1634ebc7ab9672` |
| 7 días | `b77ff73abd8d5d461885184334440635ab70ebbbe5a71afc284f2aa0a724e6a8` |

RSS máximo incluye generación, registro, preparación y Replay; no es una medición aislada del
adaptador. Los tamaños SQLite son archivos retenidos, no el pico agregado de todos los temporales.
El generador de benchmark escribe CSV incrementalmente; no almacena el histórico completo en RAM.
Cada escenario compara explícitamente ejecución continua y fragmentada.

## Limitaciones y condiciones posteriores

- El adaptador no autentica Binance. No hay OBSERVED ni SOURCE_AUTHENTICATED.
- No obtiene fills reales, no certifica liquidación/supervivencia y no calcula rendimiento histórico real.
- No hay Mark, pagos funding, fees, brackets históricos o capital autorizados de esta historia.
- ModeledQuotes mantiene su requisito concreto de SQLiteHistory. Vincularlo al nuevo puerto queda
  pendiente de revisión; no se cambió ese contrato ni se simuló herencia para eludirlo.
- Las raíces protegen respecto a archivos derivados adulterados bajo confianza en memoria/proceso;
  no son firmas independientes del proveedor. STRICT_FULL es evidencia al preparar el snapshot.
- Una sesión segmentada nueva repite validación inicial. No hereda confianza de caches/checkpoints viejos.
- El adaptador no habilita LIVE, Shadow, Binance privado, transferencias, Telegram, VPS o servicios.
- No se descargaron históricos reales ni se realizaron backtests 2024–2026. Red externa sólo para GitHub
  en operaciones de control Git; el adaptador y benchmarks no realizan acceso de red.

No se identificaron contradicciones materiales que exigieran cambiar contratos financieros
aprobados. La integración de otras categorías y recuperación persistente queda fuera de alcance.
Entrega exclusivamente en feature para auditoría externa; **NO MERGE, NO siguiente fase**.
