"""spotrun -- Burst compute to AWS spot instances."""

from spotrun.exceptions import SpotCapacityError
from spotrun.pricing import select_instance, estimate_cost
from spotrun.session import Session

__all__ = ["Session", "SpotCapacityError", "select_instance", "estimate_cost"]
