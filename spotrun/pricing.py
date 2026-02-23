"""Instance selection and cost estimation."""

# (instance_type, vcpus, arch)
COMPUTE_INSTANCES: list[tuple[str, int, str]] = [
    # 4 vCPU
    ("c6g.xlarge",     4, "arm64"),
    ("c6a.xlarge",     4, "x86_64"),
    # 8 vCPU
    ("c6g.2xlarge",    8, "arm64"),
    ("c6a.2xlarge",    8, "x86_64"),
    # 16 vCPU
    ("c6g.4xlarge",   16, "arm64"),
    ("c6a.4xlarge",   16, "x86_64"),
    # 32 vCPU
    ("c6g.8xlarge",   32, "arm64"),
    ("c7g.8xlarge",   32, "arm64"),
    ("c6a.8xlarge",   32, "x86_64"),
    # 48 vCPU
    ("c6g.12xlarge",  48, "arm64"),
    ("c6a.12xlarge",  48, "x86_64"),
    # 64 vCPU
    ("c6g.16xlarge",  64, "arm64"),
    ("c6a.16xlarge",  64, "x86_64"),
]

MAX_WORKERS_X86 = (64 - 1) // 2  # 31 (x86: 2 vCPUs per physical core)
MAX_WORKERS_ARM = 64 - 1          # 63 (ARM/Graviton: 1 vCPU = 1 physical core)


def _vcpus_needed(workers: int, arch: str) -> int:
    """Minimum vCPUs for a given worker count, accounting for architecture.

    x86: each physical core has 2 vCPUs (hyperthreading), so workers * 2 + 1.
    ARM/Graviton: each vCPU IS a physical core (no HT), so workers + 1.
    The +1 reserves one core for OS/SSH overhead.
    """
    if arch == "arm64":
        return workers + 1
    return workers * 2 + 1


def select_instance(
    workers: int,
    prices: dict[str, float] | None = None,
    include_arm: bool = False,
) -> tuple[str, int]:
    """Pick the cheapest instance with enough vCPUs for the requested workers.

    Args:
        workers: Number of parallel workers needed.
        prices: Optional dict of instance_type -> spot_price. When provided,
            selects the cheapest priced candidate.
        include_arm: If True, include ARM/Graviton instances. ARM instances
            are typically 20-40% cheaper but may have compatibility issues
            with some software. Default False (x86_64 only).

    Returns (instance_type, vcpus).
    """
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    max_workers = MAX_WORKERS_ARM if include_arm else MAX_WORKERS_X86
    if workers > max_workers:
        raise ValueError(
            f"Requested {workers} workers but max supported is {max_workers}."
        )

    candidates = [
        (it, vc) for it, vc, arch in COMPUTE_INSTANCES
        if vc >= _vcpus_needed(workers, arch) and (include_arm or arch == "x86_64")
    ]
    if not candidates:
        raise ValueError(f"No instance with enough vCPUs for {workers} workers.")

    if prices:
        priced = [(it, vc, prices[it]) for it, vc in candidates if it in prices]
        if priced:
            priced.sort(key=lambda x: x[2])
            return priced[0][0], priced[0][1]

    # Without prices, return the smallest candidate
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0], candidates[0][1]


def instance_arch(instance_type: str) -> str:
    """Return the architecture for an instance type."""
    for it, _, arch in COMPUTE_INSTANCES:
        if it == instance_type:
            return arch
    if any(fam in instance_type for fam in ("c6g", "c7g", "c8g", "m6g", "m7g")):
        return "arm64"
    return "x86_64"


def estimate_cost(spot_price_per_hour: float, minutes: float) -> float:
    """Estimate total cost given a spot price and duration in minutes."""
    return spot_price_per_hour * (minutes / 60.0)


def all_instance_types(include_arm: bool = False) -> list[str]:
    """Return instance type names for pricing queries."""
    return [
        itype for itype, _, arch in COMPUTE_INSTANCES
        if include_arm or arch == "x86_64"
    ]
