import trading_engine
from trading_engine.models import OperatingMode


def test_package_imports() -> None:
    assert trading_engine.__name__ == "trading_engine"


def test_operating_modes() -> None:
    assert {mode.value for mode in OperatingMode} == {
        "STOPPED",
        "REPLAY",
        "SHADOW",
        "LIVE_ARMED",
        "LIVE",
        "SAFE_MODE",
        "ERROR",
    }
