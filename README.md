# Yield Curve Monitor

A free, fully-automated dashboard that analyses the **U.S. Treasury yield curve**
every day and classifies the move into an interpretable **rates regime**
(bear flattening, bull steepening, term-premium shock, deep inversion, and so on).

The output is a single static page, `output/index.html`, designed to be published
on **GitHub Pages** and refreshed daily by **GitHub Actions** — no server, no
database, no paid data, no API key.

It is built to help you read the curve like a macro rates analyst: not just
"10Y up", but *"the 2Y rose more than the 10Y, so the curve bear-flattened, which
usually means the market is pricing a more restrictive Fed."*

---

## What it shows

- **Executive summary** — primary regime, a confidence score, a plain-English
  read of the day, and the headline curve move.
- **Regime scores** — every regime scored 0–100% as a horizontal bar. Click any
  regime to expand the exact rules that fired, the rules that did not, missing
  data, and contradictory signals, each with the observed value, threshold,
  weight and an explanation.
- **Yield table** — level and 1D / 5D / 20D changes (bps) plus a 1D z-score per
  maturity.
- **Curve spreads** — 2s10s, 5s30s, 3m10y, 2s5s, 10s30s, 1y10y with levels,
  changes and interpretation.
- **Curve chart** — today vs 1, 5 and 20 business days ago (interactive, Plotly).
- **Segment analysis** — front-end / belly / long-end average moves and their
  contribution to the overall shift.
- **Macro interpretation** — automatic bullets on Fed pricing, recession vs
  inflation signals, slope and inversion status.
- **Historical context** — the last 10 saved observations.

---

## Data sources (all free)

| Source | Use | Key needed |
| ------ | --- | ---------- |
| U.S. Treasury — Daily Par Yield Curve Rates (CSV) | Primary nominal yields | No |
| FRED CSV download endpoint (`DGS*`) | Fallback nominal yields | No |
| FRED breakevens (`T5YIE`, `T10YIE`, `T5YIFR`) | Optional inflation read | No |
| FRED TIPS real yields (`DFII5/10/30`) | Optional real-rate read | No |

If the primary source fails the app falls back to FRED, warns, and keeps going.
A single missing series never stops the run. If **every** live source is
unavailable, the page is still produced from clearly-flagged synthetic demo data.

---

## Local usage

```bash
pip install -r requirements.txt
python run.py
open output/index.html        # macOS  (use xdg-open on Linux, start on Windows)
```

Options:

```bash
python run.py --demo          # build from synthetic demo data (no network needed)
python run.py --years 6       # pull 6 years of Treasury history instead of 4
```

> Behind a firewall? `treasury.gov` and `fred.stlouisfed.org` must be reachable
> for live data. Use `--demo` to preview the dashboard offline.

---

## Put it on GitHub Pages

1. Create a new GitHub repository and push this project to it.
2. In the repo, go to **Settings → Pages**.
3. Under **Build and deployment → Source**, choose **GitHub Actions**.
4. Open the **Actions** tab, select **Update Yield Curve Monitor**, and click
   **Run workflow** once to do the first build.
5. When it finishes, your dashboard is live at
   `https://<your-username>.github.io/<your-repo>/`.

After that the workflow runs automatically on weekdays at 06:30 UTC
(`.github/workflows/update-dashboard.yml`), regenerates the page, commits the
updated history CSV, and redeploys. You can always trigger it manually too.

---

## Configuration

### Series — `config/series.yml`
Add, remove or remap maturities, point a maturity at a different FRED id, or
change how many years of history to pull. Each maturity has an internal `key`,
its `years` (the chart x-axis), the Treasury column name, and the FRED id.

### Regimes — `config/regimes.yml`
This is the rule book. Every regime has `confirming` rules and `contradictions`.
Each rule is:

```yaml
- {name: "2Y yield rose more than +5 bps", metric: "2Y.chg_1d", op: ">",
   threshold: 5, weight: 2, explanation: "The 2Y is policy-sensitive..."}
```

Supported operators: `>`, `>=`, `<`, `<=`, `abs>`, `abs>=`, `abs<`, `abs<=`,
`between`. Metrics come from the namespace documented at the top of the file
(per-maturity `*.chg_1d` / `*.z_1d`, `spread.*`, `seg.*`, `curve.*`, `be.*`,
`real.*`). Tune thresholds and weights freely — the scoring and the clickable
detail panels update automatically. The `meta` block controls the confidence
thresholds used to pick the headline regime.

---

## How the score works

For each regime: take the confirming-rule weight that triggered, subtract a
penalty for any triggered contradictions, normalise by the confirming weight
that actually had data, then dampen by data coverage. The result is a 0–100%
score. "Neutral / Mixed" is scored as the complement of the strongest regime, so
when nothing fits, it rises. The headline regime is chosen using the confidence
and gap thresholds in `config/regimes.yml`.

---

## Methodology & limits

- The classifier is **heuristic and rule-based**, not a statistical or trained
  model. Thresholds are sensible defaults, not estimated parameters.
- Par yields are end-of-day; intraday moves are not captured.
- Z-scores and percentiles need history to warm up; early on they may be marked
  missing.
- Breakeven / real-yield rules are skipped when those series are unavailable.
- Regime labels describe the *shape and direction* of the move, not a forecast.

## Disclaimer

This project is for educational and informational purposes only. It is **not
investment advice**, and nothing here is a recommendation to buy or sell any
security. Use at your own risk.
