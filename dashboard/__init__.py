"""Phase 10 user interfaces: the Operations Dashboard and the separate VerbaLedger Explorer.

Both are Streamlit apps that talk ONLY to the authenticated FastAPI ``/api/v1`` surface over HTTP. Neither imports the
database, ledger, recovery or governance packages (a test enforces it), so neither can write to SQLite, the ledger or
SQL Server, or bypass the Phase 7 approval workflow.
"""
