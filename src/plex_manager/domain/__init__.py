"""Domain core (pure, no I/O).

Home of the decision engine (parse → quality profile → blocklist → score), the
request/download state machine, the reconciler, and retention logic. This package
depends only on the ``ports`` interfaces, never on a concrete adapter, so it can
be unit-tested without the network. Implementations land in the v1-planning
session — see ``docs/design/overview.md``.
"""
