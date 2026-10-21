"""Plotly figures for the VerbaLedger Explorer (separate from the Operations Dashboard's charts on purpose: the two apps
share no view code). They return ``None`` for empty data so the UI can show an empty state."""

from __future__ import annotations

import plotly.graph_objects as go


def events_over_time_chart(pairs: list[tuple[str, int]]) -> go.Figure | None:
    if not pairs:
        return None
    fig = go.Figure(go.Bar(x=[p[0] for p in pairs], y=[p[1] for p in pairs]))
    fig.update_layout(title="Recorded ledger events per day (UTC)", margin={"l": 10, "r": 10, "t": 40, "b": 10}, height=300,
                      xaxis_title="Day", yaxis_title="Events")
    fig.update_xaxes(type="category")
    return fig


def entry_type_chart(counts: dict[str, int]) -> go.Figure | None:
    if not counts:
        return None
    pairs = sorted(counts.items())
    fig = go.Figure(go.Bar(x=[p[1] for p in pairs], y=[p[0] for p in pairs], orientation="h", text=[p[1] for p in pairs], textposition="auto"))
    fig.update_layout(title="Events by entry type", margin={"l": 10, "r": 10, "t": 40, "b": 10}, height=300, showlegend=False)
    fig.update_yaxes(autorange="reversed")
    return fig
