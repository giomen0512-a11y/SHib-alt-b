import configparser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


class UnitParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


@pytest.mark.parametrize(
    "name,command", [("history", "history-run"), ("update", "update-once"), ("audit", "audit-full")]
)
def test_systemd_examples_are_finite_stopped_and_use_existing_db(name: str, command: str) -> None:
    unit = UnitParser(interpolation=None, strict=False)
    unit.read(ROOT / "deploy" / "systemd" / f"shib-data-{name}.service")
    service = unit["Service"]
    assert service["Type"] == "oneshot" and service["User"] == "ubuntu"
    assert service["Environment"] == "TRADING_MODE=STOPPED"
    assert service["ExecStartPre"] == "/usr/bin/test -s ${SHIB_DATA_DB}"
    assert "trading_engine.market_data.cli " + command in service["ExecStart"]
    assert service["ReadWritePaths"] == "/var/lib/shib-alt-b"
    assert service["MemoryMax"] == "768M" and service["NoNewPrivileges"] == "true"
    assert "Install" not in unit  # Importación inicial no se habilita en cada boot.
    if name == "history":
        assert service["RestartPreventExitStatus"] == "1 2 73"
        assert unit["Unit"]["StartLimitBurst"] == "3"
    else:
        assert "Restart" not in service  # La siguiente activación del timer reintenta.


def test_timers_resume_after_reboot_and_target_correct_service() -> None:
    for name in ("update", "audit"):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(ROOT / "deploy" / "systemd" / f"shib-data-{name}.timer")
        assert unit["Timer"]["Persistent"] == "true"
        assert unit["Timer"]["Unit"] == f"shib-data-{name}.service"
    assert not tuple((ROOT / "deploy" / "systemd").glob("*.target"))
