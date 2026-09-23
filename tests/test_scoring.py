import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import server
from scoring import (
    classify_instrument,
    compute_portfolio_fit,
    compute_price_features,
    score_candidate,
)


ROOT = Path(__file__).resolve().parents[1]


def _peer(symbol: str, *, archetype: str = "IndividualStock", offset: int = 0):
    return {
        "Symbol": symbol,
        "CandidateClass": "Stock",
        "Archetype": archetype,
        "NeedsReview": False,
        "EPS": 2 + offset,
        "ReturnOnEquity": 10 + offset,
        "AnnualizedReturn": 5 + offset,
        "SharpeRatio": 0.5 + offset / 10,
        "PE": 30 - offset,
        "PEG": 3 - offset / 10,
        "Yield": offset / 10,
        "M3": 1 + offset,
        "M6": 2 + offset,
        "Y1": 3 + offset,
        "Y3": 4 + offset,
        "DistanceFrom50": offset,
        "DistanceFrom200": offset,
        "AnnualizedVolatility": 30 - offset,
        "MaximumDrawdown": -30 + offset,
        "DownsideDeviation": 20 - offset,
        "AverageDollarVolume": 1_000_000 + offset * 100_000,
    }


class ClassificationTests(unittest.TestCase):
    def test_classifies_common_reference_instruments_deterministically(self) -> None:
        etf = classify_instrument(
            {
                "Symbol": "VXUS",
                "Name": "Vanguard Total International Stock ETF",
                "AssetType": "ETF",
            }
        )
        stock = classify_instrument(
            {"Symbol": "MSFT", "Name": "Microsoft", "AssetType": "EQUITY"}
        )

        self.assertEqual(etf["CandidateClass"], "ETF")
        self.assertEqual(etf["Archetype"], "InternationalEquity")
        self.assertFalse(etf["NeedsReview"])
        self.assertEqual(stock["CandidateClass"], "Stock")
        self.assertEqual(stock["Archetype"], "IndividualStock")

    def test_unknown_instrument_requires_review(self) -> None:
        result = classify_instrument({"Symbol": "UNKNOWN"})
        self.assertEqual(result["Archetype"], "Unclassified")
        self.assertTrue(result["NeedsReview"])


class PriceFeatureTests(unittest.TestCase):
    def test_as_of_filter_prevents_future_price_lookahead(self) -> None:
        start = date(2025, 1, 1)
        history = [
            {
                "Date": start + timedelta(days=index),
                "LastValue": 100 + index,
                "Volume": 1000,
            }
            for index in range(300)
        ]
        as_of = start + timedelta(days=260)
        result = compute_price_features(history, as_of=as_of)

        self.assertEqual(result["PriceDate"], as_of.isoformat())
        self.assertEqual(result["LastValue"], 360.0)
        self.assertIsNotNone(result["Y1"])


class TransparentScoreTests(unittest.TestCase):
    def test_candidate_is_scored_only_against_relevant_peers(self) -> None:
        peers = [_peer(f"S{index}", offset=index) for index in range(6)]
        peers.append(_peer("ETF1", archetype="BroadUSEquity", offset=3))
        candidate = _peer("NEW", offset=8)

        result = score_candidate(
            candidate,
            peers,
            score_as_of=date(2026, 9, 23),
            minimum_peer_count=5,
        )

        self.assertEqual(result["PeerGroupLevel"], "Archetype")
        self.assertEqual(result["PeerCount"], 6)
        self.assertGreater(result["InvestmentQualityScore"], 50)
        self.assertGreater(result["StandaloneCandidateScore"], 50)
        self.assertEqual(result["ScoreAsOf"], "2026-09-23")
        self.assertNotIn(
            "NEW",
            [item["Symbol"] for item in result["ClosestReferenceSymbols"]],
        )

    def test_missing_values_are_neutral_and_reported_not_zeroed(self) -> None:
        peers = [_peer(f"S{index}", offset=index) for index in range(6)]
        result = score_candidate(
            {
                "Symbol": "EMPTY",
                "CandidateClass": "Stock",
                "Archetype": "IndividualStock",
            },
            peers,
        )

        self.assertEqual(result["QualityScore"], 50)
        self.assertGreater(len(result["MissingFeatures"]), 0)
        self.assertLess(result["DataCompletenessScore"], 100)

    def test_database_zero_placeholders_are_reported_as_missing(self) -> None:
        peers = [_peer(f"S{index}", offset=index) for index in range(6)]
        result = score_candidate(
            {
                "Symbol": "FUNDX",
                "CandidateClass": "MutualFund",
                "Archetype": "EquityFund",
                "PE": 0,
                "EPS": 0,
                "AnnualizedVolatility": 0,
                "AverageDollarVolume": 0,
            },
            peers,
            minimum_peer_count=2,
        )

        self.assertIn("PE", result["MissingFeatures"])
        self.assertIn("EPS", result["MissingFeatures"])
        self.assertIn("AnnualizedVolatility", result["MissingFeatures"])
        self.assertIn("AverageDollarVolume", result["MissingFeatures"])

    def test_portfolio_fit_penalizes_an_existing_duplicate(self) -> None:
        references = [_peer("MSFT"), _peer("GOOGL")]
        duplicate = compute_portfolio_fit(
            {
                "Symbol": "MSFT",
                "CandidateClass": "Stock",
                "Archetype": "IndividualStock",
            },
            [{"Symbol": "MSFT", "MarketValue": 100}],
            references,
        )
        new_asset = compute_portfolio_fit(
            {
                "Symbol": "VXUS",
                "CandidateClass": "ETF",
                "Archetype": "InternationalEquity",
            },
            [{"Symbol": "MSFT", "MarketValue": 100}],
            references,
        )

        self.assertLess(
            duplicate["PortfolioFitScore"],
            new_asset["PortfolioFitScore"],
        )


