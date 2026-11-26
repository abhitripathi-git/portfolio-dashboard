"""Modular Streamlit UI components for the family portfolio dashboard."""

from ui.sidebar import render_sidebar
from ui.tab_alt_assets import render_alt_assets_tab
from ui.tab_benchmark import render_benchmark_tab
from ui.tab_gemini import render_gemini_tab
from ui.tab_holdings import render_holdings_tab
from ui.tab_overview import render_overview_tab
from ui.tab_upload import render_upload_tab

__all__ = [
    "render_sidebar",
    "render_overview_tab",
    "render_benchmark_tab",
    "render_holdings_tab",
    "render_alt_assets_tab",
    "render_upload_tab",
    "render_gemini_tab",
]
