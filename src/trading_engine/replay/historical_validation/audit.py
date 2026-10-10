"""Auditoría streaming acotada con índice temporal efímero, sin escribir fuentes."""

import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path

from trading_engine.binance.klines import (
    CSV_HEADER,
    LAUNCH,
    from_ms,
    minute_ms,
    reference_ms,
    to_ms,
)
from trading_engine.market_data.audit import Gap
from trading_engine.replay.encoding import encoded
from trading_engine.replay.financial_ledger.policy import ledger_context
from trading_engine.utils.time import require_utc

from .contracts import (
    DEFAULT_LIMITS,
    Authentication,
    DatasetDeclaration,
    DatasetType,
    Limits,
    exact_decimal,
)
from .local_files import expected_checksum, file_digest, local_rows
from .parsers import FundingRate, milliseconds, parse_record, record_times
from .report import DatasetAudit, DatasetManifest, MonthCoverage, SemanticAudit
from .rules import FeeRules, HistoricalBrackets, RuleQuality, SymbolRules


@dataclass(frozen=True)
class FundingInventory:
    """Inventario externo explícito; no infiere calendario desde las tasas recibidas."""

    start: datetime
    end: datetime
    expected_timestamps: tuple[datetime, ...]
    source: str
    market_available_at: datetime

    def __post_init__(self) -> None:
        for at in (self.start, self.end, self.market_available_at):
            require_utc(at)
        if self.start < LAUNCH or self.start >= self.end or self.market_available_at < self.end:
            raise ValueError("Inventario funding inválido/no publicado al terminar el período")
        if type(self.expected_timestamps) is not tuple or not self.source.strip():
            raise ValueError("Inventario exige tuple y fuente")
        previous = None
        for at in self.expected_timestamps:
            require_utc(at)
            if not self.start <= at < self.end or (previous is not None and at <= previous):
                raise ValueError("Inventario desordenado/duplicado/fuera del período")
            previous = at


def _months(start: datetime, end: datetime, limits: Limits) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    current = start
    while current < end:
        if len(result) >= limits.max_months:
            raise ValueError("max_months excedido")
        boundary = datetime(
            current.year + (current.month == 12),
            1 if current.month == 12 else current.month + 1,
            1,
            tzinfo=UTC,
        )
        stop = min(boundary, end)
        result.append((current, stop))
        current = stop
    return result


def _numeric_identity(value: object) -> object:
    """Igualdad numérica exacta, sin normalize() sujeto al contexto del llamador."""
    if isinstance(value, Decimal):
        sign, digits, exponent = value.as_tuple()
        assert isinstance(exponent, int)
        if not any(digits):
            return (0, (0,), 0)
        stop = len(digits)
        while digits[stop - 1] == 0:
            stop -= 1
        return sign, digits[:stop], exponent + len(digits) - stop
    if is_dataclass(value) and not isinstance(value, type):
        return (
            type(value).__name__,
            tuple(
                (field.name, _numeric_identity(getattr(value, field.name)))
                for field in fields(value)
            ),
        )
    if isinstance(value, tuple):
        return tuple(_numeric_identity(v) for v in value)
    return value


