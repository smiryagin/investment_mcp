"""Refresh today's global reference-instrument score snapshots.

Run this after the daily dbo.SeriesData refresh.  It uses the same local-first
data path, Schwab quotas, scoring model, and snapshot procedures as the MCP.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def main() -> int:
    subject = os.getenv("MCP_SCORING_REFRESH_AUTH_SUBJECT", "").strip()
    if not subject:
        subject = os.getenv("MCP_DEFAULT_AUTH_SUBJECT", "").strip()
    if not subject:
        raise SystemExit(
            "Set MCP_SCORING_REFRESH_AUTH_SUBJECT to an active invest.Users "
            "AuthenticationSubject."
        )
    identity = server._fetch_one(
        """
        SELECT UserId
        FROM invest.Users
        WHERE AuthenticationSubject = ? AND IsActive = 1;
        """,
        (subject,),
    )
    if not identity:
        raise SystemExit("The scoring refresh identity is not active.")

    user_id = str(identity["UserId"])
    score_as_of = date.today()
    references = server._load_scoring_reference_rows()
    benchmark_history = server._load_scoring_history(
        server.SCORING_BENCHMARK,
        user_id,
        score_as_of,
    )
    completed: list[str] = []
    failed: list[dict[str, str]] = []
    for row in references:
        symbol = str(row.get("Symbol") or "").upper()
        if not symbol or bool(row.get("NeedsReview")):
            continue
        try:
            server._score_symbol_for_user(
                user_id,
                symbol,
                references,
                benchmark_history,
                account_id=None,
                target_weight_percent=server.SCORING_DEFAULT_TARGET_WEIGHT_PERCENT,
                score_as_of=score_as_of,
            )
            completed.append(symbol)
        except Exception as exc:  # The summary is safe; secrets are never included.
            failed.append({"Symbol": symbol, "Error": str(exc)})

    print(
        json.dumps(
            {
                "ScoreAsOf": score_as_of.isoformat(),
                "ModelVersion": server.SCORING_MODEL_VERSION,
                "Completed": len(completed),
                "Failed": failed,
            },
            separators=(",", ":"),
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
