# Conciliación y consulta causal — Fase 4A-5B

Opt-in offline, runtime STOPPED, SYNTHETIC_FIXTURE en pruebas. Sin autoridad
económica ni autenticidad Binance inferida. El esquema y las fuentes se describen
en [HISTORICAL_DATA_REGISTRY_SPEC.md](HISTORICAL_DATA_REGISTRY_SPEC.md).

## Selección, duplicados y conflictos

HistoricalQuery fija un conjunto explícito, único, ordenado y acotado de DatasetIds.
No selecciona automáticamente todos los archivos nuevos. Cada fuente se verifica
contra su original y sellos; la consulta abre un snapshot SQLite de lectura.
Los cursores por fuente se combinan mediante heap merge temporal; no se ordenan
millones de objetos Python. Fuente desordenada se rechaza al registrar.

La economía se identifica por categoría, contrato implícito validado, tiempos y
valores numéricos exactos. Trade incluye OHLC, volumen/taker-buy y las columnas
auxiliares ya validadas; Mark conserva únicamente sus OHLC, sin volumen Trade.
Funding mantiene rate, referencia Mark y tipo de tasa si consta. Reglas mantienen
vigencia, disponibilidad, parámetros y calidad; source/version/checksum/etiqueta
de autenticidad quedan fuera de la igualdad económica, dentro de su procedencia.

`100` y `100.000` son equivalentes bajo cualquier contexto Decimal del llamador.
Un duplicado exacto o equivalente se entrega una sola vez, con todas las fuentes,
DatasetIds y conteo de duplicados. Un archivo distinto conserva identidad propia.

Dos economías distintas en el mismo instante: **CONFLICTED_SAME_TIMESTAMP**.
Schedules con vigencias solapadas: **CONFLICTED_RULE_VALIDITY**, incluyendo
solapamientos anidados. No se prefiere mensual, diario, fecha de descarga o fuente
declarada. No existe una opción de prioridad arbitraria. Resolver una preferencia
requiere una revisión explícita, respaldada y auditada que produzca una nueva
selección/versionado; esta entrega deja el intervalo bloqueado.

`groups()` permite auditar eventos y conflictos. `read*()` realiza un preflight
streaming y falla ante conflictos del intervalo solicitado antes de entregar una
regla como si toda su vigencia fuera incontrovertida. El prefijo anterior a la
disponibilidad del conflicto continúa consultable. No se fabrican eventos correctores.

HistoricalReconciliation produce conteos únicos, duplicados, intervalos conflictivos,
gaps, coverage_status y digest causal. Persistir un informe lo reconstruye con la
selección original y su inventario funding explícito: informes adulterados se rechazan.
Reportes/conflictos se guardan atómicamente y por identidad determinista.

## Tres tiempos separados

| Tiempo | Uso |
|---|---|
| market_effective_at | Ocurrencia del evento o inicio del intervalo. |
| market_available_at | Primera disponibilidad causal declarada/contractual. |
| dataset_acquired_at | Adquisición del archivo, en Registration.declaration.acquisition_time. |

Descargar tarde no desplaza la ocurrencia/publicación; descargar temprano no
permite anticipar un registro futuro. Una consulta incluye sólo eventos efectivos
dentro de `[start,end)` y disponibles `<= cutoff`. Trade/Mark requieren start/end
alineados al minuto; cutoff puede caer entre minutos y no habilita OHLC sin cierre.
Funding exactamente en cutoff puede incluirse si end es posterior a ese instante.
El reporte conserva requested_end y el límite de prefijo para reproducir esta regla.

Trade y Mark: apertura1m, disponibilidad al cierre exclusivo open+60s. Mark Close
no se entrega antes; High/Low siguen siendo OHLC del minuto terminado y no ticks.
No hay forward-fill, reconstrucción intraminuto ni inferencia del orden High/Low.
Funding exige publicación explícita y nunca se convierte en un pago de ronda.
Reglas exigen VERIFIED_FOR_PERIOD y publicación no posterior al inicio de vigencia.
CURRENT_ONLY, UNKNOWN_HISTORY y DOCUMENTED_AT_LAUNCH bloquean su uso retrospectivo.
Vigencia faltante no se completa; bracket expirado queda MISSING para el nuevo rango.