def audit_rows(
    rows: Iterable[object],
    declaration: DatasetDeclaration,
    *,
    as_of: datetime,
    limits: Limits = DEFAULT_LIMITS,
    funding_inventory: FundingInventory | None = None,
) -> tuple[SemanticAudit, tuple[MonthCoverage, ...]]:
    """Huella causal independiente del hash del archivo completo y su adquisición."""
    require_utc(as_of)
    kind = declaration.dataset_type
    if kind in {DatasetType.EXECUTION, DatasetType.CAPITAL, DatasetType.HIGH_FREQUENCY}:
        raise ValueError("Categoría sólo catalogada; uso económico no autorizado")
    start, end = declaration.period_start, min(declaration.period_end, as_of)
    if kind in {DatasetType.TRADE, DatasetType.MARK}:
        end = from_ms(reference_ms(end) // 60_000 * 60_000)
    if end <= start:
        raise ValueError("No hay intervalo cerrado causal para auditar")
    periods = _months(start, end, limits)
    counts = [[0, 0, 0] for _ in periods]  # únicos, duplicados, conflictos por mes
    errors: list[str] = []
    gaps: list[Gap] = []
    duplicates = conflicts = count = 0
    first = last = previous = None
    cursor = start
    causal = True
    digest = sha256()
    funding_count = 0
    inventory_ok = funding_inventory is not None
    if funding_inventory is not None:
        if len(funding_inventory.expected_timestamps) > limits.max_rows:
            raise ValueError("Inventario funding excede max_rows")
        inventory_ok = (
            funding_inventory.start == start
            and funding_inventory.end == end
            and funding_inventory.market_available_at <= as_of
        )

    def finding(text: str) -> None:
        if len(errors) + len(gaps) >= limits.max_findings:
            raise ValueError("max_findings excedido; auditoría bloqueada")
        errors.append(text)

    def gap(left: datetime, right: datetime) -> None:
        if len(errors) + len(gaps) >= limits.max_findings:
            raise ValueError("max_findings excedido; auditoría bloqueada")
        gaps.append(Gap(to_ms(left), to_ms(right)))

    # Índice auxiliar en disco, nunca otra tabla del pipeline ni copia de las velas.
    with tempfile.TemporaryDirectory(prefix="shib-historical-audit-") as temporary:
        connection = sqlite3.connect(str(Path(temporary) / "identities.sqlite3"))
        connection.execute("PRAGMA cache_size=-1024")
        connection.execute("CREATE TABLE seen (at TEXT PRIMARY KEY, digest BLOB NOT NULL)")
        try:
            for row_number, row in enumerate(rows, 1):
                if row_number > limits.max_rows:
                    raise ValueError("max_rows excedido")
                if row_number == 1 and isinstance(row, list) and tuple(row) == CSV_HEADER:
                    continue
                try:
                    effective, available = record_times(row, kind)
                    if effective >= end or available > as_of:
                        continue
                    temporal = kind in {
                        DatasetType.SYMBOL_RULES,
                        DatasetType.BRACKETS,
                        DatasetType.FEES,
                    }
                    if effective < start and not temporal:
                        raise ValueError("Registro anterior al rango/lanzamiento")
                    record = parse_record(row, kind, as_of=as_of)
                    if temporal:
                        assert isinstance(record, (SymbolRules, HistoricalBrackets, FeeRules))
                        if record.reference.valid_until <= start:
                            continue
                    extra: tuple[object, ...] = ()
                    if kind is DatasetType.TRADE:
                        assert isinstance(row, list)
                        extra = (
                            exact_decimal(row[7], "quote_volume"),
                            milliseconds(row[8]),
                            exact_decimal(row[10], "taker_buy_quote_volume"),
                            exact_decimal(row[11], "ignore"),
                        )
                    identity_payload = _numeric_identity((record, extra))
                    payload = sha256(encoded(identity_payload)).digest()
                    month = next(
                        i for i, (a, b) in enumerate(periods) if a <= max(start, effective) < b
                    )
                    identity = effective.isoformat()
                    old = connection.execute("SELECT digest FROM seen WHERE at=?", (identity,))
                    found = old.fetchone()
                    if found is not None:
                        if found[0] == payload:
                            duplicates += 1
                            counts[month][1] += 1
                        else:
                            conflicts += 1
                            counts[month][2] += 1
                            finding(f"row {row_number}: contenido contradictorio")
                        continue
                    if previous is not None and effective < previous:
                        finding(f"row {row_number}: orden temporal no ascendente")
                    connection.execute("INSERT INTO seen VALUES (?, ?)", (identity, payload))
                    previous = effective
                    count += 1
                    counts[month][0] += 1
                    first = effective if first is None else min(first, effective)
                    last = effective if last is None else max(last, effective)
                    digest.update(encoded((effective, available, identity_payload)))
                    if kind in {DatasetType.TRADE, DatasetType.MARK}:
                        if effective > cursor:
                            gap(cursor, effective)
                        cursor = max(cursor, available)
                    elif isinstance(record, FundingRate):
                        if funding_inventory is not None and inventory_ok:
                            expected = funding_inventory.expected_timestamps
                            if (
                                funding_count >= len(expected)
                                or expected[funding_count] != effective
                            ):
                                inventory_ok = False
                        funding_count += 1
                        if record.mark_price is None:
                            finding(f"row {row_number}: precio funding no disponible")
                    else:
                        assert isinstance(record, (SymbolRules, HistoricalBrackets, FeeRules))
                        for i, (a, b) in enumerate(periods):
                            if (
                                i != month
                                and record.reference.valid_from < b
                                and a < record.reference.valid_until
                            ):
                                counts[i][0] += 1
                        begins = max(start, record.reference.valid_from)
                        if begins > cursor:
                            gap(cursor, begins)
                        if begins < cursor:
                            finding(f"row {row_number}: vigencias solapadas")
                        cursor = max(cursor, min(end, record.reference.valid_until))
                        if (
                            record.reference.market_available_at > record.reference.valid_from
                            or record.reference.quality
                            in {RuleQuality.CURRENT, RuleQuality.UNKNOWN, RuleQuality.LAUNCH}
                        ):
                            causal = False
                except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                    finding(f"row {row_number}: {exc}")
            if kind is not DatasetType.FUNDING and cursor < end:
                gap(cursor, end)
        finally:
            connection.close()
    funding_status = "NOT_APPLICABLE"
    if kind is DatasetType.FUNDING:
        inventory_ok = bool(
            inventory_ok
            and funding_inventory is not None
            and funding_count == len(funding_inventory.expected_timestamps)
        )
        funding_status = (
            "DECLARED_INVENTORY_MATCHED_NOT_AUTHENTICATED"
            if inventory_ok and count and not duplicates and not conflicts and not errors
            else "FUNDING_HISTORY_INCOMPLETE"
        )
    complete = count > 0 and not gaps and not errors and not conflicts
    if kind is DatasetType.FUNDING:
        complete = complete and inventory_ok and not duplicates
    semantic = SemanticAudit(
        start,
        end,
        count,
        first,
        last,
        duplicates,
        conflicts,
        tuple(errors),
        tuple(gaps),
        complete,
        causal and not errors,
        digest.hexdigest(),
        funding_status,
    )
    months: list[MonthCoverage] = []
    for (a, b), (present, duplicate, conflict) in zip(periods, counts, strict=True):
        expected_count: int | None = None
        month_gaps = tuple(
            Gap(max(g.start_ms, to_ms(a)), min(g.end_ms, to_ms(b)))
            for g in gaps
            if g.start_ms < to_ms(b) and g.end_ms > to_ms(a)
        )
        if kind in {DatasetType.TRADE, DatasetType.MARK}:
            expected_count = (minute_ms(b) - minute_ms(a)) // 60_000
        elif kind is DatasetType.FUNDING and funding_inventory is not None and inventory_ok:
            expected_count = sum(a <= t < b for t in funding_inventory.expected_timestamps)
        percent = None
        if expected_count:
            with localcontext(ledger_context()):
                percent = Decimal(present) * 100 / Decimal(expected_count)
        state = "NOT_VERIFIED"
        if conflict or errors:
            state = "CONFLICTED"
        elif not present:
            state = "MISSING"
        elif not causal:
            state = "NOT_VERIFIED"
        elif month_gaps or (kind is DatasetType.FUNDING and not complete):
            state = "PARTIAL"
        elif expected_count is not None and present < expected_count:
            state = "PARTIAL"
        else:
            state = "AVAILABLE"
        months.append(
            MonthCoverage(
                a.strftime("%Y-%m"),
                kind,
                a,
                b,
                expected_count,
                present,
                month_gaps,
                duplicate,
                conflict,
                percent,
                "NOT_VERIFIED",
                Authentication.UNKNOWN,
                state,
            )
        )
    return semantic, tuple(months)


def audit_file(
    path: Path,
    declaration: DatasetDeclaration,
    *,
    validation_time: datetime,
    as_of: datetime,
    file_format: str,
    limits: Limits = DEFAULT_LIMITS,
    member: str | None = None,
    checksum_file: Path | None = None,
    funding_inventory: FundingInventory | None = None,
) -> DatasetAudit:
    require_utc(validation_time)
    if validation_time < declaration.acquisition_time:
        raise ValueError("Validación anterior a adquisición")
    calculated = file_digest(path, limits)
    expected = (
        None if checksum_file is None else expected_checksum(checksum_file, path.name, limits)
    )
    if expected is not None and calculated != expected:
        raise ValueError("Checksum contradictorio; archivo rechazado")
    semantic, months = audit_rows(
        local_rows(path, limits, file_format=file_format, member=member),
        declaration,
        as_of=as_of,
        limits=limits,
        funding_inventory=funding_inventory,
    )
    # Segunda huella detecta cambios durante lectura sin convertir un hash en autenticidad.
    if file_digest(path, limits) != calculated:
        raise ValueError("Archivo cambió durante auditoría")
    integrity = "INTEGRITY_VALIDATED" if expected is not None else "IDENTITY_CALCULATED_ONLY"
    contract = declaration.contract
    manifest = DatasetManifest(
        declaration.dataset_id,
        declaration.dataset_type,
        contract.exchange,
        contract.market,
        contract.symbol.value,
        contract.contract_type,
        contract.settlement_asset,
        declaration.interval,
        semantic.start,
        semantic.end,
        declaration.source_url_declared,
        declaration.source_documentation,
        str(path.resolve()),
        path.name if member else None,
        calculated,
        expected,
        "MATCH" if expected is not None else "NOT_PROVIDED",
        Authentication.DECLARED,
        declaration.acquisition_time,
        validation_time,
        declaration.schema_version,
        declaration.unit_contract,
        semantic.rows,
        semantic.first_timestamp,
        semantic.last_timestamp,
        semantic.gaps,
        semantic.duplicates,
        semantic.conflicts,
        "AVAILABLE_FOR_REVIEW" if semantic.coverage_complete else "NOT_AVAILABLE",
        integrity,
        "COVERAGE_COMPLETE" if semantic.coverage_complete else "INCOMPLETE",
        "CAUSALLY_USABLE" if semantic.causally_usable else "NOT_CAUSALLY_USABLE",
    )
    from dataclasses import replace

    months = tuple(
        replace(m, integrity=integrity, authenticity=Authentication.DECLARED) for m in months
    )
    return DatasetAudit(
        manifest,
        semantic,
        months,
        (
            "SOURCE_NOT_INDEPENDENTLY_AUTHENTICATED",
            "ECONOMIC_USE_NOT_AUTHORIZED",
            "UNKNOWN_INTRAMINUTE_PATH",
            "NO_SURVIVAL_CERTIFICATION",
        ),
    )


def coverage_calendar(
    audit: DatasetAudit,
    start: datetime,
    end: datetime,
    *,
    limits: Limits = DEFAULT_LIMITS,
) -> tuple[MonthCoverage, ...]:
    """Meses anteriores al contrato son PRE_CONTRACT, nunca datos ausentes."""
    require_utc(start)
    require_utc(end)
    if start >= end:
        raise ValueError("Calendario inválido")
    result: list[MonthCoverage] = []
    for a, b in _months(start, end, limits):
        if a < LAUNCH:
            pre_end = min(b, LAUNCH)
            result.append(
                MonthCoverage(
                    a.strftime("%Y-%m"),
                    audit.manifest.dataset_type,
                    a,
                    pre_end,
                    0,
                    0,
                    (),
                    0,
                    0,
                    None,
                    "NOT_APPLICABLE",
                    Authentication.UNKNOWN,
                    "PRE_CONTRACT",
                )
            )
            a = pre_end
        if a >= b:
            continue
        found = next((m for m in audit.months if m.start == a and m.end == b), None)
        if found is None:
            # No extrapola ni recorta estadísticas sin reauditar el intervalo solicitado.
            found = MonthCoverage(
                a.strftime("%Y-%m"),
                audit.manifest.dataset_type,
                a,
                b,
                None,
                0,
                (),
                0,
                0,
                None,
                "NOT_VERIFIED",
                Authentication.UNKNOWN,
                "NOT_VERIFIED",
            )
        result.append(found)
    return tuple(result)
