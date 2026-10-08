"""HTTPS público oficial con TLS, streaming y reintentos acotados."""

import json
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from http.client import HTTPMessage
from pathlib import Path
from types import TracebackType
from typing import IO, BinaryIO, Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from trading_engine.binance.klines import from_ms, validate_symbol
from trading_engine.models.values import Symbol

ARCHIVE_BASE = "https://data.binance.vision"
REST_BASE = "https://fapi.binance.com/fapi/v1/klines"
ALLOWED_HOSTS = {"data.binance.vision", "fapi.binance.com"}


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> Request | None:
        target, original = urlsplit(newurl), urlsplit(req.full_url)
        if (
            target.scheme != "https"
            or target.hostname != original.hostname
            or target.port
            or target.path != original.path
            or target.username
            or target.password
        ):
            raise ValueError("Redirección fuera del origen oficial")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HTTPStream(Protocol):
    def read(self, size: int = -1) -> bytes: ...
    def geturl(self) -> str: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


def open_official(request: Request, timeout: int) -> HTTPStream:
    return cast(HTTPStream, build_opener(OfficialRedirect()).open(request, timeout=timeout))


class PublicHTTP:
    def __init__(
        self,
        *,
        attempts: int = 3,
        timeout: int = 20,
        sleeper: Callable[[int], None] = time.sleep,
    ) -> None:
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError("attempts debe estar entre 1 y 5")
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError("timeout debe estar entre 1 y 60 segundos")
        self.attempts, self.timeout, self.sleeper = attempts, timeout, sleeper

    @contextmanager
    def _open(self, url: str) -> Iterator[BinaryIO]:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in ALLOWED_HOSTS
            or parts.port
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise ValueError("Solo HTTPS oficial Binance")
        if parts.hostname == "fapi.binance.com" and parts.path != "/fapi/v1/klines":
            raise ValueError("Solo endpoint público Futures klines")
        if parts.hostname == "data.binance.vision" and not parts.path.startswith(
            "/data/futures/um/"
        ):
            raise ValueError("Solo archivos oficiales USDⓈ-M Futures")
        for attempt in range(self.attempts):
            try:
                response = open_official(
                    Request(url, headers={"User-Agent": "SHib-alt-b-data/1"}), timeout=self.timeout
                )
            except (HTTPError, URLError, TimeoutError) as exc:
                retryable = (
                    not isinstance(exc, HTTPError) or exc.code == 429 or 500 <= exc.code < 600
                )
                if not retryable or attempt + 1 == self.attempts:
                    raise
                self.sleeper(min(2**attempt, 8))
                continue
            with response:
                final = urlsplit(response.geturl())
                if final.scheme != "https" or final.hostname != parts.hostname:
                    raise ValueError("Redirección fuera del origen oficial")
                yield cast(BinaryIO, response)
            return
        raise RuntimeError("Reintentos agotados")

    def download(self, url: str, target: Path) -> None:
        try:
            with self._open(url) as response, target.open("wb") as output:
                shutil.copyfileobj(response, output, length=65_536)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def text(self, url: str) -> str:
        with self._open(url) as response:
            value = response.read(1_048_577)
        if len(value) > 1_048_576:
            raise ValueError("Respuesta excede el límite de 1 MiB")
        return value.decode("utf-8")

    def klines(
        self, symbol: str, start_ms: int, end_ms: int, limit: int = 1500
    ) -> list[list[object]]:
        validate_symbol(Symbol(symbol))
        if type(start_ms) is not int or type(end_ms) is not int or start_ms > end_ms:
            raise ValueError("Rango REST inválido")
        from_ms(start_ms)
        from_ms(end_ms)
        if type(limit) is not int or not 1 <= limit <= 1500:
            raise ValueError("limit debe estar entre 1 y 1500")
        query = urlencode(
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            }
        )
        value: object = json.loads(self.text(f"{REST_BASE}?{query}"), parse_float=Decimal)
        if not isinstance(value, list) or any(not isinstance(row, list) for row in value):
            raise ValueError("Respuesta REST Futures no es una lista de klines")
        return cast(list[list[object]], value)
