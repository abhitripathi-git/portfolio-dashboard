"""Formatting, styling, extra_data flattening, and Plotly layout helpers."""

from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

COLOR_PALETTE = [
    "#3B6FD8",  # Muted Sapphire
    "#2B8A78",  # Sage Teal
    "#6C63AC",  # Soft Indigo
    "#C4843D",  # Warm Ochre
    "#4F86B8",  # Steel Blue
    "#9B5F8A",  # Dusty Plum
    "#5C8E6B",  # Forest Sage
    "#B86464",  # Muted Rose
]

GAIN_COLOR = "#16805C"
LOSS_COLOR = "#C2414B"
NEUTRAL_COLOR = "#64748B"


def inject_custom_css() -> None:
    """Inject modern, theme-adaptive styling for uniform KPI grids, cards, tabs, and layout."""
    css = """<style>
html, body, [class*="css"] {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
}
[data-testid="stHeader"] {
    background: transparent;
}
.block-container {
    padding-top: 3.85rem;
    padding-bottom: 3rem;
    max-width: 1460px;
}
/* Uniform horizontal alignment across all Streamlit column rows & metrics */
[data-testid="stHorizontalBlock"] {
    align-items: stretch;
}
[data-testid="stMetric"] {
    height: 100%;
    min-height: 108px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    background: linear-gradient(180deg, rgba(148, 163, 184, 0.08) 0%, rgba(148, 163, 184, 0.03) 100%);
    border: 1px solid rgba(148, 163, 184, 0.22);
    border-radius: 14px;
    padding: 0.9rem 1.05rem;
    box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
    transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
}
[data-testid="stMetric"]:hover {
    transform: translateY(-1px);
    border-color: rgba(37, 99, 235, 0.38);
    box-shadow: 0 6px 18px rgba(37, 99, 235, 0.08);
}
[data-testid="stMetricLabel"] {
    font-size: 0.76rem;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    opacity: 0.72;
}
[data-testid="stMetricValue"] {
    font-size: clamp(1.12rem, 1.45vw, 1.44rem);
    font-weight: 750;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
/* Hero header bar */
.hero-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 0.85rem;
    padding: 1.05rem 1.35rem;
    margin-bottom: 1.1rem;
    border-radius: 16px;
    background: linear-gradient(135deg, rgba(148, 163, 184, 0.08) 0%, rgba(59, 111, 216, 0.05) 100%);
    border: 1px solid rgba(148, 163, 184, 0.22);
    box-shadow: 0 2px 12px rgba(15, 23, 42, 0.04);
}
.hero-title {
    font-size: 1.42rem;
    font-weight: 720;
    letter-spacing: -0.02em;
    line-height: 1.22;
}
.hero-sub {
    font-size: 0.82rem;
    opacity: 0.72;
    margin-top: 0.22rem;
}
.hero-badges {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.45rem;
}
.owner-pill, .live-pill {
    display: inline-flex;
    align-items: center;
    padding: 0.28rem 0.78rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
}
.owner-pill {
    background: rgba(59, 111, 216, 0.11);
    color: #3B6FD8;
    border: 1px solid rgba(59, 111, 216, 0.24);
}
.live-pill {
    background: rgba(22, 128, 92, 0.11);
    color: #16805C;
    border: 1px solid rgba(22, 128, 92, 0.24);
}
/* Symmetrical 5-card KPI Grid on Overview */
.kpi-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 0.85rem;
    margin-bottom: 1.25rem;
    align-items: stretch;
}
@media (max-width: 1180px) {
    .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
    .kpi-grid { grid-template-columns: repeat(1, minmax(0, 1fr)); }
}
.kpi-card {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    min-height: 116px;
    padding: 1rem 1.1rem;
    border-radius: 14px;
    background: linear-gradient(180deg, rgba(148, 163, 184, 0.09) 0%, rgba(148, 163, 184, 0.03) 100%);
    border: 1px solid rgba(148, 163, 184, 0.24);
    box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
    transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
}
.kpi-card:hover {
    transform: translateY(-2px);
    border-color: rgba(37, 99, 235, 0.42);
    box-shadow: 0 8px 20px rgba(37, 99, 235, 0.09);
}
.kpi-label {
    font-size: 0.74rem;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    opacity: 0.70;
    margin-bottom: 0.45rem;
}
.kpi-value {
    font-size: clamp(1.12rem, 1.45vw, 1.46rem);
    font-weight: 760;
    letter-spacing: -0.025em;
    font-variant-numeric: tabular-nums;
    line-height: 1.18;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.kpi-sub {
    margin-top: 0.52rem;
    display: flex;
    align-items: center;
    gap: 0.35rem;
}
.kpi-pill {
    display: inline-flex;
    align-items: center;
    padding: 0.16rem 0.52rem;
    border-radius: 999px;
    font-size: 0.74rem;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.kpi-pill.pos {
    background: rgba(22, 128, 92, 0.11);
    color: #16805C;
}
.kpi-pill.neg {
    background: rgba(194, 65, 75, 0.10);
    color: #C2414B;
}
.kpi-pill.neu {
    background: rgba(148, 163, 184, 0.15);
    color: inherit;
    opacity: 0.85;
}
/* Tech & System strip */
.tech-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.75rem;
    margin-top: 0.4rem;
}
@media (max-width: 960px) {
    .tech-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.tech-tile {
    padding: 0.75rem 0.95rem;
    border-radius: 12px;
    background: rgba(148, 163, 184, 0.07);
    border: 1px solid rgba(148, 163, 184, 0.20);
}
.tech-tile-label {
    font-size: 0.7rem;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    opacity: 0.68;
}
.tech-tile-val {
    font-size: 0.9rem;
    font-weight: 650;
    margin-top: 0.2rem;
}
.tech-tile-sub {
    font-size: 0.75rem;
    opacity: 0.72;
    margin-top: 0.15rem;
}
/* Modern Segmented Pill Tabs — evenly spaced across full width */
[data-baseweb="tab-list"] {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.38rem;
    margin-bottom: 1.15rem;
    background: rgba(148, 163, 184, 0.07);
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 13px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.18);
}
[data-baseweb="tab-highlight"] {
    display: none !important;
}
[data-baseweb="tab-border"] {
    display: none !important;
}
[data-baseweb="tab"] {
    flex: 1 1 0;
    min-width: max-content;
    justify-content: center;
    border-radius: 9px;
    padding: 0.54rem 1.05rem;
    font-weight: 600;
    font-size: 0.87rem;
    letter-spacing: -0.01em;
    transition: background 0.15s ease, color 0.15s ease;
}
[data-baseweb="tab"][aria-selected="true"] {
    background: rgba(59, 111, 216, 0.12);
    color: #3B6FD8;
    box-shadow: 0 1px 4px rgba(15, 23, 42, 0.06);
}
/* Segmented Pill Top Navigation Bar */
[data-testid="stSegmentedControl"] {
    width: 100%;
    margin-bottom: 0.9rem;
}
[data-testid="stSegmentedControl"] > div {
    width: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    padding: 0.32rem;
    background: rgba(148, 163, 184, 0.07);
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 13px;
}
[data-testid="stSegmentedControl"] button {
    flex: 1 1 0;
    min-width: max-content;
    justify-content: center;
    border-radius: 9px !important;
    font-weight: 600 !important;
    font-size: 0.87rem !important;
    padding: 0.5rem 0.95rem !important;
}
/* Rounded DataFrames, Expanders, Inputs, Buttons, ChatInput & Sidebar */
[data-testid="stDataFrame"] {
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 12px;
    overflow: hidden;
}
[data-testid="stExpander"] {
    border: 1px solid rgba(148, 163, 184, 0.16) !important;
    border-radius: 12px !important;
    background: rgba(148, 163, 184, 0.025);
}
[data-baseweb="select"] > div, [data-baseweb="input"] > div {
    border-radius: 10px !important;
    border-color: rgba(148, 163, 184, 0.22) !important;
}
.stButton > button, .stDownloadButton > button {
    border-radius: 10px;
    font-weight: 550;
    border-color: rgba(148, 163, 184, 0.24);
    transition: all 0.15s ease;
}
.stButton > button[kind="primary"] {
    background-color: #3B6FD8 !important;
    border-color: #3B6FD8 !important;
    color: #FFFFFF !important;
    box-shadow: 0 1px 4px rgba(59, 111, 216, 0.22);
}
.stButton > button[kind="primary"]:hover {
    background-color: #315EC0 !important;
    border-color: #315EC0 !important;
}
[data-testid="stChatInput"] > div {
    border-radius: 14px;
    border: 1px solid rgba(148, 163, 184, 0.35);
    padding: 0.35rem 0.5rem;
}
[data-testid="stChatInput"] textarea {
    min-height: 74px !important;
    font-size: 0.98rem !important;
    line-height: 1.45 !important;
    padding-top: 0.55rem !important;
}
.status-badge {
    display: inline-block;
    padding: 0.16rem 0.55rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 650;
    line-height: 1.4;
}
.badge-high {
    background: rgba(22, 128, 92, 0.12);
    color: #16805C;
    border: 1px solid rgba(22, 128, 92, 0.28);
}
.badge-medium {
    background: rgba(196, 132, 61, 0.12);
    color: #B0722C;
    border: 1px solid rgba(196, 132, 61, 0.28);
}
.badge-none {
    background: rgba(194, 65, 75, 0.11);
    color: #C2414B;
    border: 1px solid rgba(194, 65, 75, 0.28);
}
</style>"""
    st.markdown(css, unsafe_allow_html=True)


