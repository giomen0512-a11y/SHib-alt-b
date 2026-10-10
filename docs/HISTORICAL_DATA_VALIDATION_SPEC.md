# Auditoría histórica offline — Fase 4A-5A

Base: `9ed5a05f813855ae239331b7829adb9e960ed8ef`.
Runtime **STOPPED**. Biblioteca independiente, opt-in, sin clientes, descargas,
credenciales, trading, transferencias ni adaptación hacia evidencia financiera.
Los ejemplos y benchmarks utilizan exclusivamente fixtures sintéticos.

## Arquitectura y compatibilidad

`replay/historical_validation/` contiene:

| Archivo | Responsabilidad |
|---|---|
| contracts.py | Contrato exacto, categorías, unidades, declaración y límites. |
| catalog.py | Inventario económico y referencias declaradas. |
| local_files.py | CSV/JSON/JSONL y ZIP locales, checksum y límites. |
| parsers.py | Trade, Mark y funding; disponibilidad separada de adquisición. |
| rules.py | Vigencia de filtros, brackets, fees y ajustes declarativos por usuario. |
| audit.py | Auditoría streaming, identidad de eventos y cobertura mensual. |
| sqlite_audit.py | Auditoría read-only del esquema Futures aprobado. |
| report.py | Manifiestos, matriz mensual y readiness por métrica. |
| benchmark.py | Mediciones de 1/7/30 días exclusivamente sintéticos. |

No se modifican `AltBEngine`, Base/DCA/TP, simulador, ledger, performance, riesgo,
reservas, ReplayRunner ni el pipeline. No cambian tablas o fingerprints previos.
No hay un segundo normalizador Trade: `normalize_kline` sigue siendo la fuente
de validación. SQLite reutiliza `FuturesSQLite(read_only=True)` y `market_data.audit`.
El parser Mark reutiliza únicamente las invariantes de precios/OHLC del modelo,
mediante una instancia transitoria que no publica como Candle Trade.

El índice `seen(at,digest)` vive en SQLite **temporal**, fuera de la fuente,
con cache de 1 MiB. Permite reconocer duplicados no adyacentes sin retener años
en listas o diccionarios Python. Guarda sólo identidades y hashes, no otra tabla
persistente de velas; se elimina al terminar. Su disco crece O(registros únicos).

## Contrato y unidades

BINANCE, `BINANCE_USDM_FUTURES`, perpetual, `1000SHIBUSDC`, USDC.
Inicio documentado: **2024-03-28 07:00 UTC**, según la referencia proporcionada
en [HISTORICAL_SOURCE_CATALOG.md](HISTORICAL_SOURCE_CATALOG.md); no fue consultada.
No admite Spot, SHIBUSDC ni sustitución por 1000SHIBUSDT. No convierte cantidades,
precios o nocionales por 1000. No modifica el presupuesto de estrategia o leverage.

Precios: USDC por unidad del contrato 1000SHIB. Volumen y taker-buy: unidades
1000SHIB. Funding/MMR/fees: fracciones, no porcentajes multiplicados por 100.
Notional, cum y capital: USDC. Cada categoría exige su `unit_contract` exacto.
Las operaciones derivadas usan `ledger_context()` Decimal50/HALF_EVEN; valores
de origen conservan su Decimal textual exacto, sin redondear velas.

## Declaración, manifiesto e integridad

`DatasetDeclaration` requiere identidad, tipo, contrato, intervalo, período UTC,
fuente y documentación declaradas, adquisición, versión y unidades. Es un contrato
de entrada local, **no** evidencia autenticada del proveedor. Schema implementado:
`historical-local-v1`; intervalo 1m para Trade/Mark y EVENTS para funding/reglas.

`DatasetManifest` contiene dataset_id/type, exchange/market/symbol/contract/asset,
intervalo y período auditado, source_url_declared/documentation/file/archive,
sha256_calculated/expected, checksum_verification, provider_authenticity,
acquisition_time/validation_time, schema_version/unit_contract, row_count,
first/last_timestamp, gaps, duplicates, conflicts, availability_status y estados
independientes de integridad, cobertura, causalidad y autorización económica.

La validación usa un reloj UTC explícito y no puede preceder a adquisición.
`as_of` es un cutoff económico independiente: auditar retrospectivamente es posible
sin convertir la descarga tardía en tiempo de mercado. El hash del archivo se
calcula antes/después de leerlo; una modificación durante lectura bloquea.

