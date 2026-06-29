"""Web adapter — the FastAPI application, REST API, and (eventually) the UI.

This is an adapter, not part of the domain core: it translates HTTP requests into
calls on the core and renders the results. The setup wizard, settings, health
dashboard, console, and correction flows (see ADR-0005) live here.
"""
