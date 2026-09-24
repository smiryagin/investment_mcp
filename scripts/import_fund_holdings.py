"""Import normalized provider holdings exports into the global overlap dataset.

The input CSV must contain FundSymbol, AsOfDate, SourceName, HoldingSymbol,
HoldingName, and WeightPercent. HoldingKey, SourceUrl,
ReportedCoveragePercent, and IsComplete are optional. Use an administrative
connection whose database user belongs to investment_data_loader; never grant
the MCP runtime permission to replace global holdings.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

import pyodbc


ROOT = Path(__file__).resolve().parents[1]


def _load_local_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean_row(row: dict[str, str]) -> dict[str, Any]:
    fund_symbol = str(row.get("FundSymbol") or "").strip().upper()
    as_of_date = str(row.get("AsOfDate") or "").strip()
    source_name = str(row.get("SourceName") or "").strip()
    holding_symbol = str(row.get("HoldingSymbol") or "").strip().upper()
    holding_key = str(row.get("HoldingKey") or "").strip().upper()
    if not holding_key and holding_symbol:
        holding_key = f"TICKER:{holding_symbol}"
    if not fund_symbol or not as_of_date or not source_name:
        raise ValueError("FundSymbol, AsOfDate, and SourceName are required.")
    if not holding_key:
        raise ValueError("Each row requires HoldingKey or HoldingSymbol.")
    weight_percent = float(str(row.get("WeightPercent") or "").strip())
    if not 0 < weight_percent <= 100:
        raise ValueError("WeightPercent must be greater than 0 and at most 100.")
    reported_coverage = str(row.get("ReportedCoveragePercent") or "").strip()
    return {
        "FundSymbol": fund_symbol,
        "AsOfDate": as_of_date,
        "SourceName": source_name,
        "SourceUrl": str(row.get("SourceUrl") or "").strip() or None,
        "ReportedCoveragePercent": (
            float(reported_coverage) if reported_coverage else None
        ),
        "IsComplete": _truthy(row.get("IsComplete")),
        "Holding": {
            "holdingKey": holding_key,
            "holdingSymbol": holding_symbol or None,
            "holdingName": str(row.get("HoldingName") or "").strip() or None,
            "weightPercent": weight_percent,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument(
        "--connection-env",
        default="HOLDINGS_SQLSERVER_CONN",
        help="Environment variable containing the data-loader ODBC connection string.",
    )
    args = parser.parse_args()
    _load_local_env()
    connection_string = os.getenv(args.connection_env, "").strip()
    if not connection_string:
        raise SystemExit(f"Set {args.connection_env} to a data-loader connection string.")

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    metadata: dict[tuple[str, str, str], dict[str, Any]] = {}
    with args.csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream), start=2):
            try:
                cleaned = _clean_row(row)
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"CSV row {row_number}: {exc}") from exc
            key = (
                cleaned["FundSymbol"],
                cleaned["AsOfDate"],
                cleaned["SourceName"],
            )
            groups[key].append(cleaned["Holding"])
            metadata.setdefault(key, cleaned)

    imported: list[dict[str, Any]] = []
    with closing(pyodbc.connect(connection_string, autocommit=True)) as connection:
        for key, holdings in groups.items():
            details = metadata[key]
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    """
                    EXEC invest.ReplaceFundHoldingsSnapshot
                        @FundSymbol = ?,
                        @AsOfDate = ?,
                        @SourceName = ?,
                        @HoldingsJson = ?,
                        @SourceUrl = ?,
                        @ReportedCoveragePercent = ?,
                        @IsComplete = ?;
                    """,
                    key[0],
                    key[1],
                    key[2],
                    json.dumps(holdings, separators=(",", ":")),
                    details["SourceUrl"],
                    details["ReportedCoveragePercent"],
                    int(details["IsComplete"]),
                )
                while cursor.description is None and cursor.nextset():
                    pass
                columns = [column[0] for column in cursor.description]
                imported.append(dict(zip(columns, cursor.fetchone())))

    print(json.dumps({"Imported": imported}, default=str, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
