# Entrega — registro y conciliación histórica offline, Fase 4A-5B

Base aprobada `0a3c153df02527cca7ffbc021d0f11c2a73d779a`.
Feature `phase-4a-5b-historical-dataset-registry`.
Runtime **STOPPED**. Datasets reales **DATASET_NOT_PROVIDED**.
Sin merge, despliegue, red Binance, trading o resultados históricos financieros.

## Resultado y contratos

Siete archivos nuevos amplían exclusivamente `replay/historical_validation/`.
HistoricalDatasetRegistry reutiliza auditoría/parsers/archivos de 4A-5A, conserva
declaraciones y originales, genera identidades y segmentos reproducibles y usa
SQLite separada. HistoricalQuery entrega candidatos read-only por cutoff.
HistoricalReconciliation consolida y conserva conflictos; readiness separa cada
requisito económico. No se ha cambiado ninguna interfaz financiera o estratégica.

Arquitectura/esquema y uso: [HISTORICAL_DATA_REGISTRY_SPEC.md](HISTORICAL_DATA_REGISTRY_SPEC.md).
Reglas de prefijos, cobertura y conflictos:
[HISTORICAL_RECONCILIATION_SPEC.md](HISTORICAL_RECONCILIATION_SPEC.md).

Esquema STRICT1, transacciones atómicas, FK/PK, índices temporales y lectores
mode=ro/query_only. COMPLETE_IMPORT significa importación confirmada, no cobertura.
imports conserva último estado e import_events mantiene fallos anteriores al
recovery. Repeticiones idénticas no duplican registros ni confirma segmentos fallidos.

Trade no tiene una segunda tabla autoritativa: el registro guarda índices y
locators; normalize_kline y archivos originales suministran las Candle candidatas.
Inventarios externos SQLite reutilizan audit_sqlite sin copiar ni modificar datos
productivos y conservan su bloqueo causal de manifiestos globales. No se adoptan
bases operativas WAL como artefactos sellados ni se modifica su journal.

Mark, funding y reglas se almacenan aparte, con categoría, DatasetId, fuente,
integridad y tiempos. Mark Close sólo después del cierre; funding no implica pago
ni periodicidad8h; brackets necesitan vigencia explícita; fees CURRENT_ONLY o sin
vigencia verificada no se usan retrospectivamente. No se conecta ningún adaptador
al Replay ni se habilita OBSERVED o SOURCE_AUTHENTICATED.

Original hash, manifiesto y sellos se reconstruyen en lectura; cambiar registros,
auditoría, identidad o segmentos bloquea incluso con un checksum recalculado.
Una fuente distinta crea otra versión. Consultas fijas no cambian al registrarla.
Duplicados numéricos conservan procedencia y una única economía; contradicciones
bloquean en vez de preferir mensual/diario. El digest causal excluye el archivo global.

## Fixtures A–R y seguridad

| Caso | Resultado validado por las pruebas |
|---|---|
| A/B/C | Mes abril sintético:41760 minutos en mensual incompleto; gap de1440; diario complementario concilia43200/43200,100%. |
| D | Archivos idénticos o Decimal equivalente: una economía, ambas fuentes, duplicados separados. |
| E | OHLC, volumen, taker-buy y columnas auxiliares distintas: CONFLICTED_SAME_TIMESTAMP, intervalo bloqueado. |
| F | Contrato/símbolo USDT/Spot/unidades incompatibles rechazados. |
| G | Mark Close antes de cierre no se entrega; después conserva OHLC Mark y trayectoria UNKNOWN. |
| H/I | Funding con inventario declarado coincide; sin inventario sigue incompleto y no se convierte en cero. |
| J | Brackets solapados/anidados bloquean; expirados no cubren otro rango. |
| K | CURRENT_ONLY/UNKNOWN_HISTORY/DOCUMENTED_AT_LAUNCH no autorizan fees históricos. |
| L | Reimportación con mismos inputs verifica y devuelve mismos IDs, sin duplicar hechos. |
| M/N | Excepción real durante COMMIT revierte dataset/filas/segmentos; retry confirma una vez y conserva auditoría del fallo. |
| O | Trade/Mark/funding/brackets/fees/filtros mantienen eventos, orden, cobertura y digest del prefijo al añadir futuros válidos. |
| P | Fuente Trade SQLite sellada permanece idéntica byte a byte; no se copian sus Candle. |
| Q | Ausencia de datasets: DATASET_NOT_PROVIDED, categorías MISSING; no backtest financiero certificado. |
| R |168 archivos previos de fuentes/tests tienen SHA-256 idéntico a la base; suite completa conserva fingerprints anteriores. |

También: checksum incorrecto, ZIP corrupto/bomb/traversal/miembros inesperados,
fuente cambiada durante el stream o antes de commit, OHLC/timestamps/JSON inválidos,
valores no finitos, límites, corrupción SQLite, identidad/segmento/auditoría alterados,
funding incompleto, falta de publicación, informe de conciliación adulterado,
contexto Decimal hostil, ausencia de red y categorías independientes.
Los tests anteriores no se borraron, deshabilitaron, editaron o relajaron.

## Validaciones de entrega

87 tests nuevos:81 de contratos/seguridad/causalidad y6 de escala/recuperación.
La base tiene2205 tests. Se excluyen los runs preliminares/interrumpidos de los resultados de entrega.

