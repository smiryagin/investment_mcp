"""Transparent investment scoring primitives.

The functions in this module intentionally avoid machine-learned embeddings and
third-party numerical dependencies.  Every output can be reproduced from the
supplied feature rows, peer rows, model version, and scoring date.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any


SCORING_RULE_VERSION = "2"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        numeric = float(text)
    except ValueError:
        return None
    return numeric if math.isfinite(numeric) else None


def _upper(row: Mapping[str, Any], *names: str) -> str:
    return " ".join(str(row.get(name) or "") for name in names).upper()


def classify_instrument(row: Mapping[str, Any]) -> dict[str, Any]:
    """Classify an instrument using deterministic, explainable text rules."""
    symbol = str(row.get("Symbol") or "").strip().upper()
    type_text = _upper(row, "Type", "AssetType", "AssetSubType")
    descriptive_text = _upper(
        row,
        "Symbol",
        "Name",
        "Description",
        "Type",
        "AssetType",
        "AssetSubType",
    )

    if "TARGET" in descriptive_text and (
        "RETIREMENT" in descriptive_text or "DATE" in descriptive_text
    ):
        candidate_class = "MutualFund"
        archetype = "TargetDateMultiAsset"
        confidence = 0.95
    elif any(value in type_text for value in ("MUTUAL", "MUTUAL_FUND")):
        candidate_class = "MutualFund"
        archetype = "EquityFund"
        confidence = 0.78
    elif "ETF" in type_text:
        candidate_class = "ETF"
        if any(
            value in descriptive_text
            for value in ("INTERNATIONAL", "EX-US", "FOREIGN", "EMERGING")
        ):
            archetype = "InternationalEquity"
            confidence = 0.90
        elif any(
            value in descriptive_text
            for value in ("BOND", "TREASURY", "CREDIT", "FIXED INCOME", "HIGH YIELD")
        ):
            archetype = "FixedIncomeCredit"
            confidence = 0.90
        elif any(
            value in descriptive_text
            for value in ("GOLD", "COMMOD", "REAL ESTATE", "REIT")
        ):
            archetype = "DefensiveRealAssets"
            confidence = 0.90
        elif any(
            value in descriptive_text
            for value in ("SMALL CAP", "SMALL-CAP", "MID CAP", "MID-CAP")
        ):
            archetype = "SmallMidFactorEquity"
            confidence = 0.86
        elif any(
            value in descriptive_text
            for value in ("TECHNOLOGY", "SEMICONDUCTOR", "HEALTH CARE", "ENERGY", "SECTOR")
        ):
            archetype = "SectorEquity"
            confidence = 0.88
        elif "GROWTH" in descriptive_text:
            archetype = "GrowthEquityFund"
            confidence = 0.84
        elif symbol in {"VTV", "SCHV", "IWD", "IVE"} or (
            "VALUE" in descriptive_text
            and any(value in descriptive_text for value in ("LARGE CAP", "LARGE-CAP"))
        ):
            archetype = "LargeValueEquity"
            confidence = 0.90
        elif "VALUE" in descriptive_text:
            archetype = "ValueEquityFund"
            confidence = 0.78
        elif any(
            value in descriptive_text
            for value in ("S&P 500", "TOTAL STOCK", "TOTAL MARKET", "BROAD MARKET", "LARGE CAP")
        ):
            archetype = "BroadUSEquity"
            confidence = 0.88
        else:
            archetype = "EquityFund"
            confidence = 0.65
    elif any(value in type_text for value in ("EQUITY", "STOCK")):
        candidate_class = "Stock"
        archetype = "IndividualStock"
        confidence = 0.75
    elif any(value in type_text for value in ("BOND", "FIXED INCOME")):
        candidate_class = "Bond"
        archetype = "FixedIncomeCredit"
        confidence = 0.82
    else:
        candidate_class = "Other"
        archetype = "Unclassified"
        confidence = 0.25

    return {
        "CandidateClass": candidate_class,
        "Archetype": archetype,
        "ClassificationMethod": "AUTO",
        "ClassificationConfidence": round(confidence, 4),
        "ClassificationRuleVersion": SCORING_RULE_VERSION,
        "NeedsReview": archetype == "Unclassified",
    }


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _period_return(closes: Sequence[float], trading_days: int) -> float | None:
    if len(closes) <= trading_days or closes[-trading_days - 1] == 0:
        return None
    return (closes[-1] / closes[-trading_days - 1] - 1.0) * 100.0


def _daily_returns(closes: Sequence[float]) -> list[float]:
    return [
        closes[index] / closes[index - 1] - 1.0
        for index in range(1, len(closes))
        if closes[index - 1] != 0
    ]


def _maximum_drawdown(closes: Sequence[float]) -> float | None:
    if not closes:
        return None
    peak = closes[0]
    worst = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak:
            worst = min(worst, close / peak - 1.0)
    return worst * 100.0


def _aligned_beta(
    history: Sequence[Mapping[str, Any]],
    benchmark_history: Sequence[Mapping[str, Any]],
) -> float | None:
    candidate_prices = {
        _date_value(row.get("Date")): _number(
            row.get("LastValue", row.get("CloseValue", row.get("close")))
        )
        for row in history
    }
    benchmark_prices = {
        _date_value(row.get("Date")): _number(
            row.get("LastValue", row.get("CloseValue", row.get("close")))
        )
        for row in benchmark_history
    }
    common_dates = sorted(
        key
        for key in candidate_prices.keys() & benchmark_prices.keys()
        if key is not None
        and candidate_prices[key] is not None
        and benchmark_prices[key] is not None
    )
    if len(common_dates) < 30:
        return None
    candidate = [float(candidate_prices[key]) for key in common_dates]
    benchmark = [float(benchmark_prices[key]) for key in common_dates]
    candidate_returns = _daily_returns(candidate)
    benchmark_returns = _daily_returns(benchmark)
    if len(candidate_returns) != len(benchmark_returns) or len(candidate_returns) < 2:
        return None
    variance = statistics.variance(benchmark_returns)
    if variance == 0:
        return None
    covariance = sum(
        (left - statistics.fmean(candidate_returns))
        * (right - statistics.fmean(benchmark_returns))
        for left, right in zip(candidate_returns, benchmark_returns)
    ) / (len(candidate_returns) - 1)
    return covariance / variance


def compute_price_features(
    history: Sequence[Mapping[str, Any]],
    benchmark_history: Sequence[Mapping[str, Any]] | None = None,
    *,
    as_of: date | None = None,
) -> dict[str, float | str | None]:
    """Calculate point-in-time price features from daily OHLCV rows."""
    usable: list[tuple[date, float, float | None]] = []
    for row in history:
        row_date = _date_value(row.get("Date", row.get("datetime")))
        close = _number(
            row.get("LastValue", row.get("CloseValue", row.get("close")))
        )
        volume = _number(row.get("Volume", row.get("volume")))
        if row_date is None or close is None or close <= 0:
            continue
        if as_of is not None and row_date > as_of:
            continue
        usable.append((row_date, close, volume))
    usable.sort(key=lambda item: item[0])
    if not usable:
        return {}

    closes = [item[1] for item in usable]
    returns = _daily_returns(closes)
    annualized_volatility = (
        statistics.stdev(returns) * math.sqrt(252.0) * 100.0
        if len(returns) >= 2
        else None
    )
    annualized_return = None
    if len(closes) > 252 and closes[-253] > 0:
        annualized_return = (closes[-1] / closes[-253] - 1.0) * 100.0
    downside_returns = [value for value in returns if value < 0]
    downside_deviation = (
        math.sqrt(statistics.fmean(value * value for value in downside_returns))
        * math.sqrt(252.0)
        * 100.0
        if downside_returns
        else None
    )
    sharpe_ratio = None
    if returns and annualized_volatility not in (None, 0):
        sharpe_ratio = (
            statistics.fmean(returns) * 252.0 * 100.0 / annualized_volatility
        )

    moving_average_50 = statistics.fmean(closes[-50:]) if len(closes) >= 50 else None
    moving_average_200 = (
        statistics.fmean(closes[-200:]) if len(closes) >= 200 else None
    )
    dollar_volumes = [
        close * volume
        for _, close, volume in usable[-63:]
        if volume is not None and volume >= 0
    ]
    filtered_benchmark = []
    if benchmark_history:
        for row in benchmark_history:
            row_date = _date_value(row.get("Date", row.get("datetime")))
            if as_of is None or row_date is None or row_date <= as_of:
                filtered_benchmark.append(row)

    return {
        "PriceDate": usable[-1][0].isoformat(),
        "LastValue": round(closes[-1], 8),
        "M3": _period_return(closes, 63),
        "M6": _period_return(closes, 126),
        "Y1": _period_return(closes, 252),
        "AnnualizedReturn": annualized_return,
        "AnnualizedVolatility": annualized_volatility,
        "DownsideDeviation": downside_deviation,
        "MaximumDrawdown": _maximum_drawdown(closes),
        "SharpeRatio": sharpe_ratio,
        "MovingAverage50": moving_average_50,
        "MovingAverage200": moving_average_200,
        "DistanceFrom50": (
            (closes[-1] / moving_average_50 - 1.0) * 100.0
            if moving_average_50
            else None
        ),
        "DistanceFrom200": (
            (closes[-1] / moving_average_200 - 1.0) * 100.0
            if moving_average_200
            else None
        ),
        "AverageDollarVolume": (
            statistics.fmean(dollar_volumes) if dollar_volumes else None
        ),
        "MarketBeta": (
            _aligned_beta(history, filtered_benchmark)
            if filtered_benchmark
            else None
        ),
    }


METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "PE": ("PE", "PeRatio"),
    "PEG": ("PEG", "PegRatio"),
    "EPS": ("EPS",),
    "Yield": ("Yield", "DividendYield"),
    "ReturnOnEquity": ("ReturnOnEquity",),
    "AnnualizedReturn": ("AnnualizedReturn",),
    "M3": ("M3",),
    "M6": ("M6",),
    "Y1": ("Y1",),
    "Y3": ("Y3",),
    "DistanceFrom50": ("DistanceFrom50", "%50Chng"),
    "DistanceFrom200": ("DistanceFrom200", "%200Chng"),
    "AnnualizedVolatility": ("AnnualizedVolatility", "Volatility"),
    "MaximumDrawdown": ("MaximumDrawdown", "MaxDrawdown"),
    "DownsideDeviation": ("DownsideDeviation",),
    "SharpeRatio": ("SharpeRatio",),
    "AverageDollarVolume": ("AverageDollarVolume", "Liq"),
}


COMPONENT_SPECS: dict[str, tuple[tuple[str, bool, float], ...]] = {
    "QualityScore": (
        ("EPS", True, 0.25),
        ("ReturnOnEquity", True, 0.25),
        ("AnnualizedReturn", True, 0.25),
        ("SharpeRatio", True, 0.25),
    ),
    "ValuationScore": (
        ("PE", False, 0.50),
        ("PEG", False, 0.25),
        ("Yield", True, 0.25),
    ),
    "GrowthScore": (
        ("M3", True, 0.20),
        ("M6", True, 0.20),
        ("Y1", True, 0.30),
        ("Y3", True, 0.30),
    ),
    "TrendScore": (
        ("M3", True, 0.25),
        ("M6", True, 0.20),
        ("Y1", True, 0.25),
        ("DistanceFrom50", True, 0.15),
        ("DistanceFrom200", True, 0.15),
    ),
    "RiskScore": (
        ("AnnualizedVolatility", False, 0.30),
        ("MaximumDrawdown", True, 0.30),
        ("DownsideDeviation", False, 0.15),
        ("SharpeRatio", True, 0.25),
    ),
    "LiquidityCostScore": (("AverageDollarVolume", True, 1.0),),
}


SIMILARITY_METRICS = (
    "PE",
    "Yield",
    "M3",
    "M6",
    "Y1",
    "AnnualizedVolatility",
    "MaximumDrawdown",
    "SharpeRatio",
    "AverageDollarVolume",
)


def _metric(row: Mapping[str, Any], name: str) -> float | None:
    for alias in METRIC_ALIASES.get(name, (name,)):
        numeric = _number(row.get(alias))
        if numeric is not None:
            if name in {
                "PE",
                "PEG",
                "AnnualizedVolatility",
                "DownsideDeviation",
                "AverageDollarVolume",
            } and numeric <= 0:
                return None
            if name == "MaximumDrawdown" and numeric == 0:
                return None
            if (
                name == "EPS"
                and numeric == 0
                and str(row.get("CandidateClass") or "") != "Stock"
            ):
                return None
            return numeric
    return None


def _percentile(value: float, population: Sequence[float], higher: bool) -> float:
    if not population:
        return 50.0
    lower = sum(1 for item in population if item < value)
    equal = sum(1 for item in population if item == value)
    result = (lower + 0.5 * equal) / len(population) * 100.0
    return result if higher else 100.0 - result


def _component_score(
    candidate: Mapping[str, Any],
    peers: Sequence[Mapping[str, Any]],
    specs: Sequence[tuple[str, bool, float]],
) -> tuple[float, float, list[str]]:
    score = 0.0
    present_weight = 0.0
    missing: list[str] = []
    for metric_name, higher_is_better, weight in specs:
        candidate_value = _metric(candidate, metric_name)
        population = [
            value
            for value in (_metric(peer, metric_name) for peer in peers)
            if value is not None
        ]
        if candidate_value is None or len(population) < 2:
            score += 50.0 * weight
            missing.append(metric_name)
            continue
        score += _percentile(candidate_value, population, higher_is_better) * weight
        present_weight += weight
    return round(score, 2), round(present_weight * 100.0, 2), missing


def _reference_similarity(
    candidate: Mapping[str, Any],
    peers: Sequence[Mapping[str, Any]],
) -> tuple[float | None, list[dict[str, Any]]]:
    candidate_percentiles: dict[str, float] = {}
    peer_percentiles: dict[str, dict[str, float]] = {}
    for metric_name in SIMILARITY_METRICS:
        population = [
            value
            for value in (_metric(peer, metric_name) for peer in peers)
            if value is not None
        ]
        candidate_value = _metric(candidate, metric_name)
        if candidate_value is None or len(population) < 3:
            continue
        candidate_percentiles[metric_name] = _percentile(
            candidate_value,
            population,
            True,
        )
        for peer in peers:
            peer_value = _metric(peer, metric_name)
            if peer_value is None:
                continue
            symbol = str(peer.get("Symbol") or "").upper()
            if not symbol:
                continue
            peer_percentiles.setdefault(symbol, {})[metric_name] = _percentile(
                peer_value,
                population,
                True,
            )

    similarities: list[dict[str, Any]] = []
    for peer in peers:
        symbol = str(peer.get("Symbol") or "").upper()
        values = peer_percentiles.get(symbol, {})
        common = candidate_percentiles.keys() & values.keys()
        if len(common) < 3:
            continue
        distance = math.sqrt(
            statistics.fmean(
                ((candidate_percentiles[name] - values[name]) / 100.0) ** 2
                for name in common
            )
        )
        similarities.append(
            {
                "Symbol": symbol,
                "Similarity": round(max(0.0, 100.0 * (1.0 - distance)), 2),
                "ComparedFeatureCount": len(common),
            }
        )
    similarities.sort(key=lambda item: item["Similarity"], reverse=True)
    nearest = similarities[:5]
    if not nearest:
        return None, []
    return round(statistics.fmean(item["Similarity"] for item in nearest), 2), nearest


def review_tier(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 80:
        return "Strong candidate for detailed review"
    if score >= 65:
        return "Potentially useful; investigate"
    if score >= 50:
        return "No clear advantage"
    return "Weak candidate unless it fills a specific portfolio role"


def score_candidate(
    candidate: Mapping[str, Any],
    reference_rows: Sequence[Mapping[str, Any]],
    *,
    model_version: str = "1.1",
    score_as_of: date | None = None,
    minimum_peer_count: int = 5,
) -> dict[str, Any]:
    """Score one candidate against its relevant global reference peers."""
    candidate_row = dict(candidate)
    classification = {
        **classify_instrument(candidate_row),
        **{
            key: candidate_row[key]
            for key in (
                "CandidateClass",
                "Archetype",
                "ClassificationMethod",
                "ClassificationConfidence",
                "ClassificationRuleVersion",
                "NeedsReview",
            )
            if candidate_row.get(key) is not None
        },
    }
    candidate_row.update(classification)
    symbol = str(candidate_row.get("Symbol") or "").upper()
    eligible = [
        dict(row)
        for row in reference_rows
        if str(row.get("Symbol") or "").upper() != symbol
        and not bool(row.get("NeedsReview"))
    ]
    archetype_peers = [
        row
        for row in eligible
        if row.get("Archetype") == classification["Archetype"]
    ]
    if len(archetype_peers) >= minimum_peer_count:
        peers = archetype_peers
        peer_group_level = "Archetype"
    else:
        peers = [
            row
            for row in eligible
            if row.get("CandidateClass") == classification["CandidateClass"]
        ]
        peer_group_level = "CandidateClass"

    component_results: dict[str, float] = {}
    component_completeness: dict[str, float] = {}
    missing_features: list[str] = []
    for component, specs in COMPONENT_SPECS.items():
        component_score, completeness, missing = _component_score(
            candidate_row,
            peers,
            specs,
        )
        component_results[component] = component_score
        component_completeness[component] = completeness
        missing_features.extend(missing)

    similarity, closest_peers = _reference_similarity(candidate_row, peers)
    similarity_for_formula = similarity if similarity is not None else 50.0
    if similarity is None:
        missing_features.append("ReferenceSimilarity")

    investment_quality = round(
        component_results["QualityScore"] * 0.45
        + component_results["RiskScore"] * 0.30
        + component_results["LiquidityCostScore"] * 0.15
        + component_results["GrowthScore"] * 0.10,
        2,
    )
    technical_opportunity = round(
        component_results["ValuationScore"] * (20.0 / 35.0)
        + component_results["TrendScore"] * (15.0 / 35.0),
        2,
    )
    standalone = round(
        (
            investment_quality * 35.0
            + component_results["ValuationScore"] * 20.0
            + component_results["TrendScore"] * 15.0
            + similarity_for_formula * 10.0
        )
        / 80.0,
        2,
    )
    concerns: list[str] = []
    if component_results["RiskScore"] < 25:
        standalone = min(standalone, 69.0)
        concerns.append("Risk score is below the minimum for the highest review tier.")
    if component_results["LiquidityCostScore"] < 20:
        standalone = min(standalone, 64.0)
        concerns.append("Liquidity score limits the candidate's review tier.")
    if len(peers) < minimum_peer_count:
        concerns.append(
            f"Only {len(peers)} eligible {peer_group_level.lower()} peers were available."
        )

    completeness_values = list(component_completeness.values())
    data_completeness = round(statistics.fmean(completeness_values), 2)
    strengths = [
        name.replace("Score", "")
        for name, value in component_results.items()
        if value >= 75
    ]
    return {
        "Symbol": symbol,
        "CandidateClass": classification["CandidateClass"],
        "Archetype": classification["Archetype"],
        "ClassificationMethod": classification["ClassificationMethod"],
        "ClassificationConfidence": classification["ClassificationConfidence"],
        "ClassificationRuleVersion": classification[
            "ClassificationRuleVersion"
        ],
        "PeerGroupLevel": peer_group_level,
        "PeerCount": len(peers),
        **component_results,
        "InvestmentQualityScore": investment_quality,
        "TechnicalOpportunityScore": technical_opportunity,
        "ReferenceSimilarityScore": similarity,
        "StandaloneCandidateScore": standalone,
        "PortfolioFitScore": None,
        "CompositeCandidateScore": None,
        "ReviewTier": review_tier(standalone),
        "ScoreAsOf": (score_as_of or date.today()).isoformat(),
        "ModelVersion": model_version,
        "DataCompletenessScore": data_completeness,
        "ComponentCompleteness": component_completeness,
        "ClosestReferenceSymbols": closest_peers,
        "Strengths": strengths,
        "Concerns": concerns,
        "MissingFeatures": sorted(set(missing_features)),
    }


def _weighted_positions(
    positions: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], float]]:
    weighted: list[tuple[Mapping[str, Any], float]] = []
    for position in positions:
        weight_value = _number(position.get("MarketValue"))
        if weight_value is None:
            weight_value = abs(_number(position.get("Quantity")) or 0.0)
        if weight_value > 0:
            weighted.append((position, weight_value))
    total = sum(value for _, value in weighted)
    if total <= 0 and positions:
        return [(position, 1.0 / len(positions)) for position in positions]
    return [(position, value / total) for position, value in weighted]


def _normalized_holding_map(info: Mapping[str, Any] | None) -> dict[str, float]:
    if not info:
        return {}
    raw_holdings = info.get("Holdings")
    if not isinstance(raw_holdings, Mapping):
        return {}
    normalized: dict[str, float] = {}
    for raw_key, raw_weight in raw_holdings.items():
        key = str(raw_key or "").strip().upper()
        weight = _number(raw_weight)
        if not key or weight is None or weight <= 0:
            continue
        if weight > 1:
            weight /= 100.0
        normalized[key] = min(1.0, weight)
    return normalized


def _holding_age_days(
    info: Mapping[str, Any] | None,
    evaluation_date: date,
) -> int | None:
    if not info:
        return None
    as_of = _date_value(info.get("AsOfDate"))
    if as_of is None:
        return None
    return max(0, (evaluation_date - as_of).days)


def _holdings_overlap(
    candidate: Mapping[str, Any],
    weighted_positions: Sequence[tuple[Mapping[str, Any], float]],
    classifications: Mapping[str, Mapping[str, Any]],
    fund_holdings: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    candidate_symbol = str(candidate.get("Symbol") or "").upper()
    candidate_class = str(candidate.get("CandidateClass") or "")
    fund_classes = {"ETF", "MutualFund"}
    evaluation_date = _date_value(candidate.get("ScoreAsOf")) or date.today()
    candidate_info = fund_holdings.get(candidate_symbol)
    candidate_holdings = _normalized_holding_map(candidate_info)
    candidate_age_days = _holding_age_days(candidate_info, evaluation_date)
    candidate_is_stale = (
        candidate_age_days is not None and candidate_age_days > 45
    )
    candidate_coverage = _number(
        candidate_info.get("CoveragePercent") if candidate_info else None
    )
    if candidate_holdings and candidate_coverage is None:
        candidate_coverage = sum(candidate_holdings.values()) * 100.0
    candidate_coverage = max(0.0, min(100.0, candidate_coverage or 0.0))

    if not weighted_positions:
        return {
            "HoldingsOverlapStatus": "EmptyPortfolio",
            "HoldingsOverlapPercent": 0.0,
            "HoldingsDataCoveragePercent": 100.0,
            "CandidateHoldingsAsOf": (
                candidate_info.get("AsOfDate") if candidate_info else None
            ),
            "CandidateHoldingsSource": (
                candidate_info.get("SourceName") if candidate_info else None
            ),
            "CandidateHoldingsAgeDays": candidate_age_days,
        }

    if candidate_class in fund_classes and not candidate_holdings:
        return {
            "HoldingsOverlapStatus": "Unavailable",
            "HoldingsOverlapPercent": None,
            "HoldingsDataCoveragePercent": 0.0,
            "CandidateHoldingsAsOf": None,
            "CandidateHoldingsSource": None,
            "CandidateHoldingsAgeDays": None,
        }

    if candidate_class not in fund_classes | {"Stock"}:
        return {
            "HoldingsOverlapStatus": "NotApplicable",
            "HoldingsOverlapPercent": None,
            "HoldingsDataCoveragePercent": 100.0,
            "CandidateHoldingsAsOf": None,
            "CandidateHoldingsSource": None,
            "CandidateHoldingsAgeDays": None,
        }

    overlap = 0.0
    measured_weight = 0.0
    for position, position_weight in weighted_positions:
        position_symbol = str(position.get("Symbol") or "").upper()
        if not position_symbol:
            continue
        if position_symbol == candidate_symbol:
            overlap += position_weight
            measured_weight += position_weight
            continue

        position_classification = classifications.get(position_symbol) or {}
        position_class = str(position_classification.get("CandidateClass") or "")
        position_info = fund_holdings.get(position_symbol)
        position_holdings = _normalized_holding_map(position_info)
        position_age_days = _holding_age_days(position_info, evaluation_date)
        if position_age_days is not None and position_age_days > 45:
            position_holdings = {}

        if candidate_class in fund_classes:
            if position_holdings:
                pair_overlap = sum(
                    min(weight, position_holdings.get(key, 0.0))
                    for key, weight in candidate_holdings.items()
                )
            elif position_class == "Stock":
                pair_overlap = candidate_holdings.get(
                    f"TICKER:{position_symbol}",
                    candidate_holdings.get(position_symbol, 0.0),
                )
            else:
                continue
        else:
            if position_holdings:
                pair_overlap = position_holdings.get(
                    f"TICKER:{candidate_symbol}",
                    position_holdings.get(candidate_symbol, 0.0),
                )
            elif position_class == "Stock":
                pair_overlap = 0.0
            else:
                continue

        overlap += position_weight * min(1.0, max(0.0, pair_overlap))
        measured_weight += position_weight

    data_coverage = measured_weight
    if candidate_class in fund_classes:
        data_coverage *= candidate_coverage / 100.0
    if candidate_is_stale:
        status = "Stale"
    else:
        status = "Available" if data_coverage >= 0.999 else "Partial"
    return {
        "HoldingsOverlapStatus": status,
        "HoldingsOverlapPercent": round(overlap * 100.0, 2),
        "HoldingsDataCoveragePercent": round(data_coverage * 100.0, 2),
        "CandidateHoldingsAsOf": (
            candidate_info.get("AsOfDate") if candidate_info else None
        ),
        "CandidateHoldingsSource": (
            candidate_info.get("SourceName") if candidate_info else None
        ),
        "CandidateHoldingsAgeDays": candidate_age_days,
    }


def _dated_returns(history: Sequence[Mapping[str, Any]]) -> dict[date, float]:
    closes: dict[date, float] = {}
    for row in history:
        day = _date_value(row.get("Date") or row.get("datetime"))
        close = _number(
            row.get("LastValue")
            if row.get("LastValue") is not None
            else row.get("close")
        )
        if day is not None and close is not None and close > 0:
            closes[day] = close
    returns: dict[date, float] = {}
    previous: float | None = None
    for day, close in sorted(closes.items()):
        if previous is not None and previous > 0:
            returns[day] = close / previous - 1.0
        previous = close
    return returns


def _portfolio_return_correlation(
    candidate_history: Sequence[Mapping[str, Any]],
    weighted_positions: Sequence[tuple[Mapping[str, Any], float]],
    position_histories: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    minimum_observations: int = 60,
) -> dict[str, Any]:
    if not weighted_positions:
        return {
            "ReturnCorrelation": None,
            "CorrelationObservationCount": 0,
            "CorrelationDataCoveragePercent": 100.0,
        }
    candidate_returns = _dated_returns(candidate_history)
    position_returns = {
        symbol: _dated_returns(history)
        for symbol, history in position_histories.items()
    }
    candidate_values: list[float] = []
    portfolio_values: list[float] = []
    coverage_values: list[float] = []
    for day, candidate_return in candidate_returns.items():
        weighted_return = 0.0
        available_weight = 0.0
        for position, position_weight in weighted_positions:
            symbol = str(position.get("Symbol") or "").upper()
            value = position_returns.get(symbol, {}).get(day)
            if value is None:
                continue
            weighted_return += position_weight * value
            available_weight += position_weight
        if available_weight < 0.50:
            continue
        candidate_values.append(candidate_return)
        portfolio_values.append(weighted_return / available_weight)
        coverage_values.append(available_weight)

    observations = len(candidate_values)
    coverage = (
        statistics.fmean(coverage_values) * 100.0 if coverage_values else 0.0
    )
    if observations < minimum_observations:
        return {
            "ReturnCorrelation": None,
            "CorrelationObservationCount": observations,
            "CorrelationDataCoveragePercent": round(coverage, 2),
        }
    candidate_mean = statistics.fmean(candidate_values)
    portfolio_mean = statistics.fmean(portfolio_values)
    covariance = sum(
        (candidate - candidate_mean) * (portfolio - portfolio_mean)
        for candidate, portfolio in zip(candidate_values, portfolio_values)
    )
    candidate_variance = sum(
        (candidate - candidate_mean) ** 2 for candidate in candidate_values
    )
    portfolio_variance = sum(
        (portfolio - portfolio_mean) ** 2 for portfolio in portfolio_values
    )
    denominator = math.sqrt(candidate_variance * portfolio_variance)
    correlation = covariance / denominator if denominator > 0 else None
    return {
        "ReturnCorrelation": (
            round(max(-1.0, min(1.0, correlation)), 4)
            if correlation is not None
            else None
        ),
        "CorrelationObservationCount": observations,
        "CorrelationDataCoveragePercent": round(coverage, 2),
    }


def compute_portfolio_fit(
    candidate: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
    reference_rows: Sequence[Mapping[str, Any]],
    *,
    target_weight_percent: float = 2.0,
    fund_holdings: Mapping[str, Mapping[str, Any]] | None = None,
    candidate_history: Sequence[Mapping[str, Any]] | None = None,
    position_histories: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Estimate portfolio fit with classification, holdings, and correlation data."""
    target_weight = min(25.0, max(0.01, float(target_weight_percent))) / 100.0
    candidate_symbol = str(candidate.get("Symbol") or "").upper()
    candidate_class = candidate.get("CandidateClass")
    candidate_archetype = candidate.get("Archetype")
    classifications = {
        str(row.get("Symbol") or "").upper(): row for row in reference_rows
    }

    weighted_positions = _weighted_positions(positions)

    existing_symbol_weight = 0.0
    archetype_weight = 0.0
    class_weight = 0.0
    classified_weight = 0.0
    for position, position_weight in weighted_positions:
        symbol = str(position.get("Symbol") or "").upper()
        if symbol == candidate_symbol:
            existing_symbol_weight += position_weight
        classification = classifications.get(symbol)
        if not classification:
            continue
        classified_weight += position_weight
        if classification.get("Archetype") == candidate_archetype:
            archetype_weight += position_weight
        if classification.get("CandidateClass") == candidate_class:
            class_weight += position_weight

    after_archetype = archetype_weight * (1.0 - target_weight) + target_weight
    after_class = class_weight * (1.0 - target_weight) + target_weight
    score = 100.0
    if existing_symbol_weight > 0:
        score -= 35.0
    score -= min(40.0, max(0.0, after_archetype - 0.25) * 100.0)
    score -= min(20.0, max(0.0, after_class - 0.70) * 100.0)

    holdings_result = _holdings_overlap(
        candidate,
        weighted_positions,
        classifications,
        fund_holdings or {},
    )
    holdings_overlap = _number(holdings_result.get("HoldingsOverlapPercent"))
    holdings_penalty = (
        min(50.0, holdings_overlap * 0.75)
        if holdings_overlap is not None
        else 0.0
    )
    score -= holdings_penalty

    correlation_result = _portfolio_return_correlation(
        candidate_history or [],
        weighted_positions,
        position_histories or {},
    )
    correlation = _number(correlation_result.get("ReturnCorrelation"))
    correlation_penalty = (
        min(20.0, max(0.0, correlation - 0.40) * 30.0)
        if correlation is not None
        else 0.0
    )
    score -= correlation_penalty

    limitations: list[str] = []
    fit_data_incomplete = False
    holdings_status = holdings_result["HoldingsOverlapStatus"]
    holdings_coverage = _number(
        holdings_result.get("HoldingsDataCoveragePercent")
    ) or 0.0
    if weighted_positions and holdings_status == "Unavailable":
        fit_data_incomplete = True
        score = min(score, 60.0)
        limitations.append(
            "Fund holdings are unavailable; portfolio fit is capped at 60."
        )
    elif weighted_positions and holdings_status == "Stale":
        fit_data_incomplete = True
        score = min(score, 65.0)
        limitations.append(
            "Fund holdings are more than 45 days old; portfolio fit is capped at 65."
        )
    elif weighted_positions and holdings_status == "Partial" and holdings_coverage < 70:
        fit_data_incomplete = True
        score = min(score, 70.0)
        limitations.append(
            "Fund holdings coverage is below 70%; portfolio fit is capped at 70."
        )
    if holdings_overlap is not None and holdings_overlap >= 20:
        limitations.append(
            f"Estimated constituent overlap is {holdings_overlap:.2f}% and reduces portfolio fit."
        )
    if weighted_positions and correlation is None:
        fit_data_incomplete = True
        score = min(score, 75.0)
        limitations.append(
            "Return correlation has fewer than 60 overlapping observations; "
            "portfolio fit is capped at 75."
        )
    elif correlation is not None and correlation >= 0.75:
        limitations.append(
            f"Return correlation is {correlation:.2f} and reduces portfolio fit."
        )
    if fit_data_incomplete:
        limitations.append(
            "Composite score is prevented from entering a positive review tier until portfolio-fit data is sufficient."
        )
    score = round(max(0.0, min(100.0, score)), 2)
    classification_coverage = (
        round(classified_weight * 100.0, 2) if positions else 100.0
    )
    completeness_inputs: list[float] = []
    if holdings_status != "NotApplicable":
        completeness_inputs.append(holdings_coverage)
    completeness_inputs.append(
        _number(correlation_result.get("CorrelationDataCoveragePercent")) or 0.0
    )
    fit_completeness = (
        round(statistics.fmean(completeness_inputs), 2)
        if completeness_inputs
        else 100.0
    )
    return {
        "PortfolioFitScore": score,
        "TargetWeightPercent": round(target_weight * 100.0, 4),
        "ExistingSymbolWeightPercent": round(existing_symbol_weight * 100.0, 2),
        "ExistingArchetypeWeightPercent": round(archetype_weight * 100.0, 2),
        "ProjectedArchetypeWeightPercent": round(after_archetype * 100.0, 2),
        "ExistingClassWeightPercent": round(class_weight * 100.0, 2),
        "ProjectedClassWeightPercent": round(after_class * 100.0, 2),
        "ClassificationCoveragePercent": classification_coverage,
        **holdings_result,
        "HoldingsOverlapPenalty": round(holdings_penalty, 2),
        **correlation_result,
        "CorrelationPenalty": round(correlation_penalty, 2),
        "FitDataCompletenessScore": fit_completeness,
        "PortfolioFitDataStatus": (
            "Incomplete" if fit_data_incomplete else "Sufficient"
        ),
        "Limitations": limitations,
    }
