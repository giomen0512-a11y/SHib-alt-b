"""Comandos de datos públicos explícitos y finitos; no cambia el runtime STOPPED."""

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from trading_engine.binance.klines import DEFAULT_SYMBOL, LAUNCH, MARKET, from_ms, reference_ms
from trading_engine.binance.public_data import PublicHTTP
from trading_engine.market_data.audit import AuditReport, audit
from trading_engine.market_data.history import (
    Archive,
    backfill,
    import_archive,
    plan_archives,
    sync_once,
    sync_range,
    validate_range,
)
from trading_engine.models.values import Symbol
from trading_engine.persistence.futures_sqlite import FuturesSQLite, Source
from trading_engine.utils.time import require_utc


def utc_argument(value: str) -> datetime:
    try:
        return require_utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Se requiere fecha ISO-8601 UTC con Z o +00:00") from exc


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Histórico público Binance USDⓈ-M Futures 1m")
    commands = root.add_subparsers(dest="command", required=True)
    for name in (
        "plan",
        "import-archive",
        "backfill",
        "sync-once",
        "recover-range",
        "audit",
        "status",
    ):
        command = commands.add_parser(name)
        command.add_argument("--db-path", type=Path, default=Path("data/futures.sqlite3"))
        command.add_argument("--symbol", default=DEFAULT_SYMBOL.value)
        command.add_argument(
            "--start",
            type=utc_argument,
            required=name in {"backfill", "sync-once", "recover-range"},
        )
        command.add_argument(
            "--end", type=utc_argument, required=name in {"backfill", "recover-range"}
        )
        command.add_argument(
            "--as-of",
            type=utc_argument,
            help="Referencia UTC explícita; por defecto reloj UTC del operador",
        )
        command.add_argument("--dry-run", action="store_true")
        if name == "import-archive":
            command.add_argument("--archive-file", type=Path, required=True)
            command.add_argument("--source", choices=["monthly", "daily"], required=True)
            command.add_argument("--period", required=True)
            command.add_argument("--checksum-file", type=Path)
    return root


def report_dict(report: AuditReport) -> dict[str, object]:
    return {
        **asdict(report),
        "complete": report.complete,
        "missing_minutes": sum(g.missing_minutes for g in report.gaps),
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        symbol = Symbol(args.symbol)
        as_of = args.as_of or datetime.now(UTC)
        start = args.start or LAUNCH
        end = args.end or from_ms(reference_ms(as_of) // 60_000 * 60_000)
        archive: Archive | None = None
        if args.command == "import-archive":
            archive = Archive(symbol, args.period, Source(args.source))
            start = args.start or max(
                archive.start, LAUNCH if symbol == DEFAULT_SYMBOL else archive.start
            )
            end = args.end or archive.end
        validate_range(symbol, start, end, as_of)
        result: dict[str, object] = {
            "command": args.command,
            "market": MARKET,
            "symbol": symbol.value,
            "as_of": as_of.isoformat(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "db_path": str(args.db_path),
            "dry_run": args.dry_run,
        }
        if args.command == "plan" or args.dry_run:
            result["archives"] = [
                {"url": a.url, "source": a.source.value, "period": a.period}
                for a in plan_archives(symbol, start, end, as_of=as_of)
            ]
            result["rest_policy"] = (
                "Solo rangos ausentes y minutos cerrados; sin red en plan/dry-run"
            )
            print(json.dumps(result))
            return 0
        with FuturesSQLite(args.db_path, read_only=args.command in {"status", "audit"}) as store:
            if args.command == "status":
                first, last, count = store.bounds(symbol)
                result.update(
                    {
                        "first_persisted_open_ms": first,
                        "last_persisted_open_ms": last,
                        "rows": count,
                        "manifest": store.manifests(),
                    }
                )
            elif args.command == "import-archive":
                assert archive is not None
                checksum = None if args.checksum_file is None else args.checksum_file.read_text()
                imported = import_archive(
                    store,
                    args.archive_file,
                    archive,
                    as_of=as_of,
                    start=start,
                    end=end,
                    checksum_text=checksum,
                )
                result["import"] = asdict(imported)
                result["audit"] = report_dict(audit(store, symbol, start, end, as_of=as_of))
            else:
                client = PublicHTTP()
                if args.command == "audit":
                    report = audit(store, symbol, start, end, as_of=as_of)
                elif args.command == "backfill":
                    report = backfill(store, client, symbol, start, end, as_of=as_of)
                elif args.command == "recover-range":
                    report = sync_range(store, client, symbol, start, end, as_of=as_of)
                else:
                    report = sync_once(store, client, symbol, start, end, as_of=as_of)
                result["audit"] = report_dict(report)
        print(json.dumps(result))
        audited = cast(dict[str, object], result.get("audit", {"complete": True}))
        return 0 if audited["complete"] else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "data_command_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