class ScoringMigrationTests(unittest.TestCase):
    def test_classification_migration_uses_global_series_and_trigger(self) -> None:
        sql = (ROOT / "sql" / "013_add_reference_instrument_classification.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("dbo.Series", sql)
        self.assertIn("CREATE OR ALTER TRIGGER dbo.TR_Series_AutoClassify", sql)
        self.assertIn("FROM inserted AS source", sql)
        self.assertIn("ClassificationMethod = 'AUTO'", sql)
        self.assertIn("WITH EXECUTE AS OWNER", sql)
        self.assertNotIn("ReferenceUniverseMembers", sql)
        self.assertNotIn("schwabapi.com", sql.lower())

    def test_runtime_grants_use_procedures_for_snapshot_writes(self) -> None:
        sql = (ROOT / "sql" / "015_grant_scoring_runtime.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("McpScoringReferenceInstruments", sql)
        self.assertIn("UpsertInstrumentFeatureSnapshot", sql)
        self.assertIn("UpsertInstrumentScoreSnapshot", sql)
        self.assertIn("UpsertPortfolioCandidateScoreSnapshot", sql)
        self.assertIn("DENY INSERT, UPDATE, DELETE", sql)


class ScoringToolTests(unittest.TestCase):
    def test_read_only_scoring_tools_are_registered(self) -> None:
        registered = server.mcp._tool_manager._tools
        expected = {
            "get_reference_universe",
            "get_scoring_model",
            "score_instrument",
            "rank_candidates",
            "get_score_history",
        }
        self.assertTrue(expected <= set(registered))
        for name in expected:
            self.assertTrue(registered[name].annotations.readOnlyHint, name)
            properties = registered[name].parameters.get("properties", {})
            self.assertNotIn("user_id", properties, name)

    def test_owned_position_query_hides_inaccessible_account(self) -> None:
        with patch.object(server, "_fetch_all", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Account not found"):
                server._owned_scoring_positions(
                    "00000000-0000-0000-0000-000000000001",
                    "00000000-0000-0000-0000-000000000002",
                    [],
                )

    def test_external_candidate_uses_one_quote_and_one_history_request(self) -> None:
        quote_payload = {
            "IWM": {
                "symbol": "IWM",
                "assetMainType": "EQUITY",
                "assetSubType": "ETF",
                "quote": {"lastPrice": 200, "totalVolume": 1000},
                "reference": {"description": "Russell 2000 ETF"},
                "fundamental": {"peRatio": 18},
            }
        }
        start = date(2025, 1, 1)
        history_payload = {
            "symbol": "IWM",
            "candles": [
                {
                    "datetime": int(
                        datetime.combine(
                            start + timedelta(days=index),
                            time.min,
                            tzinfo=timezone.utc,
                        )
                        .timestamp()
                        * 1000
                    ),
                    "close": 100 + index,
                    "volume": 1000,
                }
                for index in range(260)
            ],
        }
        benchmark_history = [
            {"Date": start + timedelta(days=index), "LastValue": 200 + index}
            for index in range(260)
        ]

        with patch.object(server, "_fetch_all", return_value=[]), patch.object(
            server,
            "_schwab_cached_market_data_get",
            side_effect=[quote_payload, history_payload],
        ) as market_get:
            candidate, features = server._prepare_scoring_candidate(
                "IWM",
                "user-id",
                [],
                start + timedelta(days=259),
                benchmark_history,
            )

        self.assertEqual(candidate["CandidateClass"], "ETF")
        self.assertEqual(features["PriceDate"], (start + timedelta(days=259)).isoformat())
        self.assertEqual(market_get.call_args_list[0].args[0], "/quotes")
        self.assertEqual(market_get.call_args_list[1].args[0], "/pricehistory")
        self.assertEqual(market_get.call_args_list[0].kwargs["units"], 1)
        self.assertEqual(market_get.call_args_list[1].kwargs["units"], 5)


if __name__ == "__main__":
    unittest.main()