def format_inr_compact(value: float | int | None) -> str:
    """Format INR compactly in Cr / L for sub-labels (e.g., '₹10.99 Cr')."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(num) or math.isinf(num):
        return "—"
    sign = "-" if num < 0 else ""
    a = abs(num)
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:.2f} L"
    return f"{sign}₹{format_indian_number(a, decimals=0)}"


def format_indian_number(value: float | int | None, decimals: int = 0) -> str:
    """Format a number using Indian lakh/crore comma grouping."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(num) or math.isinf(num):
        return "—"

    sign = "-" if num < 0 else ""
    num = abs(num)
    formatted = f"{num:.{decimals}f}"
    if "." in formatted:
        int_part, dec_part = formatted.split(".", 1)
        dec_str = f".{dec_part}"
    else:
        int_part, dec_str = formatted, ""

    if len(int_part) <= 3:
        grouped = int_part
    else:
        last_three = int_part[-3:]
        rest = int_part[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.append(rest)
        grouped = ",".join(reversed(groups)) + "," + last_three

    return f"{sign}{grouped}{dec_str}"


def format_inr(value: float | int | None, decimals: int = 0, signed: bool = False) -> str:
    """Format a numeric value in INR (₹) with Indian comma grouping."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(num) or math.isinf(num):
        return "—"
    prefix = "+" if signed and num > 0 else ("-" if num < 0 else "")
    return f"{prefix}₹{format_indian_number(abs(num), decimals=decimals)}"


def format_pct(value: float | int | None, decimals: int = 2, signed: bool = True) -> str:
    """Format a percentage value (already in 0-100 scale)."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(num) or math.isinf(num):
        return "—"
    prefix = "+" if signed and num > 0 else ""
    return f"{prefix}{num:.{decimals}f}%"


def format_xirr(xirr_decimal: float | None) -> str:
    """Format decimal XIRR (e.g. 0.154 -> '+15.40%')."""
    if xirr_decimal is None:
        return "N/A"
    return format_pct(float(xirr_decimal) * 100.0, decimals=2, signed=True)


def _format_extra_val(val: Any) -> Any:
    if val is None or val == "":
        return None
    if isinstance(val, dict):
        if len(val) > 4:
            return f"{len(val)} items"
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, list):
        return ", ".join(str(x) for x in val[:5]) + ("..." if len(val) > 5 else "")
    return val


