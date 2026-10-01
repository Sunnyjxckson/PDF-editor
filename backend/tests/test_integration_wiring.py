"""Integration wiring: the AI router is mounted by main.py exactly once."""
from collections import Counter

from backend.features.ai import router as ai_router
from backend.main import app


def _keys(routes):
    return [(r.path, tuple(sorted(getattr(r, "methods", None) or ()))) for r in routes]


def test_every_ai_route_is_mounted_exactly_once():
    mounted = Counter(_keys(app.router.routes))
    wanted = _keys(ai_router.routes)
    assert wanted, "AI router has no routes"
    assert {k: mounted.get(k, 0) for k in wanted} == {k: 1 for k in wanted}


def test_no_route_is_registered_twice():
    dupes = [k for k, n in Counter(_keys(app.router.routes)).items() if n > 1]
    assert dupes == []