## Prefijos y correcciones

Digest causal = SHA-256 incremental de eventos económicos reconocidos y ordenados.
No incluye hash global de archivo, DatasetId, fecha de adquisición o futuros.
`prefix_key()` separa ese resultado del selection_id. Al agregar datos posteriores
válidos sin revisión retroactiva, IDs económicos, orden, registros, cobertura y
digest del prefijo permanecen iguales. El hash global cambia como corresponde.

La auditoría del artefacto completo sigue siendo un gate de integridad: un futuro
malformado o una fuente alterada no se convierte en un archivo sellado confiable.
No se proclama invariancia entre un archivo válido y otro corrupto. Esta comprobación
offline no introduce valores económicos futuros en el prefijo entregado.

Una corrección posterior del pasado crea otro DatasetId; no modifica consultas
fijas. Seleccionar versiones contradictorias bloquea, sin reescribir un resultado
sellado. El registro no garantiza autenticidad independiente de esa corrección.

## Cobertura e inventario funding

Trade/Mark: cada apertura cerrada cuenta una vez; todos los minutos esperados se
verifican. Gaps iniciales/interiores/finales son explícitos, sin interpolación.
Una serie con primera y última vela no es completa por esos extremos.

Funding: sólo un FundingInventory externo, publicado para el período, acredita
un **inventario declarado coincidente**, no autenticación oficial. Se persiste su
fuente, publicación, período e instantes exactos junto al informe; no se deduce
periodicidad8h. Sin inventario/referencia Mark: FUNDING_HISTORY_INCOMPLETE. Un período
vacío no es funding cero. Inventarios incompatibles de un mes se bloquean.

Matriz mensual y seis categorías: PRE_CONTRACT, AVAILABLE, PARTIAL, MISSING,
CONFLICTED, NOT_VERIFIED. Marzo empieza en lanzamiento; nada anterior se importa.
Cada RegistryCoverageRow contiene MonthCoverage, esperados/presentes, gaps,
duplicados, contradicciones, porcentaje Decimal50, checksum, autenticidad, DatasetIds,
fuentes, uso causal y autorización económica siempre pendiente. La cobertura del
prefijo no acredita el resto del mes. No se extrapolan categorías no auditadas.

Mark continuo, funding disponible, fees/brackets verificables, capital, reservas,
ejecución y trayectoria intraminuto siguen siendo condiciones independientes.
Ausencia de breaches muestreados no certifica supervivencia ni liquidación Binance.

## Recursos y límites

Lectura entrega iteradores; el índice y los datos auditados crecen en disco.
Heap acotado por fuentes (hasta1000), grupo del mismo instante hasta10000 registros,
hallazgos/gaps y meses según Limits de 4A-5A. No hay journal económico por vela:
Trade almacena identidades/locators necesarios para auditar, sin duplicar Candle.

Conservación de originales es obligatoria. CSV/ZIP Trade usan recorrido streaming
del original para resolver locators; no se promete acceso aleatorio O(1) al CSV.
Verificación de fuentes/sellos requiere recorrido del archivo completo. read* hace
preflight antes de entrega y una segunda lectura validada: tiempo de prefijo y
recovery puede crecer con todo el artefacto, aunque su memoria sea acotada.
Los benchmarks incluyen ese coste; no presentan la consulta de tres minutos como
si sólo hubiera leído tres filas físicas. No es una garantía para años de datos.

Una conexión mantiene BEGIN durante el iterador: debe consumirse o cerrarse.
El stream comprueba identidad por fila antes de entregarla y SHA al terminar.
Una interrupción o cambio impide sellar un reporte completo. Antes de conectar
adaptadores financieros deberán revisarse también atomicidad del consumidor,
checkpoints y manejo de errores después de un prefijo parcialmente entregado.
