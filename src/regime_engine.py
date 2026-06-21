"""Interpretable rule engine that scores every curve regime.

Each regime gets a transparent score in [0, 100] built from:
  * confirming-rule weight that triggered,
  * minus a penalty for triggered contradictions,
  * normalised by the confirming weight that actually had data,
  * dampened by how much of the rule set had data (coverage).

The output is a JSON-serialisable dict that the HTML report renders directly.
Missing metrics never raise: the rule is simply marked "missing".
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import yaml

from .utils import LOG, clamp, safe_float

# Map regime id -> column name in the history CSV (spec section 10).
# front_end_anchored is intentionally not persisted (not in the spec column list).
HISTORY_SCORE_COLS = {
    "bear_flattening": "bear_flattening_score",
    "bear_steepening": "bear_steepening_score",
    "bull_steepening": "bull_steepening_score",
    "bull_flattening": "bull_flattening_score",
    "parallel_bear": "parallel_bear_shift_score",
    "parallel_bull": "parallel_bull_shift_score",
    "rates_shock": "rates_shock_score",
    "recession_scare": "recession_pricing_score",
    "inflation_repricing": "inflation_repricing_score",
    "term_premium_shock": "term_premium_shock_score",
    "deep_inversion": "deep_inversion_score",
    "disinversion": "disinversion_score",
    "neutral_mixed": "neutral_mixed_score",
}

_OP_SYMBOL = {
    ">": "&gt;", ">=": "&ge;", "<": "&lt;", "<=": "&le;",
    "abs>": "|x| &gt;", "abs>=": "|x| &ge;", "abs<": "|x| &lt;", "abs<=": "|x| &le;",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_regimes_config(path: str = "config/regimes.yml") -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------- #
# Rule evaluation
# --------------------------------------------------------------------------- #
def _eval_op(op: str, observed: float, threshold) -> bool:
    if op == ">":
        return observed > threshold
    if op == ">=":
        return observed >= threshold
    if op == "<":
        return observed < threshold
    if op == "<=":
        return observed <= threshold
    if op == "abs>":
        return abs(observed) > threshold
    if op == "abs>=":
        return abs(observed) >= threshold
    if op == "abs<":
        return abs(observed) < threshold
    if op == "abs<=":
        return abs(observed) <= threshold
    if op == "between":
        lo, hi = threshold
        return lo <= observed <= hi
    LOG.warning("Unknown operator '%s' - rule treated as not triggered.", op)
    return False


def _condition_str(op: str, threshold) -> str:
    if op == "between":
        return f"in [{threshold[0]}, {threshold[1]}]"
    return f"{_OP_SYMBOL.get(op, op)} {threshold}"


def evaluate_rule(rule: dict, metrics: Dict[str, float]) -> dict:
    """Return an annotated copy of a rule with its evaluation status."""
    metric = rule["metric"]
    observed = safe_float(metrics.get(metric))
    out = {
        "name": rule["name"],
        "metric": metric,
        "condition": _condition_str(rule["op"], rule["threshold"]),
        "observed": observed,
        "threshold": rule["threshold"],
        "weight": rule.get("weight", 1),
        "explanation": rule.get("explanation", ""),
    }
    if observed is None:
        out["status"] = "missing"
    elif _eval_op(rule["op"], observed, rule["threshold"]):
        out["status"] = "triggered"
    else:
        out["status"] = "not_triggered"
    return out


# --------------------------------------------------------------------------- #
# Per-regime scoring
# --------------------------------------------------------------------------- #
def score_regime(regime: dict, metrics: Dict[str, float], meta: dict) -> dict:
    confirming = [evaluate_rule(r, metrics) for r in regime.get("confirming", [])]
    contradictions = [evaluate_rule(r, metrics) for r in regime.get("contradictions", [])]

    triggered = [r for r in confirming if r["status"] == "triggered"]
    not_triggered = [r for r in confirming if r["status"] == "not_triggered"]
    missing = [r for r in confirming if r["status"] == "missing"]
    fired_contra = [r for r in contradictions if r["status"] == "triggered"]

    total_weight = sum(r["weight"] for r in confirming)
    available_weight = sum(r["weight"] for r in (triggered + not_triggered))
    obtained = sum(r["weight"] for r in triggered)
    contra_penalty = sum(r["weight"] for r in fired_contra) * meta.get("contradiction_weight", 0.7)

    if available_weight <= 0:
        score = 0.0
    else:
        raw = (obtained - contra_penalty) / available_weight        # may be < 0
        raw = clamp(raw, 0.0, 1.0)
        coverage = available_weight / total_weight if total_weight else 0.0
        floor = meta.get("coverage_floor", 0.5)
        coverage_mult = floor + (1.0 - floor) * coverage
        score = 100.0 * raw * coverage_mult

    return {
        "id": regime["id"],
        "name": regime["name"],
        "category": regime.get("category", "neutral"),
        "score": round(clamp(score, 0.0, 100.0), 1),
        "definition": " ".join(regime.get("definition", "").split()),
        "interpretation": " ".join(regime.get("interpretation", "").split()),
        "triggered_rules": triggered,
        "not_triggered_rules": not_triggered,
        "missing_rules": missing,
        "contradictions": fired_contra,
        "n_confirming": len(confirming),
        "n_data": len(triggered) + len(not_triggered),
    }


# --------------------------------------------------------------------------- #
# Primary-regime selection
# --------------------------------------------------------------------------- #
def _select_primary(real_sorted: List[dict], meta: dict) -> Tuple[dict, str, Optional[str]]:
    """Decide the headline regime. Returns (primary_obj, status, bias_name)."""
    best = real_sorted[0]
    second = real_sorted[1] if len(real_sorted) > 1 else None
    gap = best["score"] - (second["score"] if second else 0.0)

    pmin = meta.get("primary_min_confidence", 60)
    fmin = meta.get("flag_min_confidence", 55)
    gthr = meta.get("gap_threshold", 5)

    if best["score"] >= pmin and gap >= gthr:
        return best, "clear", None
    if best["score"] >= pmin:
        return best, "contested", (second["name"] if second else None)
    if best["score"] >= fmin:
        return best, "tentative", (second["name"] if second else None)
    # Neutral / Mixed wins; keep the leaning regime as bias when it is close.
    bias = best["name"] if (50 <= best["score"] < fmin and gap < gthr) else None
    return None, "mixed", bias


# --------------------------------------------------------------------------- #
# Narrative helpers
# --------------------------------------------------------------------------- #
def _fmt_bps(v: Optional[float], decimals: int = 0) -> str:
    v = safe_float(v)
    return "n/a" if v is None else f"{v:+.{decimals}f} bps"


def _inversion_phrase(metrics: Dict[str, float]) -> Optional[str]:
    lvl = safe_float(metrics.get("spread.2s10s.level"))
    if lvl is None:
        return None
    if lvl >= 0:
        c5 = safe_float(metrics.get("spread.2s10s.chg_5d"))
        if c5 is not None and c5 > 3:
            return "The 2s10s curve is positively sloped and has been steepening."
        return "The 2s10s curve is positively sloped."
    # inverted
    trend = ""
    c20 = safe_float(metrics.get("spread.2s10s.chg_20d"))
    if c20 is not None:
        if c20 > 5:
            trend = " and the inversion has been easing over the past month"
        elif c20 < -5:
            trend = " and the inversion has been deepening over the past month"
    depth = "deeply inverted" if lvl < -50 else "inverted"
    return f"The 2s10s curve is {depth} at {lvl:+.0f} bps{trend}."


def _exec_summary(primary: Optional[dict], bias: Optional[str], metrics: Dict[str, float]) -> str:
    front = safe_float(metrics.get("seg.front.chg_1d"))
    long = safe_float(metrics.get("seg.long.chg_1d"))
    s2s10s = safe_float(metrics.get("spread.2s10s.chg_1d"))

    if primary is not None:
        head = primary["interpretation"]
    elif bias:
        head = (f"No single curve regime dominates today, though signals lean slightly "
                f"toward {bias}.")
    else:
        head = "No single curve regime dominates today; the moves are small or mixed."

    facts = []
    if front is not None and long is not None:
        facts.append(f"the front-end moved {front:+.1f} bps versus {long:+.1f} bps at the long-end")
    if s2s10s is not None:
        verb = "steepened" if s2s10s > 0 else "flattened" if s2s10s < 0 else "was unchanged"
        facts.append(f"the 2s10s spread {verb} {abs(s2s10s):.0f} bps on the day")
    tail = ""
    if facts:
        tail = " On the day, " + " and ".join(facts) + "."
    inv = _inversion_phrase(metrics)
    inv = f" {inv}" if inv else ""
    return f"{head}{tail}{inv}"


def _macro_bullets(metrics: Dict[str, float]) -> List[str]:
    bullets: List[str] = []
    g = lambda k: safe_float(metrics.get(k))  # noqa: E731

    two = g("2Y.chg_1d")
    five = g("5Y.chg_1d")
    ten = g("10Y.chg_1d")
    thirty = g("30Y.chg_1d")
    front = g("seg.front.chg_1d")
    long = g("seg.long.chg_1d")
    shift = g("curve.shift_1d")
    s2s10s_chg = g("spread.2s10s.chg_1d")
    s2s10s_lvl = g("spread.2s10s.level")
    be10 = g("be.10y.chg_1d")

    # Fed-path read from the front-end.
    if two is not None:
        if two > 5:
            bullets.append("The policy-sensitive 2Y rose meaningfully, consistent with the market "
                           "pricing a more restrictive Fed path (higher-for-longer).")
        elif two < -5:
            bullets.append("The 2Y fell sharply, consistent with the market pricing future rate "
                           "cuts or a softer Fed path.")

    # Recession-scare read.
    if front is not None and long is not None and front < -4 and front < long - 2:
        bullets.append("Front-end and belly yields led lower while the long-end lagged, the kind of "
                       "bull-steepening that often accompanies growth or recession concerns.")

    # Inflation read.
    if be10 is not None and be10 > 0 and (ten is not None and ten > 0):
        bullets.append("Nominal long yields rose alongside higher breakeven inflation, pointing to an "
                       "inflation-repricing element rather than a pure real-rate move.")
    elif be10 is not None and be10 < 0:
        bullets.append("Breakeven inflation fell, so any rise in nominal yields is more a real-rate "
                       "story than an inflation one.")

    # Term-premium / long-end read.
    if thirty is not None and two is not None and thirty > 0 and thirty - two > 5:
        bullets.append("The 30Y sold off well beyond the 2Y, a signature of term-premium or duration "
                       "pressure at the long end.")

    # Slope read.
    if s2s10s_chg is not None:
        if s2s10s_chg > 3:
            bullets.append("The curve steepened on the day (2s10s wider).")
        elif s2s10s_chg < -3:
            bullets.append("The curve flattened on the day (2s10s narrower).")
        else:
            bullets.append("The curve slope was broadly stable on the day.")

    # Level read.
    if shift is not None:
        if shift > 4:
            bullets.append("Across maturities the curve shifted higher, a broad rates sell-off.")
        elif shift < -4:
            bullets.append("Across maturities the curve shifted lower, a broad duration rally.")

    # Inversion status.
    inv = _inversion_phrase(metrics)
    if inv:
        bullets.append(inv)

    if not bullets:
        bullets.append("Moves were small across the curve, with no strong macro signal on the day.")
    return bullets


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_engine(features: dict, regimes_cfg: dict) -> dict:
    metrics: Dict[str, float] = features["metrics"]
    meta = regimes_cfg.get("meta", {})
    regimes_def = regimes_cfg["regimes"]

    # Score the real regimes first (everything except neutral_mixed).
    scored: List[dict] = []
    neutral_def = None
    for rd in regimes_def:
        if rd["id"] == "neutral_mixed":
            neutral_def = rd
            continue
        scored.append(score_regime(rd, metrics, meta))

    real_sorted = sorted(scored, key=lambda r: r["score"], reverse=True)
    best_real = real_sorted[0]["score"] if real_sorted else 0.0

    # Neutral / Mixed score is the complement of the best real regime.
    neutral_score = round(clamp(100.0 - best_real, 8.0, 92.0), 1)
    neutral_obj = {
        "id": "neutral_mixed",
        "name": neutral_def["name"] if neutral_def else "Neutral / Mixed",
        "category": "neutral",
        "score": neutral_score,
        "definition": " ".join((neutral_def.get("definition", "") if neutral_def else "").split()),
        "interpretation": " ".join((neutral_def.get("interpretation", "") if neutral_def else "").split()),
        "triggered_rules": [], "not_triggered_rules": [], "missing_rules": [],
        "contradictions": [], "n_confirming": 0, "n_data": 0,
    }

    primary, status, bias = _select_primary(real_sorted, meta)
    if primary is None:
        primary_name, primary_score = neutral_obj["name"], neutral_obj["score"]
        primary_obj_for_summary = None
    else:
        primary_name, primary_score = primary["name"], primary["score"]
        primary_obj_for_summary = primary

    summary = _exec_summary(primary_obj_for_summary, bias, metrics)
    macro_bullets = _macro_bullets(metrics)

    # Full regime list for display: real regimes + neutral, sorted by score.
    all_regimes = sorted(scored + [neutral_obj], key=lambda r: r["score"], reverse=True)

    data_date = features["data_date"]
    report = {
        "data_date": data_date.strftime("%Y-%m-%d"),
        "primary_regime": primary_name,
        "primary_score": primary_score,
        "primary_status": status,           # clear | contested | tentative | mixed
        "bias_regime": bias,
        "summary": summary,
        "curve_summary": features["curve_summary"],
        "regimes": all_regimes,
        "maturities": features["maturities"],
        "spreads": features["spreads"],
        "segments": features["segments"],
        "macro_bullets": macro_bullets,
        "chart": features["chart"],
        "has_breakevens": features.get("has_breakevens", False),
    }

    LOG.info("Primary regime: %s (%.1f%%, %s).", primary_name, primary_score, status)
    return report
