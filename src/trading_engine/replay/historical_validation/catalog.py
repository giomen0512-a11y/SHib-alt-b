"""Referencias declaradas, nunca consultadas por la auditoría."""

from dataclasses import dataclass

from .contracts import DatasetType

LAUNCH_REFERENCE = (
    "https://www.binance.com/en/support/announcement/detail/28ca126f6a1e4f17a7915d8be309c32d"
)
DOCS = "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/"


@dataclass(frozen=True)
class SourceCatalogEntry:
    category: DatasetType
    reference: str
    documentation: str
    purpose: str
    restriction: str


CATALOG = (
    SourceCatalogEntry(
        DatasetType.TRADE,
        "GET /fapi/v1/klines; https://data.binance.vision/",
        DOCS + "market-data",
        "Trade OHLC/volumen 1m",
        "No demuestra fills/liquidez",
    ),
    SourceCatalogEntry(
        DatasetType.MARK,
        "GET /fapi/v1/markPriceKlines",
        DOCS + "market-data",
        "Mark OHLC 1m",
        "Sin trayectoria intraminuto",
    ),
    SourceCatalogEntry(
        DatasetType.FUNDING,
        "GET /fapi/v1/fundingRate",
        DOCS + "market-data",
        "Tasa/evento histórico",
        "No prueba pago de usuario",
    ),
    SourceCatalogEntry(
        DatasetType.SYMBOL_RULES,
        "GET /fapi/v1/exchangeInfo",
        DOCS + "market-data",
        "Filtros/estado",
        "Actual no implica histórico",
    ),
    SourceCatalogEntry(
        DatasetType.BRACKETS,
        "GET /fapi/v1/leverageBracket",
        DOCS + "account",
        "Mantenimiento",
        "USER_DATA firmado: NO acceso",
    ),
    SourceCatalogEntry(
        DatasetType.FEES,
        "Tarifas/promociones/condiciones por usuario",
        DOCS + "account",
        "Fees temporales",
        "No consultar cuenta ni inferir rol",
    ),
    SourceCatalogEntry(
        DatasetType.EXECUTION,
        "Evidencia de ejecución independiente",
        "Revisión futura",
        "Calibración",
        "OHLC no acredita ejecución",
    ),
    SourceCatalogEntry(
        DatasetType.CAPITAL,
        "Evidencia de partición independiente",
        "Revisión futura",
        "Capital/margen",
        "Sin saldos inventados",
    ),
    SourceCatalogEntry(
        DatasetType.HIGH_FREQUENCY,
        "Mark/trades alta frecuencia",
        "Revisión futura",
        "Solo catálogo",
        "No certifica supervivencia",
    ),
)
