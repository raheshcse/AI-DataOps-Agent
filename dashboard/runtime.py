"""Builds the API clients for the Streamlit apps. Tests inject clients with :func:`set_override`; production reads
the environment. Tokens are read here and handed straight to the client; they are never put in Streamlit state."""

from __future__ import annotations

from typing import Any

import streamlit as st

_overrides: dict[str, Any] = {}


def set_override(kind: str, client: Any | None) -> None:
    if client is None:
        _overrides.pop(kind, None)
    else:
        _overrides[kind] = client


@st.cache_resource(show_spinner=False)
def _ops_from_env():
    from dashboard.ops_client import from_environment

    return from_environment()


@st.cache_resource(show_spinner=False)
def _ledger_from_env():
    from dashboard.ledger_client import from_environment

    return from_environment()


def ops_client():
    return _overrides["ops"] if "ops" in _overrides else _ops_from_env()


def ledger_client():
    return _overrides["ledger"] if "ledger" in _overrides else _ledger_from_env()
