import ast
import sys
from pathlib import Path


def test_pure_math_package_only_depends_on_standard_library_config_and_required_models() -> None:
    package = Path(__file__).resolve().parents[4] / "src/trading_engine/strategy/alt_b"
    modules = sorted(package.glob("*.py"))
    assert {module.name for module in modules} == {
        "__init__.py",
        "config.py",
        "math.py",
        "market.py",
        "entry.py",
        "state_market.py",
    }
    allowed = {
        "trading_engine.strategy.alt_b.config": {"AltBConfig"},
        "trading_engine.models._validation": {
            "finite_decimal",
            "require_type",
            "utc_timestamp",
            "nonblank",
        },
        "trading_engine.models.market": {"Candle"},
        "trading_engine.strategy.alt_b.math": {"_context"},
        "trading_engine.strategy.alt_b.entry": {"EntryEvaluation", "evaluate_entry_checkpoint"},
        "trading_engine.strategy.alt_b.market": {
            "_consecutive_minutes",
            "_validate_candles",
            "_validate_checkpoint",
            "aggregate_15m_block",
            "decimal_median",
            "rv30m_snapshot",
        },
        "trading_engine.models.orders": {"Fill"},
        "trading_engine.models.values": {"Price", "Symbol"},
    }
    for module in modules:
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module is not None
                if node.module.startswith("trading_engine."):
                    assert node.module in allowed
                    assert {alias.name for alias in node.names} <= allowed[node.module]
                else:
                    assert node.module.split(".")[0] in sys.stdlib_module_names
            elif isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), module
                if isinstance(node.value, str):
                    assert "BuyRatioProxy" not in node.value
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"getcontext", "setcontext", "open", "float"}, module
        assert "BuyRatioProxy" not in module.read_text()
