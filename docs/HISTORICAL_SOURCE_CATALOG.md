# Catálogo histórico separado por categoría económica — 4A-5A

Contrato exclusivo: Binance USDⓈ-M Futures perpetual **1000SHIBUSDC / USDC**.
No es SHIBUSDC Spot, SHIBUSDT Spot ni 1000SHIBUSDT Futures.
Lanzamiento **documentado en la solicitud**: 2024-03-28 07:00 UTC.

Referencia de lanzamiento proporcionada:
https://www.binance.com/en/support/announcement/detail/28ca126f6a1e4f17a7915d8be309c32d

Estas referencias están declaradas; **no fueron consultadas ni descargadas** durante
esta fase. No se verifica por citarlas que un archivo local provenga del proveedor,
que sus parámetros tengan vigencia histórica o que el contrato estuviera disponible
en todos los minutos pedidos. Estado de datasets reales: **DATASET_NOT_PROVIDED**.

## Matriz de fuentes

| Categoría | Referencia declarada | Unidad y propósito | Cobertura/limitación |
|---|---|---|---|
| TRADE_KLINES_1M | GET /fapi/v1/klines; archivos UM data.binance.vision | OHLC USDC/1000SHIB; volumen/taker-buy 1000SHIB | Minutos cerrados, gaps explícitos; no fills ni profundidad. |
| MARK_PRICE_KLINES_1M | GET /fapi/v1/markPriceKlines | Mark OHLC USDC/1000SHIB | Close disponible al cierre; intraminuto UNKNOWN. |
| FUNDING_RATE_HISTORY | GET /fapi/v1/fundingRate | Fracción por evento; Mark de referencia | Inventario/calendario explícito, no intervalo fijo8h ni pago del bot. |
| SYMBOL_RULES | GET /fapi/v1/exchangeInfo | Tick/step/filtros/estado/leverage | Estado actual no prueba parámetros 2024–2026. |
| MAINTENANCE_BRACKETS | GET /fapi/v1/leverageBracket | Notional USDC, MMR fracción, cum USDC | USER_DATA firmado: prohibida su consulta aquí; vigencias y ajustes por usuario faltantes. |
| TRADING_FEE_RULES | Documentación tarifaria/promociones/condiciones | Tasas maker/taker, descuentos, moneda, nivel | No consultar usuario ni inferir maker de LIMIT; vigencia/rol efectivo faltantes. |
| ORDER_EXECUTION_EVIDENCE | Evidencia independiente por revisar | Cantidad 1000SHIB, precios USDC/1000SHIB | Sólo catálogo; ejecución del simulador permanece MODELED. |
| CAPITAL_AND_MARGIN_EVIDENCE | Particiones/evidencia independientes por revisar | USDC y partidas explícitas | Sólo catálogo; no balances, reservas ni confirms reales inventados. |
| HIGH_FREQUENCY_MARK_OR_TRADES | Dataset opcional por aportar | Tipo/precio/recepción explícitos | Sólo catálogo; no alimenta riesgo ni certifica liquidación. |

Documentación declarada de mercado:
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data

Documentación declarada de cuenta:
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account

Archivo público declarado:
https://data.binance.vision/

Los patrones de archivo de Trade ya están documentados en
[DATA_PIPELINE.md](DATA_PIPELINE.md); esta fase no descarga ni reconstruye el pipeline.
No se inventan patrones archivados de Mark/funding o fechas de cambios de reglas.

## Procedencia y autorización

1. Hash calculado: identidad del archivo.
2. Hash esperado coincidente: integridad contra el checksum local aportado.
3. URL/nombre/documentación: fuente declarada.
4. Autenticación independiente: pendiente, no habilitada por manifiestos.
5. Validación semántica: específica de categoría/contrato.
6. Cobertura: específica de período, sin sustituir datasets.
7. Causalidad: disponibilidad distinta de ocurrencia y adquisición.
8. Uso económico: revisión separada de 4A-5B.

El checksum aportado puede estar sin autenticación oficial. Un tercero puede
generar tanto archivo como checksum: coincidencia no basta para SOURCE_AUTHENTICATED.
No se habilita OBSERVED ni se publican MarkEvidence/FundingEvidence/brackets a riesgo.

## Reglas históricas y faltantes

ExchangeInfo actual sólo acredita una declaración actual, salvo evidencia de
vigencia revisada independientemente. DOCUMENTED_AT_LAUNCH no prueba ausencia de
cambios posteriores. Brackets de otro símbolo/USDT se rechazan. No se inventan MMR,
tarifas estables, promotions, descuentos o actualizaciones 2024–2026.

Funding requiere inventario completo por período y tasa/Mark por evento; una serie
vacía no es funding0. Pago económico exige exposición válida y contratos posteriores.
Fees históricos necesitan vigencia, nivel/promoción/descuento y rol por FillId.
Capital y margen necesitan evidencia conciliada; no se convierten de nocional.

Trade OHLC y taker-buy agregado no demuestran queue position, profundidad, slippage,
ejecución garantizada, orden High/Low o liquidación intraminuto. Mark1m tampoco
demuestra supervivencia entre muestras. Preservar ejecución MODELED y riesgo muestreado.

Sin archivos reales verificados, todas las categorías esenciales están MISSING.
La validación de liquidación permanece INCOMPLETE. Futuros archivos íntegros pero
sin autenticar se informarán NOT_AUTHENTICATED, aunque su cobertura sea completa.

## Preparación 4A-5B

Solicitar datasets locales explícitos, originales y checksums con procedencia
verificable; revisar autenticación separada sin pedir credenciales en esta fase.
Reconciliar solapamientos/duplicados, vigencias y contratos de disponibilidad.
Diseñar adaptadores económicos opt-in con revisión de Reference/OBSERVED, sin
convertir validación de archivos en autorización general. Diseñar escala/segmentación
y recuperación antes de cargar años en los journals financieros actuales.
Acceso firmado a datos privados requiere otra revisión; no se solicita ni activa aquí.

## Adaptación Mark opt-in — Fase 4A-5D3

La preparación histórica y la publicación financiera se separan en
[HISTORICAL_MARK_PRICE_ADAPTER_SPEC.md](HISTORICAL_MARK_PRICE_ADAPTER_SPEC.md).
Sólo fixtures explícitos de MARK_PRICE_KLINES_1M pueden publicar MarkEvidence bajo
SYNTHETIC_FIXTURE. Declaración, hashes y verificación local conservan NOT_AUTHENTICATED;
no se autoriza OBSERVED, datos reales ni liquidación/supervivencia entre muestras.