def expand_extra_data_columns(
    rows: list[dict[str, Any]],
    reserved_keys: set[str] | None = None,
    skip_extra_keys: set[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Convert row dicts into a DataFrame and expand `extra_data` keys as clean columns.

    Returns `(df, extra_column_names)`.
    """
    if not rows:
        return pd.DataFrame(), []

    reserved = {k.lower() for k in (reserved_keys or set())}
    skip = {k.lower() for k in (skip_extra_keys or set())}

    extra_keys_ordered: list[str] = []
    seen_extra: set[str] = set()
    for row in rows:
        ed = row.get("extra_data")
        if isinstance(ed, dict):
            for k, v in ed.items():
                ks = str(k).strip()
                if not ks or ks.lower() in skip or v in (None, "", {}):
                    continue
                if ks not in seen_extra:
                    seen_extra.add(ks)
                    extra_keys_ordered.append(ks)

    normalized_rows: list[dict[str, Any]] = []
    extra_col_names: dict[str, str] = {}
    for k in extra_keys_ordered:
        col_name = k if k.lower() not in reserved else f"Extra: {k}"
        extra_col_names[k] = col_name

    for row in rows:
        base = {k: v for k, v in row.items() if k != "extra_data"}
        ed = row.get("extra_data") if isinstance(row.get("extra_data"), dict) else {}
        for k in extra_keys_ordered:
            base[extra_col_names[k]] = _format_extra_val(ed.get(k))
        normalized_rows.append(base)

    df = pd.DataFrame(normalized_rows)
    return df, [extra_col_names[k] for k in extra_keys_ordered]


def style_plotly_fig(fig: go.Figure, title: str = "", height: int = 380) -> go.Figure:
    """Apply consistent modern styling to Plotly figures."""
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, family="Inter, system-ui, sans-serif"), x=0.0, xanchor="left") if title else None,
        font=dict(family="Inter, system-ui, sans-serif", size=12),
        height=height,
        margin=dict(l=16, r=16, t=46 if title else 20, b=16),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        hovermode="x unified",
        hoverlabel=dict(font_size=12, font_family="Inter, system-ui, sans-serif"),
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(148, 163, 184, 0.10)", zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor="rgba(148, 163, 184, 0.10)", zeroline=False)
    return fig