Un CHECKSUM local exige exactamente SHA-256 y nombre del archivo. Coincidencia:
`INTEGRITY_VALIDATED` contra **ese checksum aportado**, no autenticidad oficial.
Contradicción: excepción y rechazo, sin omitir la verificación. Ausencia:
`IDENTITY_CALCULATED_ONLY`, `NOT_PROVIDED`. ZIP CRC tampoco autentica Binance.
No se deduce procedencia del nombre o URL. No se compara el SHA del ZIP con el
CSV descomprimido: el hash corresponde al archivo local completo indicado.

`SOURCE_DECLARED` se separa de `SOURCE_AUTHENTICATED`. El verificador independiente
de autenticidad no está habilitado en esta entrega; un manifiesto o regla que
intente declararse `SOURCE_AUTHENTICATED` se rechaza. Todos los archivos aportados
siguen sin autenticación independiente. El estado READY_FOR_VALIDATION está
definido para revisión futura, pero esta implementación no certifica autenticidad.
`Reference` financiero sigue rechazando OBSERVED. Ningún resultado autoriza
FinancialPerformance, Risk ni Replay: `NOT_AUTHORIZED_FOR_FINANCIAL_ENGINES`.

## Trade klines

Se aceptan las 12 columnas oficiales aprobadas, cabecera UM exacta opcional, CSV,
JSON REST acotado o JSONL. Símbolo de la fila array procede de la declaración
explícita: el formato Binance no incluye símbolo por fila. No es comprobación
independiente de la identidad contractual del proveedor. Los metadatos JSON
presentes no pueden contradecir mercado, exchange, settlement o categoría.

Se reutiliza la validación de ms UTC, múltiplos de60000, closeTime=open+59999,
OHLC, volumen no negativo, taker-buy<=volumen, quote/taker-quote, trades y campos.
Cierre de dominio=open+60000, intervalo `[open,open+60s)`. No hay desplazamiento.
Una vela sólo entra en el prefijo cuando se cerró. No rellena gaps, interpola
precios, convierte taker-buy ausente en cero ni altera precios/cantidades.

Conteos separan filas únicas, duplicados numéricamente idénticos, contradicciones
y desorden. Duplicados exactos se informan sin duplicar cobertura. Contradicción
o desorden bloquean suficiencia; gaps iniciales, interiores y finales permanecen.
First/last son aperturas de registros únicos reconocidos.
La identidad Decimal elimina sólo ceros finales mediante as_tuple(), sin normalize()
que dependa del contexto. Trade incluye quote volume, trades, taker-buy quote e ignore
ya validados, para detectar contradicciones aunque el Candle de dominio sea igual.

## SQLite aprobado

`audit_sqlite` exige una base local existente y una declaración Trade. Abre
`mode=ro`, query_only y BEGIN para una vista consistente. Nunca ejecuta el colector,
backfill, UPDATE, reparaciones o creación del esquema original.

Reutiliza auditoría temporal e integrity_check. Un preflight limita filas/gaps
antes de llamar al auditor aprobado, cuya colección de gaps es acumulativa.
Recorre manifiestos streaming con un límite de entradas; cuenta complete/failed/
missing, sin checksum, checksum mal formado y conflictos. Cuenta filas de otro
contrato/mercado o anteriores al lanzamiento y no publica disponibilidad si existen.
`complete` de importación no demuestra cobertura. Última fila reciente tampoco.

SQLite sólo conserva cierre normalizado: no permite revalidar el closeTime crudo
de Binance. Tampoco reautentica los ZIP originales ni considera el checksum
de `import_manifest` prueba de proveedor. El digest causal de filas ordenadas es
distinto del digest de inventario completo de manifiestos.

Los diagnósticos globales de la base/manifiestos no tienen todos los contratos
de disponibilidad necesarios para decisiones causales históricas. Se etiquetan
`BLOCKED_REQUIRES_SEPARATE_PREFIX_MANIFEST_REVIEW`. No se cambian los controles
globales aprobados para hacer parecer causal esa vista. La futura adaptación 5B
deberá revisar inventarios/recepciones de origen independientemente.

## Mark Price

Categoría y parser independientes de Trade, Index y Last. `MarkKline` contiene
instante del inicio del intervalo, disponibilidad al cerrar, OHLC Decimal,
`CLOSED_MARK_OHLC_1M`, `UNKNOWN_INTRAMINUTE_PATH`. Sólo valida la semántica OHLC de
Mark y el intervalo; campos de volumen/contadores de este formato no son volumen
Trade ni se reutilizan económicamente. Requiere 12 columnas.

`close_sample(at)` exige UTC y at>=cierre antes de entregar Mark Close. Es una
muestra del intervalo ya terminado; no entrega High/Low como ticks conocidos,
no reconstruye trayectorias o precios continuos y no genera MarkEvidence.
Gaps delimitan continuidad conocida; no hay forward-fill ni vigencia inventada.
La semántica exacta del instante de una futura muestra financiera debe aprobarse
en 5B; el objeto actual conserva intervalo y disponibilidad, no un feed de ticks.

