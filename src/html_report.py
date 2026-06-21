"""Render the analysis into a single self-contained output/index.html.

Hybrid approach: all text/sections are server-rendered in Python; only the curve
chart (Plotly via CDN) and the click-to-expand regime panels use a little
vanilla JS. The page needs no web server and no data fetch at view time.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd

from .utils import (
    DASH, fmt_bps, fmt_level_bps, fmt_pct, fmt_yield, fmt_z, safe_float, sign_class,
)

# Category -> accent colour for regime bars / panels.
CATEGORY_COLOR = {
    "bear": "#e5484d",          # red
    "bull": "#30a46c",          # green
    "shock": "#f76808",         # orange
    "inflation": "#a371f7",     # purple
    "neutral": "#8b949e",       # grey
    "neutral_color": "#1fb8b0",  # teal (disinversion)
}

STATUS_LABEL = {
    "clear": "High confidence",
    "contested": "Contested",
    "tentative": "Tentative",
    "mixed": "Mixed / no clear regime",
}
STATUS_COLOR = {
    "clear": "#30a46c", "contested": "#d29922",
    "tentative": "#d29922", "mixed": "#8b949e",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _esc(text) -> str:
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _fmt_observed(rule: dict) -> str:
    v = safe_float(rule.get("observed"))
    if v is None:
        return DASH
    metric = rule.get("metric", "")
    if metric.endswith(".z_1d") or metric.endswith(".z_5d"):
        return f"{v:+.2f}"
    if metric.endswith(".recently_inverted"):
        return "yes (recent)" if v >= 0.5 else "no"
    return f"{v:+.1f} bps"


def _fmt_threshold(rule: dict) -> str:
    thr = rule.get("threshold")
    metric = rule.get("metric", "")
    if isinstance(thr, (list, tuple)):
        return f"[{thr[0]}, {thr[1]}]"
    if metric.endswith(".z_1d") or metric.endswith(".z_5d") or metric.endswith(".recently_inverted"):
        return f"{thr}"
    return f"{thr} bps"


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def _banner(demo: bool, source: str) -> str:
    if not demo:
        return ""
    return (
        '<div class="banner">&#9888; <strong>Demo / synthetic data.</strong> '
        f'This page was generated from generated sample data ({_esc(source)}), not live '
        'market rates. Run with live network access for real Treasury / FRED data.</div>'
    )


def _warnings_block(warnings: List[str]) -> str:
    real = [w for w in (warnings or []) if w and "synthetic demo data" not in w.lower()]
    if not real:
        return ""
    items = "".join(f"<li>{_esc(w)}</li>" for w in real)
    return f'<details class="warns"><summary>Data warnings ({len(real)})</summary><ul>{items}</ul></details>'


def _exec_section(report: dict) -> str:
    status = report.get("primary_status", "mixed")
    color = STATUS_COLOR.get(status, "#8b949e")
    label = STATUS_LABEL.get(status, "Mixed")
    score = report.get("primary_score", 0.0)
    cs = report.get("curve_summary", {})

    def cell(lbl, val):
        return (f'<div class="kpi"><div class="kpi-val">{val}</div>'
                f'<div class="kpi-lbl">{lbl}</div></div>')

    kpis = "".join([
        cell("Curve shift (1D)", fmt_bps(cs.get("curve_shift_1d"))),
        cell("Front-end (1D)", fmt_bps(cs.get("front_end_avg_1d"))),
        cell("Belly (1D)", fmt_bps(cs.get("belly_avg_1d"))),
        cell("Long-end (1D)", fmt_bps(cs.get("long_end_avg_1d"))),
        cell("2s10s", fmt_level_bps(cs.get("2s10s"))),
        cell("2s10s &Delta;1D", fmt_bps(cs.get("2s10s_change_1d"))),
    ])

    return f"""
    <section class="card exec">
      <div class="exec-head">
        <div>
          <div class="exec-eyebrow">Primary rates regime</div>
          <h2 class="exec-regime">{_esc(report.get('primary_regime'))}</h2>
        </div>
        <div class="exec-score">
          <div class="score-num">{score:.0f}<span>%</span></div>
          <div class="score-pill" style="background:{color}1f;color:{color};border-color:{color}55">{label}</div>
        </div>
      </div>
      <p class="exec-summary">{_esc(report.get('summary'))}</p>
      <div class="kpi-row">{kpis}</div>
    </section>
    """


def _rule_block(icon: str, title: str, rules: List[dict], css: str) -> str:
    if not rules:
        return ""
    rows = []
    for r in rules:
        rows.append(
            f'<div class="rule {css}">'
            f'<div class="rule-top"><span class="rule-name">{icon} {_esc(r["name"])}</span>'
            f'<span class="rule-w">w{r.get("weight", 1)}</span></div>'
            f'<div class="rule-meta">Observed <b>{_fmt_observed(r)}</b> '
            f'&nbsp;&middot;&nbsp; Condition {_esc(r["condition"])} {_fmt_threshold(r)}</div>'
            f'<div class="rule-exp">{_esc(r.get("explanation"))}</div>'
            f'</div>'
        )
    return f'<div class="rule-group"><div class="rule-group-h">{title} ({len(rules)})</div>{"".join(rows)}</div>'


def _regime_section(report: dict) -> str:
    regimes = report.get("regimes", [])
    max_score = max((r["score"] for r in regimes), default=100) or 100
    rows = []
    for i, r in enumerate(regimes):
        color = CATEGORY_COLOR.get(r.get("category", "neutral"), "#8b949e")
        pct = r["score"]
        width = max(2.0, pct / max_score * 100.0)
        pid = f"rp{i}"

        detail = (
            _rule_block("&#9989;", "Triggered rules", r.get("triggered_rules", []), "ok")
            + _rule_block("&#10060;", "Not triggered", r.get("not_triggered_rules", []), "no")
            + _rule_block("&#9888;", "Missing data", r.get("missing_rules", []), "miss")
            + _rule_block("&#128317;", "Contradictions", r.get("contradictions", []), "contra")
        )
        cov = ""
        if r.get("n_confirming"):
            cov = f'<span class="cov">{r.get("n_data", 0)}/{r["n_confirming"]} rules had data</span>'
        if not detail:
            detail = '<div class="rule-empty">This regime is scored as the complement of the strongest regime.</div>'

        rows.append(f"""
        <div class="regime">
          <button class="regime-bar" onclick="toggleDetail('{pid}')" aria-expanded="false">
            <span class="regime-name">{_esc(r['name'])}</span>
            <span class="bar-wrap"><span class="bar-fill" style="width:{width:.1f}%;background:{color}"></span></span>
            <span class="regime-score">{pct:.0f}%</span>
            <span class="chev" id="{pid}-chev">&#9656;</span>
          </button>
          <div class="regime-detail" id="{pid}">
            <div class="detail-def"><span class="dot" style="background:{color}"></span>{_esc(r['definition'])} {cov}</div>
            {detail}
          </div>
        </div>
        """)
    return f"""
    <section class="card">
      <h3 class="sec-title">Regime scores</h3>
      <p class="sec-sub">Click any regime to see which rules fired, which did not, and the contradictions.</p>
      <div class="regimes">{''.join(rows)}</div>
    </section>
    """


def _yield_table(maturities: List[dict]) -> str:
    rows = []
    for m in maturities:
        c1, c5, c20 = m.get("change_1d_bps"), m.get("change_5d_bps"), m.get("change_20d_bps")
        z = m.get("zscore_1d")
        rows.append(
            f'<tr><td class="mat">{_esc(m["maturity"])}</td>'
            f'<td class="num">{fmt_yield(m.get("yield"))}</td>'
            f'<td class="num {sign_class(c1)}">{fmt_bps(c1)}</td>'
            f'<td class="num {sign_class(c5)}">{fmt_bps(c5)}</td>'
            f'<td class="num {sign_class(c20)}">{fmt_bps(c20)}</td>'
            f'<td class="num {sign_class(z)}">{fmt_z(z)}</td></tr>'
        )
    return f"""
    <section class="card">
      <h3 class="sec-title">Yield curve table</h3>
      <div class="tbl-scroll"><table class="tbl">
        <thead><tr><th>Maturity</th><th class="num">Yield</th><th class="num">1D bps</th>
        <th class="num">5D bps</th><th class="num">20D bps</th><th class="num">Z (1D)</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table></div>
    </section>
    """


def _spread_table(spreads: List[dict]) -> str:
    rows = []
    for s in spreads:
        c1, c5, c20 = s.get("change_1d_bps"), s.get("change_5d_bps"), s.get("change_20d_bps")
        rows.append(
            f'<tr><td class="mat">{_esc(s["spread"])}</td>'
            f'<td class="num">{fmt_level_bps(s.get("level_bps"))}</td>'
            f'<td class="num {sign_class(c1)}">{fmt_bps(c1)}</td>'
            f'<td class="num {sign_class(c5)}">{fmt_bps(c5)}</td>'
            f'<td class="num {sign_class(c20)}">{fmt_bps(c20)}</td>'
            f'<td class="interp">{_esc(s.get("interpretation"))}</td></tr>'
        )
    return f"""
    <section class="card">
      <h3 class="sec-title">Curve spreads</h3>
      <div class="tbl-scroll"><table class="tbl">
        <thead><tr><th>Spread</th><th class="num">Level</th><th class="num">1D</th>
        <th class="num">5D</th><th class="num">20D</th><th>Interpretation</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table></div>
    </section>
    """


def _chart_section() -> str:
    return """
    <section class="card">
      <h3 class="sec-title">Curve chart</h3>
      <p class="sec-sub">Par yields today versus 1, 5 and 20 business days ago.</p>
      <div id="curveChart" class="chart"></div>
    </section>
    """


def _segment_section(segments: dict) -> str:
    order = [("front", "Front-end"), ("belly", "Belly"), ("long", "Long-end")]
    cards = []
    for key, label in order:
        seg = segments.get(key, {})
        members = ", ".join(seg.get("members", [])) or DASH
        contrib = seg.get("contribution_pct")
        contrib_str = DASH if contrib is None else f"{contrib:+.0f}%"
        cards.append(f"""
        <div class="seg">
          <div class="seg-h">{label}</div>
          <div class="seg-members">{_esc(members)}</div>
          <div class="seg-grid">
            <div><span class="{sign_class(seg.get('avg_1d'))}">{fmt_bps(seg.get('avg_1d'))}</span><em>1D</em></div>
            <div><span class="{sign_class(seg.get('avg_5d'))}">{fmt_bps(seg.get('avg_5d'))}</span><em>5D</em></div>
            <div><span class="{sign_class(seg.get('avg_20d'))}">{fmt_bps(seg.get('avg_20d'))}</span><em>20D</em></div>
            <div><span>{contrib_str}</span><em>contribution</em></div>
          </div>
          <div class="seg-interp">{_esc(seg.get('interpretation'))}</div>
        </div>
        """)
    return f"""
    <section class="card">
      <h3 class="sec-title">Segment analysis</h3>
      <div class="seg-row">{''.join(cards)}</div>
    </section>
    """


def _macro_section(bullets: List[str]) -> str:
    items = "".join(f"<li>{_esc(b)}</li>" for b in bullets)
    return f"""
    <section class="card">
      <h3 class="sec-title">Macro interpretation</h3>
      <ul class="macro">{items}</ul>
    </section>
    """


def _history_section(history_df: Optional[pd.DataFrame]) -> str:
    if history_df is None or history_df.empty:
        return ""
    df = history_df.tail(10).iloc[::-1]

    def g(row, col):
        return row[col] if col in df.columns else None

    rows = []
    for _, row in df.iterrows():
        rows.append(
            f'<tr><td>{_esc(g(row, "data_date"))}</td>'
            f'<td>{_esc(g(row, "primary_regime"))}</td>'
            f'<td class="num">{fmt_pct(g(row, "primary_score"))}</td>'
            f'<td class="num">{fmt_level_bps(g(row, "2s10s"))}</td>'
            f'<td class="num">{fmt_level_bps(g(row, "5s30s"))}</td>'
            f'<td class="num">{fmt_yield(g(row, "10y_yield"))}</td>'
            f'<td class="num {sign_class(g(row, "front_end_avg_1d"))}">{fmt_bps(g(row, "front_end_avg_1d"))}</td>'
            f'<td class="num {sign_class(g(row, "long_end_avg_1d"))}">{fmt_bps(g(row, "long_end_avg_1d"))}</td></tr>'
        )
    return f"""
    <section class="card">
      <h3 class="sec-title">Historical context</h3>
      <p class="sec-sub">Last {len(df)} saved observations.</p>
      <div class="tbl-scroll"><table class="tbl">
        <thead><tr><th>Date</th><th>Primary regime</th><th class="num">Conf.</th>
        <th class="num">2s10s</th><th class="num">5s30s</th><th class="num">10Y</th>
        <th class="num">Front 1D</th><th class="num">Long 1D</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table></div>
    </section>
    """


# --------------------------------------------------------------------------- #
# CSS / JS (plain strings: keep braces away from f-strings)
# --------------------------------------------------------------------------- #
_CSS = """
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2330;--bd:#2a313c;--tx:#e6edf3;
--mut:#8b949e;--up:#f0635f;--down:#3fb950;--flat:#8b949e;--accent:#58a6ff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
line-height:1.5;font-size:15px;}
.wrap{max-width:1040px;margin:0 auto;padding:28px 20px 60px;}
header.top{margin-bottom:22px;}
.title{font-size:26px;font-weight:700;letter-spacing:-.01em;margin:0;}
.subtitle{color:var(--mut);font-size:13.5px;margin-top:6px;}
.subtitle b{color:var(--tx);font-weight:600;}
.banner{background:#3d2c00;border:1px solid #8a6d1a;color:#f2d57e;padding:11px 14px;
border-radius:10px;margin-bottom:18px;font-size:13.5px;}
.warns{margin:0 0 18px;font-size:13px;color:var(--mut);}
.warns summary{cursor:pointer;}
.warns ul{margin:8px 0 0 18px;}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;
padding:20px 22px;margin-bottom:18px;}
.sec-title{margin:0 0 2px;font-size:17px;font-weight:650;}
.sec-sub{margin:0 0 16px;color:var(--mut);font-size:13px;}
/* exec */
.exec-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;}
.exec-eyebrow{text-transform:uppercase;letter-spacing:.08em;font-size:11.5px;color:var(--mut);}
.exec-regime{margin:4px 0 0;font-size:25px;font-weight:720;letter-spacing:-.01em;}
.exec-score{text-align:right;flex-shrink:0;}
.score-num{font-size:40px;font-weight:760;line-height:1;}
.score-num span{font-size:20px;color:var(--mut);margin-left:2px;}
.score-pill{display:inline-block;margin-top:8px;padding:4px 10px;border-radius:999px;
font-size:12px;font-weight:600;border:1px solid;}
.exec-summary{margin:16px 0 18px;font-size:15px;color:#cdd6e0;}
.kpi-row{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;}
.kpi{background:var(--card2);border:1px solid var(--bd);border-radius:10px;padding:10px 12px;}
.kpi-val{font-size:16px;font-weight:660;font-variant-numeric:tabular-nums;}
.kpi-lbl{font-size:11px;color:var(--mut);margin-top:3px;}
/* regimes */
.regime{border-bottom:1px solid var(--bd);}
.regime:last-child{border-bottom:none;}
.regime-bar{width:100%;background:none;border:none;color:var(--tx);cursor:pointer;
display:grid;grid-template-columns:210px 1fr 52px 16px;align-items:center;gap:14px;
padding:11px 2px;text-align:left;font-size:14px;}
.regime-bar:hover{background:#1b2230;}
.regime-name{font-weight:560;}
.bar-wrap{background:#222a36;border-radius:6px;height:11px;overflow:hidden;}
.bar-fill{display:block;height:100%;border-radius:6px;}
.regime-score{text-align:right;font-variant-numeric:tabular-nums;font-weight:640;}
.chev{transition:transform .15s ease;color:var(--mut);font-size:13px;}
.chev.open{transform:rotate(90deg);}
.regime-detail{display:none;padding:6px 4px 18px 4px;}
.regime-detail.open{display:block;}
.detail-def{font-size:13.5px;color:#c2ccd6;margin-bottom:14px;
background:var(--card2);border:1px solid var(--bd);border-radius:10px;padding:11px 13px;}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;vertical-align:middle;}
.cov{color:var(--mut);font-size:12px;margin-left:8px;}
.rule-group{margin-bottom:12px;}
.rule-group-h{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 7px;}
.rule{border-left:3px solid var(--bd);background:var(--card2);border-radius:0 8px 8px 0;
padding:8px 12px;margin-bottom:7px;}
.rule.ok{border-left-color:var(--down);}
.rule.no{border-left-color:#3a4350;}
.rule.miss{border-left-color:#d29922;}
.rule.contra{border-left-color:var(--up);}
.rule-top{display:flex;justify-content:space-between;gap:10px;}
.rule-name{font-size:13.5px;font-weight:550;}
.rule-w{color:var(--mut);font-size:11.5px;flex-shrink:0;}
.rule-meta{font-size:12px;color:var(--mut);margin-top:3px;font-variant-numeric:tabular-nums;}
.rule-meta b{color:var(--tx);}
.rule-exp{font-size:12.5px;color:#aeb8c4;margin-top:5px;}
.rule-empty{font-size:13px;color:var(--mut);font-style:italic;}
/* tables */
.tbl-scroll{overflow-x:auto;}
.tbl{width:100%;border-collapse:collapse;font-size:13.5px;}
.tbl th,.tbl td{padding:8px 10px;border-bottom:1px solid var(--bd);text-align:left;white-space:nowrap;}
.tbl th{color:var(--mut);font-weight:560;font-size:12px;text-transform:uppercase;letter-spacing:.03em;}
.tbl td.mat{font-weight:620;}
.tbl .num{text-align:right;font-variant-numeric:tabular-nums;}
.tbl .interp{color:var(--mut);font-size:12.5px;white-space:normal;min-width:240px;}
.up{color:var(--up);}.down{color:var(--down);}.flat{color:var(--flat);}.muted{color:var(--mut);}
/* chart */
.chart{width:100%;height:380px;}
/* segments */
.seg-row{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;}
.seg{background:var(--card2);border:1px solid var(--bd);border-radius:12px;padding:14px 15px;}
.seg-h{font-weight:650;font-size:15px;}
.seg-members{color:var(--mut);font-size:12px;margin:3px 0 12px;}
.seg-grid{display:grid;grid-template-columns:1fr 1fr;gap:9px;}
.seg-grid>div{display:flex;flex-direction:column;}
.seg-grid span{font-size:15px;font-weight:640;font-variant-numeric:tabular-nums;}
.seg-grid em{font-size:11px;color:var(--mut);font-style:normal;margin-top:1px;}
.seg-interp{font-size:12.5px;color:#aeb8c4;margin-top:12px;}
/* macro */
.macro{margin:0;padding-left:20px;}
.macro li{margin-bottom:9px;font-size:14px;color:#cdd6e0;}
footer{color:var(--mut);font-size:12px;margin-top:26px;text-align:center;line-height:1.7;}
footer .disc{color:#7d8590;max-width:680px;margin:0 auto;}
@media(max-width:820px){
 .kpi-row{grid-template-columns:repeat(3,1fr);}
 .seg-row{grid-template-columns:1fr;}
 .regime-bar{grid-template-columns:130px 1fr 44px 14px;gap:8px;font-size:13px;}
 .exec-regime{font-size:21px;}
}
"""

_JS = """
function toggleDetail(id){
  var p=document.getElementById(id);
  var c=document.getElementById(id+'-chev');
  var open=p.classList.toggle('open');
  if(c)c.classList.toggle('open',open);
  var btn=p.previousElementSibling;
  if(btn)btn.setAttribute('aria-expanded',open?'true':'false');
}
(function(){
  var data=__CHART_JSON__;
  var el=document.getElementById('curveChart');
  if(!el||!window.Plotly||!data||!data.labels){if(el)el.innerHTML='<div style=\"color:#8b949e;font-size:13px\">Chart unavailable.</div>';return;}
  var defs=[['today','Today','#58a6ff',3],['d1','1D ago','#a371f7',1.5],
            ['d5','5D ago','#d29922',1.5],['d20','20D ago','#8b949e',1.5]];
  var traces=[];
  defs.forEach(function(d){
    var s=data.series[d[0]];
    if(!s||!s.values)return;
    traces.push({x:data.labels,y:s.values,mode:'lines+markers',name:d[1]+(s.date?' ('+s.date+')':''),
      line:{color:d[2],width:d[3]},marker:{size:5},connectgaps:true});
  });
  var layout={paper_bgcolor:'rgba(0,0,0,0)',plot_bgcolor:'rgba(0,0,0,0)',
    font:{color:'#e6edf3',size:12},margin:{l:48,r:16,t:10,b:40},
    xaxis:{type:'category',gridcolor:'#222a36',title:{text:'Maturity'}},
    yaxis:{gridcolor:'#222a36',ticksuffix:'%',title:{text:'Yield'}},
    legend:{orientation:'h',y:-0.2},hovermode:'x unified'};
  Plotly.newPlot(el,traces,layout,{displayModeBar:false,responsive:true});
})();
"""


# --------------------------------------------------------------------------- #
# Top-level render
# --------------------------------------------------------------------------- #
def render(report: dict, history_df: Optional[pd.DataFrame], demo: bool,
           source: str, warnings: List[str]) -> str:
    run_dt = datetime.now(timezone.utc)
    chart_json = json.dumps(report.get("chart", {}))
    js = _JS.replace("__CHART_JSON__", chart_json)

    body = "".join([
        _exec_section(report),
        _regime_section(report),
        _yield_table(report.get("maturities", [])),
        _spread_table(report.get("spreads", [])),
        _chart_section(),
        _segment_section(report.get("segments", {})),
        _macro_section(report.get("macro_bullets", [])),
        _history_section(history_df),
    ])

    head = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Yield Curve Monitor &middot; {_esc(report.get('data_date'))}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>{_CSS}</style>
</head><body><div class="wrap">
<header class="top">
  <h1 class="title">Yield Curve Monitor</h1>
  <div class="subtitle">Data as of <b>{_esc(report.get('data_date'))}</b>
   &nbsp;&middot;&nbsp; Generated {run_dt.strftime('%Y-%m-%d %H:%M')} UTC
   &nbsp;&middot;&nbsp; Source: {_esc(source)}</div>
</header>
{_banner(demo, source)}
{_warnings_block(warnings)}
"""

    footer = """
<footer>
  <div class="disc">Rule-based, interpretable classification of U.S. Treasury par-yield curve
  moves. Methodology is heuristic and meant as an analytical aid, not a forecast.
  <strong>This is not investment advice.</strong></div>
</footer>
</div>
<script>""" + js + """</script>
</body></html>"""

    return head + body + footer
