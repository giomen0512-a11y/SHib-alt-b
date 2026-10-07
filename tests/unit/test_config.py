import pytest

from trading_engine.config import Settings, load_settings
from trading_engine.models import OperatingMode


def test_defaults_need_no_secrets() -> None:
    assert load_settings({}) == Settings(mode=OperatingMode.STOPPED)


def test_reads_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "STOPPED")
    assert load_settings().mode is OperatingMode.STOPPED


@pytest.mark.parametrize("mode", [mode for mode in OperatingMode if mode != OperatingMode.STOPPED])
def test_unimplemented_modes_fail_closed(mode: OperatingMode) -> None:
    with pytest.raises(ValueError, match="solo admite STOPPED"):
        load_settings({"TRADING_MODE": mode.value})
    with pytest.raises(ValueError, match="solo admite STOPPED"):
        Settings(mode=mode)


@pytest.mark.parametrize("mode", ["", "unknown", "live"])
def test_invalid_modes_are_not_silently_accepted(mode: str) -> None:
    with pytest.raises(ValueError):
        load_settings({"TRADING_MODE": mode})
