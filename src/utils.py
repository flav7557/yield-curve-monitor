"""Shared utilities: logging, safe math, and HTML formatting helpers."""
from __future__ import annotations

import logging
import math
import os
from typing import Optional

import numpy as np

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def get_logger(name: str = "ycm") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", "%H:%M:%S")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


LOG = get_logger()


# --------------------------------------------------------------------------- #
# Filesystem
# --------------------------------------------------------------------------- #
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------------- #
# Safe numeric handling
# --------------------------------------------------------------------------- #
def safe_float(x) -> Optional[float]:
    """Return a float, or None for NaN / None / non-numeric."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def is_num(x) -> bool:
    return safe_float(x) is not None


def nanmean(values) -> Optional[float]:
    arr = [safe_float(v) for v in values]
    arr = [v for v in arr if v is not None]
    if not arr:
        return None
    return float(np.mean(arr))


# --------------------------------------------------------------------------- #
# Formatting for the HTML report
# --------------------------------------------------------------------------- #
DASH = "&mdash;"


def fmt_yield(x) -> str:
    v = safe_float(x)
    return DASH if v is None else f"{v:.2f}%"


def fmt_bps(x, signed: bool = True) -> str:
    v = safe_float(x)
    if v is None:
        return DASH
    return f"{v:+.1f}" if signed else f"{v:.1f}"


def fmt_bps_int(x) -> str:
    v = safe_float(x)
    if v is None:
        return DASH
    return f"{v:+.0f}"


def fmt_level_bps(x) -> str:
    """Spread levels: signed integer bps."""
    v = safe_float(x)
    if v is None:
        return DASH
    return f"{v:+.0f} bps"


def fmt_z(x) -> str:
    v = safe_float(x)
    return DASH if v is None else f"{v:+.2f}"


def fmt_pct(x, decimals: int = 0) -> str:
    v = safe_float(x)
    return DASH if v is None else f"{v:.{decimals}f}%"


def sign_class(x) -> str:
    """CSS class name based on the sign of a value (for coloured cells)."""
    v = safe_float(x)
    if v is None:
        return "muted"
    if v > 0.05:
        return "up"
    if v < -0.05:
        return "down"
    return "flat"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
