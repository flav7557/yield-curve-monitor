"""Compute curve features from the yield history.

Produces:
  * a flat `metrics` dict consumed by the regime engine
  * structured per-maturity / per-spread / per-segment objects for the report
  * the data needed to draw the curve chart
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .utils import LOG, nanmean, safe_float

# Spread definitions: name -> (long_leg, short_leg); level = long - short.
SPREAD_DEFS = {
    "2s10s": ("10Y", "2Y"),
    "5s30s": ("30Y", "5Y"),
    "3m10y": ("10Y", "3M"),
    "2s5s": ("5Y", "2Y"),
    "10s30s": ("30Y", "10Y"),
    "1y10y": ("10Y", "1Y"),
    "2s30s": ("30Y", "2Y"),  # internal helper, not displayed
}
DISPLAY_SPREADS = ["2s10s", "5s30s", "3m10y", "2s5s", "10s30s", "1y10y"]

# Segment membership used for averages (per spec section 4).
SEGMENTS = {
    "front": ["3M", "6M", "1Y", "2Y"],
    "belly": ["3Y", "5Y", "7Y"],
    "long": ["10Y", "20Y", "30Y"],
}

# Metric-name stems for breakevens / real yields.
BE_MAP = {"5Y_BE": "be.5y", "10Y_BE": "be.10y", "5Y5Y_BE": "be.5y5y"}
REAL_MAP = {"5Y_REAL": "real.5y", "10Y_REAL": "real.10y", "30Y_REAL": "real.30y"}


# --------------------------------------------------------------------------- #
# Low-level series helpers (all changes returned in basis points)
# --------------------------------------------------------------------------- #
def _change_bps(level: pd.Series, lag: int) -> Optional[float]:
    s = level.dropna()
    if len(s) <= lag:
        return None
    return float((s.iloc[-1] - s.iloc[-1 - lag]) * 100.0)


def _zscore(level: pd.Series, lag: int, window: int = 252, min_obs: int = 30) -> Optional[float]:
    chg = (level - level.shift(lag)) * 100.0
    chg = chg.dropna()
    if len(chg) < min_obs:
        return None
    ref = chg.tail(window)
    sd = ref.std(ddof=0)
    if sd is None or sd == 0 or np.isnan(sd):
        return None
    return float((chg.iloc[-1] - ref.mean()) / sd)


def _percentile(level: pd.Series, years: float) -> Optional[float]:
    s = level.dropna()
    if s.empty:
        return None
    window = int(years * 252)
    ref = s.tail(window)
    if len(ref) < 30:
        return None
    latest = s.iloc[-1]
    return float((ref <= latest).mean() * 100.0)


# --------------------------------------------------------------------------- #
# Spread series
# --------------------------------------------------------------------------- #
def _spread_series(yields: pd.DataFrame, name: str) -> Optional[pd.Series]:
    long_leg, short_leg = SPREAD_DEFS[name]
    if long_leg not in yields.columns or short_leg not in yields.columns:
        return None
    s = (yields[long_leg] - yields[short_leg]) * 100.0  # bps
    return s.dropna()


def _recently_inverted(spread: pd.Series, lookback: int = 20) -> float:
    tail = spread.dropna().tail(lookback)
    if tail.empty:
        return 0.0
    return 1.0 if float(tail.min()) < 0 else 0.0


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def compute_features(bundle: dict, cfg: dict) -> dict:
    yields: pd.DataFrame = bundle["yields"]
    breakevens: pd.DataFrame = bundle.get("breakevens", pd.DataFrame())
    real_yields: pd.DataFrame = bundle.get("real_yields", pd.DataFrame())

    yields = yields.sort_index()
    available = [c for c in yields.columns if yields[c].notna().any()]
    data_date = yields[available].dropna(how="all").index[-1]
    yields = yields.loc[:data_date]

    prev_idx = yields[available].dropna(how="all").index
    prev_date = prev_idx[-2] if len(prev_idx) >= 2 else None

    years_map = {m["key"]: m["years"] for m in cfg["maturities"]}

    metrics: Dict[str, float] = {}
    maturities: List[dict] = []

    # ---- Per-maturity features -------------------------------------------- #
    for m in cfg["maturities"]:
        key = m["key"]
        if key not in yields.columns:
            continue
        level_series = yields[key].dropna()
        if level_series.empty:
            continue
        level = float(level_series.iloc[-1])
        chg_1d = _change_bps(level_series, 1)
        chg_5d = _change_bps(level_series, 5)
        chg_20d = _change_bps(level_series, 20)
        chg_60d = _change_bps(level_series, 60)
        z_1d = _zscore(level_series, 1)
        z_5d = _zscore(level_series, 5)
        p3 = _percentile(level_series, 3)
        p10 = _percentile(level_series, 10)

        for stem, val in (
            ("level", level), ("chg_1d", chg_1d), ("chg_5d", chg_5d),
            ("chg_20d", chg_20d), ("chg_60d", chg_60d), ("z_1d", z_1d), ("z_5d", z_5d),
        ):
            if val is not None:
                metrics[f"{key}.{stem}"] = val

        maturities.append({
            "maturity": key, "years": years_map.get(key),
            "yield": level, "change_1d_bps": chg_1d, "change_5d_bps": chg_5d,
            "change_20d_bps": chg_20d, "change_60d_bps": chg_60d,
            "zscore_1d": z_1d, "zscore_5d": z_5d,
            "pctile_3y": p3, "pctile_10y": p10,
        })

    # ---- Spreads ----------------------------------------------------------- #
    spread_objs: List[dict] = []
    for name in SPREAD_DEFS:
        s = _spread_series(yields, name)
        if s is None or s.empty:
            continue
        level = float(s.iloc[-1])
        chg_1d = _change_from_series_bps(s, 1)
        chg_5d = _change_from_series_bps(s, 5)
        chg_20d = _change_from_series_bps(s, 20)
        z_1d = _spread_zscore(s)
        recently = _recently_inverted(s)

        metrics[f"spread.{name}.level"] = level
        if chg_1d is not None:
            metrics[f"spread.{name}.chg_1d"] = chg_1d
        if chg_5d is not None:
            metrics[f"spread.{name}.chg_5d"] = chg_5d
        if chg_20d is not None:
            metrics[f"spread.{name}.chg_20d"] = chg_20d
        if z_1d is not None:
            metrics[f"spread.{name}.z_1d"] = z_1d
        metrics[f"spread.{name}.recently_inverted"] = recently

        if name in DISPLAY_SPREADS:
            spread_objs.append({
                "spread": name, "level_bps": level,
                "change_1d_bps": chg_1d, "change_5d_bps": chg_5d, "change_20d_bps": chg_20d,
                "zscore_1d": z_1d, "status": _inversion_status(name, level),
                "interpretation": _spread_interpretation(name, level, chg_1d),
            })

    # ---- Segments ---------------------------------------------------------- #
    seg_objs: Dict[str, dict] = {}
    for seg, members in SEGMENTS.items():
        present = [m for m in members if m in yields.columns]
        c1 = nanmean([metrics.get(f"{m}.chg_1d") for m in present])
        c5 = nanmean([metrics.get(f"{m}.chg_5d") for m in present])
        c20 = nanmean([metrics.get(f"{m}.chg_20d") for m in present])
        if c1 is not None:
            metrics[f"seg.{seg}.chg_1d"] = c1
        if c5 is not None:
            metrics[f"seg.{seg}.chg_5d"] = c5
        if c20 is not None:
            metrics[f"seg.{seg}.chg_20d"] = c20
        seg_objs[seg] = {
            "members": present, "avg_1d": c1, "avg_5d": c5, "avg_20d": c20,
        }

    # ---- Derived curve metrics -------------------------------------------- #
    all_chg_1d = [metrics.get(f"{m['key']}.chg_1d") for m in cfg["maturities"]]
    curve_shift_1d = nanmean(all_chg_1d)
    if curve_shift_1d is not None:
        metrics["curve.shift_1d"] = curve_shift_1d

    front, belly, long = metrics.get("seg.front.chg_1d"), metrics.get("seg.belly.chg_1d"), metrics.get("seg.long.chg_1d")
    if front is not None and long is not None:
        metrics["curve.front_vs_long_1d"] = front - long
        metrics["curve.long_vs_front_1d"] = long - front
        metrics["curve.long_vs_front_abs_1d"] = abs(long) - abs(front)
    if belly is not None and front is not None and long is not None:
        metrics["curve.belly_vs_wings_1d"] = belly - (front + long) / 2.0

    # Contribution of each segment to the overall move.
    for seg, obj in seg_objs.items():
        if obj["avg_1d"] is not None and curve_shift_1d not in (None, 0):
            obj["contribution_pct"] = round(obj["avg_1d"] / curve_shift_1d * 100.0, 0)
        else:
            obj["contribution_pct"] = None
        obj["interpretation"] = _segment_interpretation(seg, obj["avg_1d"])

    # ---- Breakevens / real yields ----------------------------------------- #
    _add_optional_metrics(breakevens, BE_MAP, metrics)
    _add_optional_metrics(real_yields, REAL_MAP, metrics)
    has_breakevens = not breakevens.empty

    # ---- Curve chart series ----------------------------------------------- #
    chart = _build_chart(yields, cfg, available)

    curve_summary = {
        "curve_shift_1d": curve_shift_1d,
        "front_end_avg_1d": front,
        "belly_avg_1d": belly,
        "long_end_avg_1d": long,
        "front_vs_long_1d": metrics.get("curve.front_vs_long_1d"),
        "2s10s": metrics.get("spread.2s10s.level"),
        "2s10s_change_1d": metrics.get("spread.2s10s.chg_1d"),
        "5s30s": metrics.get("spread.5s30s.level"),
        "5s30s_change_1d": metrics.get("spread.5s30s.chg_1d"),
        "3m10y": metrics.get("spread.3m10y.level"),
    }

    LOG.info("Computed %d metrics over %d maturities (data date %s).",
             len(metrics), len(maturities), data_date.date())

    return {
        "metrics": metrics,
        "maturities": maturities,
        "spreads": spread_objs,
        "segments": seg_objs,
        "curve_summary": curve_summary,
        "chart": chart,
        "data_date": data_date,
        "prev_date": prev_date,
        "has_breakevens": has_breakevens,
    }


# --------------------------------------------------------------------------- #
# Helpers used above
# --------------------------------------------------------------------------- #
def _change_from_series_bps(series: pd.Series, lag: int) -> Optional[float]:
    """Series already in bps -> change is a plain difference."""
    s = series.dropna()
    if len(s) <= lag:
        return None
    return float(s.iloc[-1] - s.iloc[-1 - lag])


def _spread_zscore(series: pd.Series, lag: int = 1, window: int = 252, min_obs: int = 30) -> Optional[float]:
    chg = (series - series.shift(lag)).dropna()
    if len(chg) < min_obs:
        return None
    ref = chg.tail(window)
    sd = ref.std(ddof=0)
    if sd in (None, 0) or np.isnan(sd):
        return None
    return float((chg.iloc[-1] - ref.mean()) / sd)


def _add_optional_metrics(df: pd.DataFrame, mapping: Dict[str, str], metrics: dict) -> None:
    if df is None or df.empty:
        return
    df = df.sort_index()
    for col, stem in mapping.items():
        if col not in df.columns:
            continue
        s = df[col].dropna()
        if s.empty:
            continue
        metrics[f"{stem}.level"] = float(s.iloc[-1])
        chg = _change_bps(s, 1)
        if chg is not None:
            metrics[f"{stem}.chg_1d"] = chg


def _build_chart(yields: pd.DataFrame, cfg: dict, available: List[str]) -> dict:
    order = [m["key"] for m in cfg["maturities"] if m["key"] in available]
    years = [next(m["years"] for m in cfg["maturities"] if m["key"] == k) for k in order]
    idx = yields[available].dropna(how="all").index

    def row_at(offset: int):
        if len(idx) <= offset:
            return None, None
        d = idx[-1 - offset]
        vals = [safe_float(yields.loc[d, k]) if k in yields.columns else None for k in order]
        return d.strftime("%Y-%m-%d"), vals

    series = {}
    for label, off in (("today", 0), ("d1", 1), ("d5", 5), ("d20", 20)):
        d, vals = row_at(off)
        series[label] = {"date": d, "values": vals}

    return {"labels": order, "years": years, "series": series}


# --------------------------------------------------------------------------- #
# Interpretation strings
# --------------------------------------------------------------------------- #
def _inversion_status(name: str, level: float) -> str:
    if level is None:
        return "n/a"
    if level < -75:
        return "deeply inverted"
    if level < 0:
        return "inverted"
    if level > 100:
        return "steep"
    return "normal"


def _spread_interpretation(name: str, level: Optional[float], chg: Optional[float]) -> str:
    if level is None:
        return "No data."
    sign = "inverted" if level < 0 else "positive"
    move = ""
    c = safe_float(chg)
    if c is not None:
        if c > 1:
            move = " and steepened on the day"
        elif c < -1:
            move = " and flattened on the day"
        else:
            move = " and was little changed"
    return f"The {name} spread is {sign} at {level:+.0f} bps{move}."


def _segment_interpretation(seg: str, avg: Optional[float]) -> str:
    v = safe_float(avg)
    if v is None:
        return "No data."
    label = {"front": "Front-end", "belly": "Belly", "long": "Long-end"}[seg]
    direction = "rose" if v > 0 else "fell" if v < 0 else "was flat"
    return f"{label} {direction} by {abs(v):.1f} bps on average."