## Funding

JSON/JSONL: symbol, fundingTime ms, fundingRate textual Decimal, markPrice opcional,
rateType opcional y `market_available_at` UTC explícito. Fuente y dataset se
conservan en el manifiesto. Disponibilidad no puede preceder al evento.
Tasa histórica no es pago del usuario, exposición ni FundingEvidence.
Mark ausente se conserva como dato faltante y bloquea suficiencia económica.

`FundingInventory` declara período, lista ordenada de timestamps esperados,
fuente y publicación después de terminar el período. No se infiere un calendario
de8h ni se deduce ausencia de omisiones mirando sólo eventos recibidos.
El inventario también es declarado, no autenticado. Eventos deben coincidir
exactamente; duplicados, omitidos, referencias incompletas o inventario inexistente
producen `FUNDING_HISTORY_INCOMPLETE`. Un período vacío no se convierte en cero.
Una fixture puede declarar un calendario cambiante explícito.

## Reglas temporales

Todas requieren symbol/margin_asset correctos, valid_from/valid_until, disponibilidad,
source/version, checksum opcional, authentication_status y quality.
Se detectan huecos, solapamientos y vigencias expiradas. Una regla anterior al inicio
del período puede cubrirlo si su vigencia sigue válida. No se inventan fechas.

`SymbolRules`: tick/step, min/max quantity, min notional, min/max price, max leverage
y contract_status. Quality separa VERIFIED_FOR_PERIOD, DOCUMENTED_AT_LAUNCH,
CURRENT_ONLY y UNKNOWN_HISTORY. Son etiquetas declaradas del fixture, no autoridad
de la fuente. CURRENT_ONLY/UNKNOWN_HISTORY o publicación posterior a vigencia no
son retrospectivamente utilizables. No redondea órdenes ni cambia estrategia.
DOCUMENTED_AT_LAUNCH tampoco basta para acreditar vigencia de un intervalo posterior.

`HistoricalBrackets` exige floor/cap/MMR/cum/maxleverage y reutiliza las invariantes
de `MaintenanceBracket`: rangos ordenados y contiguos, floor0 inicial, no mantenimiento
negativo. Incluye ajustes de usuario multiplier+scope **declarativos**, sin aplicarlos
al motor ni consultar cuentas. Una revisión deberá determinar cobertura notional
necesaria y autenticidad. Intervalos faltantes: `BRACKET_HISTORY_NOT_AVAILABLE`.

`FeeRules` exige maker/taker, user_tier, promotion, discount, fee_currency USDC y
descripción de evidencia de rol. Puede documentar rol desconocido, sin atribuirlo
a fills. Admite rebates declarados; no acepta tasas ausentes, fees monetarias float
o conversiones de otra moneda. Vigencia no cubierta: `HISTORICAL_FEES_INCOMPLETE`.
La revisión posterior deberá reconciliar promociones/descuentos y roles efectivos;
ninguna LIMIT se convierte automáticamente en maker.

## Cobertura y readiness

Matriz por mes y categoría: intervalo UTC, esperados/presentes, gaps, duplicados,
conflictos, porcentaje Decimal50, integridad, autenticidad y utilización.
Esperados Trade/Mark=duración/minuto; funding sólo con inventario explícito;
reglas temporales no inventan un número esperado de actualizaciones.
Coverage mensual distingue AVAILABLE/PARTIAL/MISSING/CONFLICTED/NOT_VERIFIED.
`coverage_calendar` separa PRE_CONTRACT; marzo comienza exactamente en lanzamiento.
No extrapola estadísticas a otro rango sin reauditarlo.

Readiness separa Trade, Mark, funding, filtros, brackets, fees, ejecución y validación
de liquidación. No produce READY global ni concatena archivos sin reconciliar
solapamientos: para declarar cobertura solicita una auditoría que cubra el período.
Sin fuente: MISSING/DATASET_NOT_PROVIDED. Otros estados: INCOMPLETE, CONFLICTED,
NOT_AUTHENTICATED, NOT_CAUSALLY_USABLE, READY_FOR_VALIDATION para revisión futura.
Liquidación permanece INCOMPLETE por intraminuto, reservas, ejecución y prioridad
del exchange no verificados. No hay rentabilidad neta/supervivencia certificadas.
Ejecución/capital/alta frecuencia se catalogan sin adaptador económico autorizado.

## Causalidad y prefijos

