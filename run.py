#!/usr/bin/env python3
"""Yield Curve Monitor - entry point.

Pipeline: load data -> compute features -> score regimes -> render HTML -> save history.

The run never crashes on a single missing series. If every live source is
unavailable it falls back to clearly-flagged synthetic demo data so the page
(and the GitHub Action) still completes.

Usage:
    python run.py             # live data (Treasury primary, FRED fallback)
    python run.py --demo      # synthetic demo data
    python run.py --years 6   # override how many years of Treasury history to pull
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from src.curve_features import compute_features
from src.data_loader import generate_demo_data, load_data, load_series_config
from src.html_report import render
from src.regime_engine import HISTORY_SCORE_COLS, load_regimes_config, run_engine
from src.utils import LOG, ensure_dir, safe_float

OUTPUT_HTML = "output/index.html"
HISTORY_CSV = "data/yield_curve_history.csv"

HISTORY_COLUMNS = [
    "run_date", "data_date", "primary_regime", "primary_score",
    "bear_flattening_score", "bear_steepening_score", "bull_steepening_score",
    "bull_flattening_score", "parallel_bear_shift_score", "parallel_bull_shift_score",
    "rates_shock_score", "recession_pricing_score", "inflation_repricing_score",
    "term_premium_shock_score", "deep_inversion_score", "disinversion_score",
    "neutral_mixed_score",
    "2y_yield", "5y_yield", "10y_yield", "30y_yield",
    "2s10s", "5s30s", "3m10y",
    "front_end_avg_1d", "belly_avg_1d", "long_end_avg_1d",
]


# --------------------------------------------------------------------------- #
# History persistence
# --------------------------------------------------------------------------- #
def _build_history_row(report: dict, features: dict) -> dict:
    metrics = features["metrics"]
    cs = report.get("curve_summary", {})
    scores_by_id = {r["id"]: r["score"] for r in report.get("regimes", [])}

    row = {c: None for c in HISTORY_COLUMNS}
    row["run_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row["data_date"] = report.get("data_date")
    row["primary_regime"] = report.get("primary_regime")
    row["primary_score"] = report.get("primary_score")

    for rid, col in HISTORY_SCORE_COLS.items():
        if rid in scores_by_id:
            row[col] = scores_by_id[rid]

    row["2y_yield"] = safe_float(metrics.get("2Y.level"))
    row["5y_yield"] = safe_float(metrics.get("5Y.level"))
    row["10y_yield"] = safe_float(metrics.get("10Y.level"))
    row["30y_yield"] = safe_float(metrics.get("30Y.level"))
    row["2s10s"] = safe_float(metrics.get("spread.2s10s.level"))
    row["5s30s"] = safe_float(metrics.get("spread.5s30s.level"))
    row["3m10y"] = safe_float(metrics.get("spread.3m10y.level"))
    row["front_end_avg_1d"] = safe_float(cs.get("front_end_avg_1d"))
    row["belly_avg_1d"] = safe_float(cs.get("belly_avg_1d"))
    row["long_end_avg_1d"] = safe_float(cs.get("long_end_avg_1d"))
    return row


def _update_history(row: dict) -> pd.DataFrame:
    ensure_dir("data")
    frames = []
    if os.path.exists(HISTORY_CSV):
        try:
            frames.append(pd.read_csv(HISTORY_CSV))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not read existing history (%s); starting fresh.", exc)

    frames.append(pd.DataFrame([row]))
    hist = pd.concat(frames, ignore_index=True)

    # One row per run_date, keep the most recent computation.
    hist = hist.drop_duplicates(subset="run_date", keep="last")
    hist = hist.sort_values("run_date").reset_index(drop=True)
    hist = hist.reindex(columns=HISTORY_COLUMNS)

    try:
        hist.to_csv(HISTORY_CSV, index=False)
        LOG.info("History updated: %d rows in %s.", len(hist), HISTORY_CSV)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not write history CSV: %s", exc)
    return hist


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _resolve_demo_flag(arg_demo: bool) -> bool:
    if arg_demo:
        return True
    return os.environ.get("YCM_DEMO", "").strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the Yield Curve Monitor dashboard.")
    parser.add_argument("--demo", action="store_true", help="use synthetic demo data")
    parser.add_argument("--years", type=int, default=None,
                        help="override how many years of Treasury history to pull")
    args = parser.parse_args()

    demo = _resolve_demo_flag(args.demo)

    cfg = load_series_config()
    if args.years:
        cfg["treasury"]["years_back"] = args.years
    regimes_cfg = load_regimes_config()

    # ---- Load data (with automatic demo fallback) ------------------------- #
    bundle = load_data(cfg, demo=demo)
    if bundle.get("yields") is None:
        LOG.error("No live data available - falling back to synthetic demo data so the page still builds.")
        fb = generate_demo_data(cfg)
        fb.update(
            source="DEMO (live sources unavailable)",
            demo=True,
            warnings=(bundle.get("warnings") or []) + ["Live sources unavailable; showing synthetic demo data."],
        )
        bundle = fb
        demo = True

    source = bundle.get("source", "unknown")
    warnings = bundle.get("warnings", [])

    # ---- Compute -> classify --------------------------------------------- #
    features = compute_features(bundle, cfg)
    report = run_engine(features, regimes_cfg)

    # ---- History --------------------------------------------------------- #
    row = _build_history_row(report, features)
    history_df = _update_history(row)

    # ---- Render ---------------------------------------------------------- #
    html = render(report, history_df, demo=demo, source=source, warnings=warnings)
    ensure_dir("output")
    with open(OUTPUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    LOG.info("Wrote %s (%d KB).", OUTPUT_HTML, len(html) // 1024)

    print(f"\nDone. Primary regime: {report['primary_regime']} "
          f"({report['primary_score']:.0f}%). Open {OUTPUT_HTML}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