| Validación | Resultado |
|---|---|
| pytest específico host | **87 passed en 485.34s** sobre la implementación final. |
| pytest completo host | **2292 passed en 1178.48s**: 2205 existentes y 87 nuevos. |
| Docker específico offline | **87 passed en 571.39s**, sin warnings, red deshabilitada. |
| Docker completo | No ejecutado; no se presenta la selección como suite completa. |
| Ruff format --check . | 177 archivos formateados. |
| Ruff check . | All checks passed. |
| mypy | Sin problemas en 177 archivos. |
| git diff --check y --cached --check | Sin errores. |

Docker utiliza imagen de validación 4A-5A ya disponible, mismas dependencias,
COPY de fuentes/tests actuales y PYTHONPATH explícito. Build --network none
--pull=false; contenedor --network none --read-only --tmpfs /tmp y cacheprovider
desactivado. El constructor clásico evita escribir cache buildx fuera del workspace;
no se reemplaza configuración Docker, proxy, CA o credenciales y no hace descargas.

Comandos host:

```sh
.venv/bin/python -m pytest -q tests/unit/replay/test_historical_registry.py tests/unit/replay/test_historical_registry_benchmark.py
.venv/bin/python -m pytest -q
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
git diff --check
```

## Benchmarks exclusivamente sintéticos

Cada escenario corre en proceso nuevo: CSV streaming, segmentos diarios explícitos,
consulta completa, conciliación, reabrir/reconstruir identidad y prefijo de3 minutos.
RSS es máximo de todo el proceso (runtime/caches incluidos), no sólo memoria Python.
SQLite bytes es tamaño final del registro, sin sumar CSV original o índices de
auditoría temporales. Hay suites concurrentes: tiempos diagnósticos, no garantías.

| Días | Registros | Registro s | Conciliación s | Registros/s | Pico RSS KiB | SQLite bytes | Consulta s | Recovery s | Prefijo3 min s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
|1|1440|2.7385|2.1027|525.83|21392|835584|3.2312|1.0629|2.2058|
|7|10080|6.2451|5.4569|1614.07|22376|5394432|14.4769|8.5906|22.6973|
|30|43200|83.0421|63.9321|520.22|22516|22872064|125.4015|18.2486|82.6636|

Todos los escenarios reconocieron exactamente3 eventos en el prefijo y conservaron
identidades tras recovery. Memoria del proceso:aproximadamente21–22MiB en estas
mediciones. El tiempo de30 días y la consulta por prefijo revelan un coste significativo
de reconstrucción/revalidación del artefacto. No se presenta como un motor listo para
años de datos; requiere revisión de verificación por segmento/caché autenticada.
Las variaciones de throughput/latencia incluyen contención de suites concurrentes.

Reproducir en directorios **nuevos** fuera de Git:

```python
from dataclasses import asdict
from pathlib import Path
from trading_engine.replay.historical_validation.registry_benchmark import benchmark_registry
for days in (1,7,30):
    print(asdict(benchmark_registry(days, Path(f"/tmp/registry-review-{days}"))))
```

Los consumidores son streaming y el heap/grupos están acotados; no hay una lista
Python de todos los minutos ni journal financiero por vela. Los índices/segmentos
crecen en SQLite. Reconstruir integridad requiere fuentes completas: consulta por
prefijo y recuperación **también** pagan ese coste, aunque entreguen sólo3 registros.
Trade CSV/ZIP no tiene acceso aleatorio físico indexado. No se extrapolan mediciones
sintéticas a un replay financiero multianual. Los motores previos siguen acumulando
journals/snapshots; requieren una revisión futura de checkpoints segmentados.

## Archivos y Git

Sólo12 archivos nuevos:7 en historical_validation,2 de tests y estos3 documentos.
Los168 archivos previos de código/tests se congelaron mediante SHA-256 antes de
editar y permanecen exactamente iguales; también se conserva toda documentación previa.
main debe seguir en la base aprobada. Un único commit de implementación sobre main,
publicación normal exclusivamente feature. El SHA se reporta fuera del propio commit.
No se incluyen CSV/ZIP/checksums/SQLite reales o sintéticos, logs o temporales en Git.

## Faltantes y conexión posterior

1. Datasets reales locales con originales y evidencia independiente de procedencia.
2. Verificador autenticado de proveedor; SHA/CRC/checksum local no prueban Binance.
3. Publicación por evento, vigencia de reglas/brackets/fees y roles efectivos del usuario.
4. Inventario funding histórico auténtico y atribución económica por exposición/ronda.
5. Revisión causal separada de manifiestos Trade SQLite antes de habilitar su candidato.
6. Autorización económica de cada fuente/Reference; no se cambia OBSERVED aquí.
7. Política auditada de correcciones y decisiones de preferencia entre fuentes.
8. Optimización revisada de verificación por segmento y locators físicos, checkpoints,
   atomicidad del consumidor y recuperación eficiente de motores financieros.
9. Trayectoria intraminuto, prioridad real de liquidación, reservas y ejecución del
   exchange: siguen desconocidas; no existe supervivencia o rentabilidad certificada.

No se han aportado datos reales ni autenticidad independiente. Los candidatos
read-only no son todavía fuentes financieras autorizadas. No se ejecutó ningún
backtest real, histórico2024–2026, API Binance, saldo privado, orden o transferencia.
No hay servicios, VPS, Telegram, LIVE ni Shadow. Detenerse para auditoría.
**NO MERGE. NO DESPLIEGUE. NO TRADING.**