`market_effective_at`, `market_available_at` y adquisición son conceptos distintos.
Trade/Mark disponibles al cierre; funding/reglas requieren publicación explícita.
Los valores económicos de registros futuros se ignoran después de leer sus
metadatos de tiempo. Event digest y SemanticAudit del prefijo no incorporan hash
del archivo completo, descarga, futuro económico ni registros no disponibles.
El manifiesto del archivo completo puede cambiar de SHA al agregar datos, como debe.

Tests comparan prefijos Trade, Mark, funding, brackets y fees antes/después de datos
futuros, incluso con valores económicos futuros inválidos. No se confunde identidad
global del dataset con resultado causal. Si el timestamp no se puede localizar o
el límite/decodificación del archivo falla, la auditoría queda bloqueada y no
proclama invariancia. La conservación criptográfica de todo el archivo y su validez
de formato son validaciones globales, no señales de decisión histórica.

## Seguridad y recursos

Valores económicos requieren strings Decimal finitos, sin floats implícitos,
NaN/Infinity o exponentes excesivos. JSON rechaza keys repetidas. UTF-8 estricto,
cabecera exacta y schema por categoría. Cada archivo es local y elegido explícitamente.

Límites por defecto: archivo128MiB, descomprimido256MiB, ratio100, línea64KiB,
JSON monolítico1MiB, 1 millón de filas, 1000 hallazgos y120 meses. Son límites
de auditoría, configurables; excederlos bloquea, nunca trunca un informe como completo.
CSV/JSONL son streaming. JSON array sólo para lotes pequeños acotados; años requieren
streaming/segmentación. Memoria O(límites de línea+hallazgos+meses+cache), disco O(filas).

ZIP exige un miembro exacto plano, sin extracción; rechaza traversal, symlink,
cifrado, directorios, miembros extra, compresión fuera de STORED/DEFLATED,
ratio/tamaño excesivos y CRC corrupto. Un preflight del directorio final limita
metadatos antes de ZipFile; ZIP64 y multidisk quedan rechazados. Checksum limita4KiB.
La elección local explícita permite rutas absolutas locales; no acepta URLs como
transporte ni ejecuta comandos contenidos en los archivos.

La verificación CHECKSUM del importador existente acopla parsing y hash del archivo
sin límites de lectura configurables. Esta capa complementaria necesita sus límites
de bytes y detección de cambios antes/después; usa la misma primitiva SHA-256 bajo
esas restricciones, sin cambiar ni sustituir la integridad del pipeline aprobado.

## Uso reproducible

Ejemplo de declaración de un CSV local existente, sin descarga:

```python
from datetime import UTC, datetime
from pathlib import Path
from trading_engine.replay.encoding import encoded
from trading_engine.replay.historical_validation.audit import audit_file
from trading_engine.replay.historical_validation.contracts import (
    Contract, DatasetDeclaration, DatasetType, units,
)
from trading_engine.replay.historical_validation.report import readiness_report

start = datetime(2024, 3, 28, 7, tzinfo=UTC)
end = datetime(2024, 3, 28, 7, 3, tzinfo=UTC)
declaration = DatasetDeclaration(
    "local-trade-review", DatasetType.TRADE, Contract(), "1m", start, end,
    "local:declared-source", "operator-supplied-documentation",
    datetime(2026, 10, 9, tzinfo=UTC), "historical-local-v1", units(DatasetType.TRADE),
)
result = audit_file(
    Path("/ruta/explicitamente/aportada.csv"), declaration,
    validation_time=datetime(2026, 10, 10, tzinfo=UTC), as_of=end, file_format="csv",
)
print(encoded(result).decode())
print(encoded(readiness_report((result,), start, end)).decode())
```

Para SQLite: `audit_sqlite(path, declaration, as_of=end)`; siempre read-only.
Para ZIP añadir `member="nombre.csv"`, y opcionalmente checksum_file local.
Para Mark elegir explícitamente DatasetType.MARK y sus unidades. Funding/reglas
usan JSONL con sus contratos adicionales, sin publicación automática al replay.

## Rendimiento y siguiente revisión

Mediciones sintéticas y comandos en [PHASE_4A_5A_REPORT.md](PHASE_4A_5A_REPORT.md).
Los motores financieros actuales acumulan journals, series, muestras, evidencia,
fills y snapshots; el coste de hash/recovery depende del prefijo completo.
No se reescriben para optimizar. La auditoría no guarda journal por vela.

4A-5B requiere datasets realmente aportados y autenticación independiente, fuentes
de publicación causal, conciliación de archivos, vigencias de filtros/brackets/fees,
inventario funding y adaptación económica revisada por separado. Diseñar checkpoints
persistentes autenticados, paginación/segmentación, batch-read y reanudación eficiente.
Nada de ello se activa aquí. Ausencia de breaches muestreados no prueba supervivencia.
