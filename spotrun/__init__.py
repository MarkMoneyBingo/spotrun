"""spotrun -- Burst compute to AWS spot instances."""

from spotrun.pricing import select_instance, estimate_cost
from spotrun.session import Session

__all__ = ["Session", "select_instance", "estimate_cost"]
