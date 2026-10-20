"""AUD-5D4-001: pérdida de confianza dentro de la construcción, antes del yield."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest

from trading_engine.replay.encoding import encoded
from trading_engine.replay.historical_validation import funding_source, segment_session
from trading_engine.replay.historical_validation.funding_history import PreparedFundingHistory
from trading_engine.replay.historical_validation.funding_source import HistoricalFundingSource
from trading_engine.replay.historical_validation.segment_session import SealedSegmentSession
from trading_engine.replay.historical_validation.verification import SessionPolicy, VerificationMode

from .test_historical_funding import STOP, T, inventory, publication, registered, registration, row

STRICT = VerificationMode.STRICT_FULL
SEALED = VerificationMode.SEALED_SEGMENT_SESSION
CASES = [
    (STRICT, "prepared_close", "PREPARED_FUNDING_NOT_AVAILABLE"),
    (SEALED, "prepared_close", "PREPARED_FUNDING_NOT_AVAILABLE"),
    (SEALED, "session_close", "VERIFICATION_NOT_AVAILABLE"),
    (SEALED, "ttl", "SESSION_EXPIRED"),
    (SEALED, "source_changed", "SOURCE_CHANGED"),
    (SEALED, "real_ttl", "SESSION_EXPIRED"),
]


@contextmanager
def subject(
    folder: Path, mode: VerificationMode, fault: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[HistoricalFundingSource, Callable[[], None]]]:
    inv = inventory(0, 1, 2)
    reg = registration(folder, [row(i) for i in range(3)], inv)
    path, ids = registered(folder, reg)
    with context_session(path, ids, mode, fault) as active:
        with PreparedFundingHistory(
            path, ids, T, STOP, cutoff=STOP, inventory=inv, mode=mode, session=active
        ) as history:

            def invalidate() -> None:
                if fault == "prepared_close":
                    history.close()
                elif fault == "session_close":
                    assert active is not None
                    active.close()
                elif fault == "ttl":
                    assert active is not None
                    expired = active._started + active.policy.max_seconds + 1
                    monkeypatch.setattr(segment_session, "monotonic", lambda: expired)
                elif fault == "real_ttl":
                    assert active is not None
                    # Reloj, inicio, política y validadores productivos permanecen intactos.
                    remaining = active._started + active.policy.max_seconds - monotonic()
                    assert remaining > 0, "La sesión debe entrar vigente en el constructor"
                    sleep(remaining + 0.02)
                    assert monotonic() - active._started > active.policy.max_seconds
                else:
                    assert fault == "source_changed"
                    with Path(reg.original_file).open("a") as stream:
                        stream.write("\n")

            yield HistoricalFundingSource(history, publication()), invalidate


@contextmanager
def context_session(
    path: Path, ids: tuple[str, ...], mode: VerificationMode, fault: str
) -> Iterator[SealedSegmentSession | None]:
    if mode is STRICT:
        yield None
    else:
        with SealedSegmentSession(
            path,
            ids,
            session_identity="aud-5d4-001-fixture",
            opened_at=STOP,
            policy=SessionPolicy(
                2 if fault == "real_ttl" else 3600, 1000, "Local synthetic immutable fixture"
            ),
        ) as active:
            yield active


@pytest.mark.parametrize("mode,fault,error", CASES)
@pytest.mark.parametrize("prefix_count", [0, 1])
def test_no_delivery_when_trust_is_lost_during_event_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: VerificationMode,
    fault: str,
    error: str,
    prefix_count: int,
) -> None:
    with subject(tmp_path, mode, fault, monkeypatch) as (source, invalidate):
        expected = tuple(source.read(0, STOP))
        identity, token = source.identity, source.history_session
        original = funding_source.HistoricalFundingRateEvent
        candidates = []

        def construct(*args: Any, **kwargs: Any) -> funding_source.HistoricalFundingRateEvent:
            value = original(*args, **kwargs)
            candidates.append(value)
            if len(candidates) == prefix_count + 1:
                # Invalida después de crear el objeto y antes de devolverlo al generador.
                source.check()
                invalidate()
            return value

        monkeypatch.setattr(funding_source, "HistoricalFundingRateEvent", construct)
        stream = source.read(0, STOP)
        delivered = [next(stream) for _ in range(prefix_count)]
        frozen_prefix = encoded(tuple(delivered))
        assert source.identity == identity and source.history_session is token
        with pytest.raises(ValueError) as caught:
            delivered.append(next(stream))
        assert str(caught.value) == error
        assert delivered == list(expected[:prefix_count])
        assert encoded(tuple(delivered)) == frozen_prefix
        assert candidates == list(expected[: prefix_count + 1])
        cursor = delivered[-1].sequence + 1 if delivered else 0
        assert cursor == prefix_count
        with pytest.raises(StopIteration):
            next(stream)
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            next(source.read(cursor, STOP))
        with pytest.raises(ValueError, match="NOT_AVAILABLE"):
            source.inventory_at(STOP)


@pytest.mark.parametrize("mode,fault,error", CASES)
def test_inventory_rejects_trust_lost_during_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: VerificationMode,
    fault: str,
    error: str,
) -> None:
    with subject(tmp_path, mode, fault, monkeypatch) as (source, invalidate):
        original = funding_source.FundingInventoryPublication

        def construct(*args: Any, **kwargs: Any) -> funding_source.FundingInventoryPublication:
            value = original(*args, **kwargs)
            source.check()
            invalidate()
            return value

        monkeypatch.setattr(funding_source, "FundingInventoryPublication", construct)
        with pytest.raises(ValueError) as caught:
            source.inventory_at(STOP)
        assert str(caught.value) == error


@pytest.mark.parametrize("mode", [STRICT, SEALED])
def test_constructor_error_is_not_replaced_after_trust_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: VerificationMode
) -> None:
    with subject(tmp_path, mode, "prepared_close", monkeypatch) as (source, invalidate):
        stream = source.read(0, STOP)
        prefix = next(stream)
        original = funding_source.HistoricalFundingRateEvent
        failure = OSError("original construction failure")

        def construct(*args: Any, **kwargs: Any) -> funding_source.HistoricalFundingRateEvent:
            original(*args, **kwargs)
            invalidate()
            raise failure

        monkeypatch.setattr(funding_source, "HistoricalFundingRateEvent", construct)
        with pytest.raises(OSError) as caught:
            next(stream)
        assert caught.value is failure
        assert prefix.sequence == 0
