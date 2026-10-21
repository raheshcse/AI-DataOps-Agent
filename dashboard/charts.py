"""Plotly figures built from view-model data. They return ``None`` for empty data so the UI can show an empty state."""

from __future__ import annotations

import plotly.graph_objects as go

SEVERITY_COLOURS = {"critical": "#b91c1c", "high": "#ea580c", "medium": "#ca8a04", "low": "#2563eb"}


def _bar(pairs: list[tuple[str, int]], title: str, colours: dict[str, str] | None = None, horizontal: bool = False) -> go.Figure | None:
    if not pairs:
        return None
    labels, values = [p[0] for p in pairs], [p[1] for p in pairs]
    marker = {"color": [colours.get(k, "#6b7280") for k in labels]} if colours else {}
    fig = go.Figure(go.Bar(x=values if horizontal else labels, y=labels if horizontal else values,
                           orientation="h" if horizontal else "v", marker=marker, text=values, textposition="auto"))
    fig.update_layout(title=title, margin={"l": 10, "r": 10, "t": 40, "b": 10}, height=320, showlegend=False)
    if horizontal:
        fig.update_yaxes(autorange="reversed")
    return fig


def severity_chart(pairs: list[tuple[str, int]]) -> go.Figure | None:
    return _bar(pairs, "Incidents by severity", SEVERITY_COLOURS)


def workflow_chart(pairs: list[tuple[str, int]]) -> go.Figure | None:
    return _bar(pairs, "Incidents by workflow status", horizontal=True)


def timeline_chart(pairs: list[tuple[str, int]]) -> go.Figure | None:
    if not pairs:
        return None
    fig = go.Figure(go.Bar(x=[p[0] for p in pairs], y=[p[1] for p in pairs]))
    fig.update_layout(title="Incidents created per day (loaded incidents only)", margin={"l": 10, "r": 10, "t": 40, "b": 10},
                      height=320, xaxis_title="Day", yaxis_title="Incidents")
    return fig
