import io
import json
import ssl
from email.message import Message
from http.client import HTTPMessage
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

import trading_engine.binance.public_data as http_module
from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, normalize_kline, to_ms
from trading_engine.binance.public_data import ARCHIVE_BASE, REST_BASE, OfficialRedirect, PublicHTTP


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str = REST_BASE) -> None:
        super().__init__(body)
        self.url = url

    def geturl(self) -> str:
        return self.url


def test_real_json_shape_public_futures_parameters(
    rest_json: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Request] = []

    def opened(request: Request, timeout: int) -> Response:
        calls.append(request)
        assert timeout == 20
        return Response(rest_json.encode())

    monkeypatch.setattr(http_module, "open_official", opened)
    rows = PublicHTTP().klines(DEFAULT_SYMBOL.value, to_ms(LAUNCH), to_ms(LAUNCH) + 119999)
    params = parse_qs(urlsplit(calls[0].full_url).query)
    assert params == {
        "symbol": ["1000SHIBUSDC"],
        "interval": ["1m"],
        "startTime": [str(to_ms(LAUNCH))],
        "endTime": [str(to_ms(LAUNCH) + 119999)],
        "limit": ["1500"],
    }
    assert not calls[0].has_header("X-mbx-apikey")
    candle = normalize_kline(rows[0], DEFAULT_SYMBOL, as_of=LAUNCH.replace(day=29))
    assert candle.taker_buy_base_volume is not None


@pytest.mark.parametrize("error", [429, 500, 502, 503, "timeout", "connection"])
def test_bounded_retries(error: int | str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    sleeps: list[int] = []

    def failing(request: Request, timeout: int) -> Response:
        calls.append(request.full_url)
        if isinstance(error, int):
            raise HTTPError(request.full_url, error, "fixture", Message(), None)
        if error == "timeout":
            raise TimeoutError("fixture")
        raise URLError("fixture")

    monkeypatch.setattr(http_module, "open_official", failing)
    with pytest.raises((HTTPError, TimeoutError, URLError)):
        PublicHTTP(sleeper=sleeps.append).text(REST_BASE)
    assert len(calls) == 3 and sleeps == [1, 2]


def test_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    count = 0

    def opened(request: Request, timeout: int) -> Response:
        nonlocal count
        count += 1
        if count == 1:
            raise HTTPError(request.full_url, 429, "fixture", Message(), None)
        return Response(b"[]")

    monkeypatch.setattr(http_module, "open_official", opened)
    assert PublicHTTP(sleeper=lambda _: None).text(REST_BASE) == "[]"
    assert count == 2


@pytest.mark.parametrize("code", [400, 403, 404, 451])
def test_no_retry_for_access_or_missing_file(code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    count = 0

    def opened(request: Request, timeout: int) -> Response:
        nonlocal count
        count += 1
        raise HTTPError(request.full_url, code, "fixture", Message(), None)

    monkeypatch.setattr(http_module, "open_official", opened)
    with pytest.raises(HTTPError):
        PublicHTTP(sleeper=lambda _: None).text(REST_BASE)
    assert count == 1


@pytest.mark.parametrize(
    "reason",
    [
        OSError("Tunnel connection failed: 403 Forbidden"),
        OSError("Tunnel connection failed: 451 Unavailable"),
        ssl.SSLCertVerificationError("fixture invalid certificate"),
    ],
)
def test_proxy_denial_and_invalid_tls_fail_without_retry(
    reason: OSError, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    sleeps: list[int] = []

    def failing(request: Request, timeout: int) -> Response:
        calls.append(request.full_url)
        raise URLError(reason)

    monkeypatch.setattr(http_module, "open_official", failing)
    with pytest.raises(URLError):
        PublicHTTP(sleeper=sleeps.append).text(REST_BASE)
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize(
    "url",
    [
        "http://fapi.binance.com/fapi/v1/klines",
        "https://mirror.invalid/data.zip",
        "https://fapi.binance.com:443/x",
    ],
)
def test_unofficial_urls_rejected_without_access(url: str) -> None:
    with pytest.raises(ValueError):
        PublicHTTP().text(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://fapi.binance.com/fapi/v1/order",
        "https://data.binance.vision/data/spot/file.zip",
        "https://key:secret@fapi.binance.com/fapi/v1/klines",
    ],
)
def test_private_spot_and_credentials_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        PublicHTTP().text(url)


@pytest.mark.parametrize("url", ["https://evil.invalid/file", "http://data.binance.vision/file"])
def test_redirect_rejected_before_contacting_target(url: str) -> None:
    with pytest.raises(ValueError):
        OfficialRedirect().redirect_request(
            Request(ARCHIVE_BASE), io.BytesIO(), 302, "Found", HTTPMessage(), url
        )


def test_download_streaming_and_partial_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_url = ARCHIVE_BASE + "/data/futures/um/monthly/klines/1000SHIBUSDC/1m/fixture.zip"
    body = b"x" * 140000
    response = Response(body, archive_url)
    monkeypatch.setattr(http_module, "open_official", lambda request, timeout: response)
    target = tmp_path / "fixture.zip"
    PublicHTTP().download(archive_url, target)
    assert target.read_bytes() == body and response.closed

    class Interrupted(Response):
        def read(self, size: int | None = -1) -> bytes:
            raise URLError("interrupted body")

    monkeypatch.setattr(
        http_module, "open_official", lambda request, timeout: Interrupted(b"", archive_url)
    )
    with pytest.raises(URLError):
        PublicHTTP().download(archive_url, target)
    assert not target.exists()


@pytest.mark.parametrize("body", [{"code": -1}, [1], "invalid", None])
def test_rest_response_schema_rejected(body: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        http_module, "open_official", lambda request, timeout: Response(json.dumps(body).encode())
    )
    with pytest.raises(ValueError):
        PublicHTTP().klines(DEFAULT_SYMBOL.value, to_ms(LAUNCH), to_ms(LAUNCH) + 59999)


def test_json_numeric_price_not_coerced_to_string(monkeypatch: pytest.MonkeyPatch) -> None:
    from .conftest import row_at

    row = row_at()
    row[1] = 1.25
    monkeypatch.setattr(
        http_module, "open_official", lambda request, timeout: Response(json.dumps([row]).encode())
    )
    rows = PublicHTTP().klines(DEFAULT_SYMBOL.value, to_ms(LAUNCH), to_ms(LAUNCH) + 59999)
    with pytest.raises(ValueError, match="texto Decimal"):
        normalize_kline(rows[0], DEFAULT_SYMBOL, as_of=LAUNCH.replace(day=29))


@pytest.mark.parametrize("attempts,timeout", [(0, 20), (6, 20), (True, 20), (3, 0), (3, 61)])
def test_transport_bounds(attempts: int, timeout: int) -> None:
    with pytest.raises(ValueError):
        PublicHTTP(attempts=attempts, timeout=timeout)
