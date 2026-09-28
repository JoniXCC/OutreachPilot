"""Altair chart builders for the dashboard.

Conventions: magnitude charts use a single hue; categorical series use the
validated palette in fixed order (never cycled); every mark has a tooltip;
thin bars with 4px rounded data-ends; recessive grid; one axis per chart.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # validated categorical slots 1-3
PRIMARY = SERIES[0]
LABEL_GRAY = "#8a8a8a"  # readable on both light and dark surfaces


def _style(chart: alt.Chart, height: int) -> alt.Chart:
    # Axis/grid colours come from Streamlit's theme so light and dark mode both work.
    return (chart.properties(height=height)
            .configure_view(strokeWidth=0)
            .configure_axis(labelFontSize=12, titleFontSize=12, labelLimit=180)
            .configure_axisY(labelOverlap=False)
            .configure_legend(orient="top"))


def hbar(data: dict[str, int], label: str, value: str = "Count", sort_desc: bool = True,
         order: list[str] | None = None) -> alt.Chart | None:
    """Horizontal bar chart of one measure (single hue)."""
    if not data:
        return None
    df = pd.DataFrame({label: list(data.keys()), value: list(data.values())})
    sort = order if order else ("-x" if sort_desc else None)
    chart = alt.Chart(df).mark_bar(color=PRIMARY, cornerRadiusEnd=4, height={"band": 0.62}).encode(
        y=alt.Y(f"{label}:N", sort=sort, title=None),
        x=alt.X(f"{value}:Q", title=value, axis=alt.Axis(tickMinStep=1, format="d"),
                scale=alt.Scale(domain=[0, max(df[value]) * 1.12 + 0.3], nice=False)),
        tooltip=[alt.Tooltip(f"{label}:N"), alt.Tooltip(f"{value}:Q", format="d")],
    )
    text = alt.Chart(df).mark_text(align="left", dx=4, color=LABEL_GRAY, fontSize=12).encode(
        y=alt.Y(f"{label}:N", sort=sort), x=alt.X(f"{value}:Q"), text=alt.Text(f"{value}:Q", format="d"))
    return _style(chart + text, height=max(90, 32 * len(df)))


def daily_bars(df: pd.DataFrame, date_col: str, value_col: str, title: str) -> alt.Chart | None:
    """Emails per day (single series, time on x)."""
    if df.empty:
        return None
    chart = alt.Chart(df).mark_bar(color=PRIMARY, cornerRadiusEnd=4, width={"band": 0.6}).encode(
        x=alt.X(f"{date_col}:T", title=None, axis=alt.Axis(format="%d %b", labelAngle=0)),
        y=alt.Y(f"{value_col}:Q", title=title, axis=alt.Axis(tickMinStep=1, format="d")),
        tooltip=[alt.Tooltip(f"{date_col}:T", format="%a %d %b %Y"), alt.Tooltip(f"{value_col}:Q", format="d")],
    )
    return _style(chart, height=220)


def stacked_hbar(df: pd.DataFrame, category: str, series: str, value: str,
                 series_order: list[str]) -> alt.Chart | None:
    """Two-or-three-series stacked bars (e.g. live vs cached AI calls) with a legend."""
    if df.empty:
        return None
    data = df.groupby([category, series], as_index=False)[value].sum()
    categories = data.groupby(category)[value].sum().sort_values(ascending=False).index.tolist()
    colors = SERIES[: len(series_order)]
    chart = alt.Chart(data).mark_bar(cornerRadiusEnd=4, height={"band": 0.62}).encode(
        y=alt.Y(f"{category}:N", title=None, sort=categories),
        x=alt.X(f"{value}:Q", title=value, stack="zero", axis=alt.Axis(tickMinStep=1, format="d")),
        color=alt.Color(f"{series}:N", scale=alt.Scale(domain=series_order, range=colors), title=None),
        order=alt.Order(f"{series}:N", sort="descending"),
        tooltip=[alt.Tooltip(f"{category}:N"), alt.Tooltip(f"{series}:N"), alt.Tooltip(f"{value}:Q", format="d")],
    )
    totals = data.groupby(category, as_index=False)[value].sum()
    labels = alt.Chart(totals).mark_text(align="left", dx=4, color=LABEL_GRAY, fontSize=12).encode(
        y=alt.Y(f"{category}:N", sort=categories), x=alt.X(f"{value}:Q"),
        text=alt.Text(f"{value}:Q", format="d"))
    return _style(chart + labels, height=max(110, 40 * len(categories)))
