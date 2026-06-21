"""Load U.S. Treasury par-yield data (primary) with a FRED fallback.

Both sources are free and require no API key. Everything is defensive: a single
missing series or a failing source never crashes the run.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests
import yaml

from .utils import LOG, ensure_dir

USER_AGENT = "yield-curve-monitor/1.0 (+https://github.com)"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_series_config(path: str = "config/series.yml") -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int) -> Optional[str]:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as exc:  # noqa: BLE001 - we want to swallow *any* network error
        LOG.warning("Request failed (%s): %s", url.split("?")[0], exc)
        return None


# --------------------------------------------------------------------------- #
# Treasury (primary)
# --------------------------------------------------------------------------- #
def fetch_treasury(cfg: dict, warnings: List[str]) -> Optional[pd.DataFrame]:
    tcfg = cfg["treasury"]
    if not tcfg.get("enabled", True):
        return None

    col_map = {m["treasury"]: m["key"] for m in cfg["maturities"] if m.get("treasury")}
    this_year = datetime.now(timezone.utc).year
    years = range(this_year, this_year - int(tcfg.get("years_back", 4)), -1)

    frames = []
    for year in years:
        url = tcfg["url_template"].format(year=year)
        text = _get(url, tcfg.get("timeout", 30))
        if not text:
            continue
        try:
            df = pd.read_csv(io.StringIO(text))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Treasury CSV parse failed for %s: %s", year, exc)
            continue
        if "Date" not in df.columns:
            continue
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        keep = {"Date": "date"}
        keep.update({c: col_map[c] for c in df.columns if c in col_map})
        df = df[list(keep)].rename(columns=keep)
        frames.append(df)

    if not frames:
        warnings.append("Treasury source returned no data; falling back to FRED.")
        return None

    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["date"]).drop_duplicates("date").set_index("date").sort_index()
    out = out.apply(pd.to_numeric, errors="coerce")
    LOG.info("Treasury: loaded %d rows, %d maturities.", len(out), out.shape[1])
    return out


# --------------------------------------------------------------------------- #
# FRED (fallback for nominal, primary for breakevens / real yields)
# --------------------------------------------------------------------------- #
def _fetch_fred_series(series_id: str, start: str, csv_template: str, timeout: int) -> Optional[pd.Series]:
    url = csv_template.format(series=series_id, start=start)
    text = _get(url, timeout)
    if not text:
        return None
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception:  # noqa: BLE001
        return None
    date_col = next((c for c in df.columns if c.lower() in ("date", "observation_date")), df.columns[0])
    val_col = next((c for c in df.columns if c != date_col), None)
    if val_col is None:
        return None
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    s = pd.to_numeric(df[val_col].replace(".", np.nan), errors="coerce")
    s.index = df[date_col]
    return s.dropna()


def fetch_fred_group(items: List[dict], key_field: str, cfg: dict, warnings: List[str]) -> pd.DataFrame:
    fcfg = cfg["fred"]
    if not fcfg.get("enabled", True):
        return pd.DataFrame()
    cols: Dict[str, pd.Series] = {}
    for item in items:
        sid = item.get("fred")
        if not sid:
            continue
        s = _fetch_fred_series(sid, fcfg["start_date"], fcfg["csv_template"], fcfg.get("timeout", 30))
        if s is None or s.empty:
            warnings.append(f"FRED series {sid} ({item[key_field]}) unavailable.")
            continue
        cols[item[key_field]] = s
    if not cols:
        return pd.DataFrame()
    df = pd.DataFrame(cols).sort_index()
    return df


def fetch_fred_nominal(cfg: dict, warnings: List[str]) -> Optional[pd.DataFrame]:
    df = fetch_fred_group(cfg["maturities"], "key", cfg, warnings)
    if df.empty:
        return None
    LOG.info("FRED nominal: loaded %d rows, %d maturities.", len(df), df.shape[1])
    return df


# --------------------------------------------------------------------------- #
# Demo generator (clearly flagged synthetic data)
# --------------------------------------------------------------------------- #
def generate_demo_data(cfg: dict) -> Dict[str, pd.DataFrame]:
    """Synthetic yields engineered to show a bear-flattening move on the last day.

    Used only when --demo is passed or all live sources fail in demo mode.
    The HTML banner makes clear the data is not real.
    """
    rng = np.random.default_rng(42)
    end = pd.Timestamp(datetime.now(timezone.utc).date())
    dates = pd.bdate_range(end=end, periods=820)
    n = len(dates)  # bdate_range can trim when `end` lands on a weekend

    base = {
        "1M": 5.30, "2M": 5.28, "3M": 5.25, "4M": 5.18, "6M": 5.05, "1Y": 4.78,
        "2Y": 4.52, "3Y": 4.38, "5Y": 4.28, "7Y": 4.34, "10Y": 4.45,
        "20Y": 4.80, "30Y": 4.62,
    }
    mats = [m["key"] for m in cfg["maturities"] if m["key"] in base]
    data = {}
    for m in mats:
        # mild mean-reverting random walk around the base level
        steps = rng.normal(0, 0.035, n).cumsum()
        steps -= np.linspace(0, steps[-1], n)  # pin the end near base
        series = base[m] + steps + rng.normal(0, 0.01, n)
        data[m] = series
    df = pd.DataFrame(data, index=dates)

    # Engineer the final day: front-end up sharply, long-end roughly flat.
    bumps = {"1M": 6, "2M": 7, "3M": 7, "4M": 8, "6M": 8, "1Y": 9, "2Y": 9,
             "3Y": 7, "5Y": 5, "7Y": 4, "10Y": 2, "20Y": 1, "30Y": 1}
    for m, bp in bumps.items():
        if m in df.columns:
            df.iloc[-1, df.columns.get_loc(m)] = df.iloc[-2, df.columns.get_loc(m)] + bp / 100.0

    # Synthetic breakevens / real yields (small, stable).
    be = pd.DataFrame({
        "5Y_BE": 2.35 + rng.normal(0, 0.01, n).cumsum() * 0.1,
        "10Y_BE": 2.30 + rng.normal(0, 0.01, n).cumsum() * 0.1,
        "5Y5Y_BE": 2.45 + rng.normal(0, 0.01, n).cumsum() * 0.1,
    }, index=dates)
    real = pd.DataFrame({
        "5Y_REAL": 1.95 + rng.normal(0, 0.01, n).cumsum() * 0.1,
        "10Y_REAL": 2.10 + rng.normal(0, 0.01, n).cumsum() * 0.1,
        "30Y_REAL": 2.25 + rng.normal(0, 0.01, n).cumsum() * 0.1,
    }, index=dates)

    return {"yields": df, "breakevens": be, "real_yields": real}


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _save_processed(df: pd.DataFrame, name: str) -> None:
    ensure_dir("data/processed")
    try:
        df.to_csv(f"data/processed/{name}.csv")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not write processed/%s.csv: %s", name, exc)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def load_data(cfg: dict, demo: bool = False) -> dict:
    """Return {yields, breakevens, real_yields, source, demo, warnings}."""
    warnings: List[str] = []

    if demo:
        LOG.warning("DEMO MODE: generating synthetic yield data.")
        bundle = generate_demo_data(cfg)
        bundle.update(source="DEMO (synthetic)", demo=True, warnings=["Running on synthetic demo data."])
        _save_processed(bundle["yields"], "yields")
        return bundle

    # 1) Treasury primary
    yields = fetch_treasury(cfg, warnings)
    source = "U.S. Treasury (Par Yield Curve)"

    # 2) FRED nominal fallback
    if yields is None or yields.dropna(how="all").empty:
        yields = fetch_fred_nominal(cfg, warnings)
        source = "FRED (fallback)"

    if yields is None or yields.dropna(how="all").empty:
        warnings.append("All live data sources failed.")
        LOG.error("No live data available from Treasury or FRED.")
        return {"yields": None, "breakevens": pd.DataFrame(), "real_yields": pd.DataFrame(),
                "source": "none", "demo": False, "warnings": warnings}

    # 3) Optional breakevens / real yields (FRED only, non-fatal)
    breakevens = fetch_fred_group(cfg.get("breakevens", []), "key", cfg, warnings)
    real_yields = fetch_fred_group(cfg.get("real_yields", []), "key", cfg, warnings)

    _save_processed(yields, "yields")
    if not breakevens.empty:
        _save_processed(breakevens, "breakevens")
    if not real_yields.empty:
        _save_processed(real_yields, "real_yields")

    return {
        "yields": yields,
        "breakevens": breakevens,
        "real_yields": real_yields,
        "source": source,
        "demo": False,
        "warnings": warnings,
    }
