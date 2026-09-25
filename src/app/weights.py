"""Default weight map for assessment checks."""

WEIGHTS: dict[str, float] = {
    "ac_quality":       1.5,
    "has_persona":      1.0,   # blocker — weight unused in quality calc but defined
    "value_statement":  1.0,
    "failure_handling": 1.0,
    "safe_rollout":     1.0,
    "title_clarity":    0.5,
    "scope_size":       0.0,   # flag — excluded from quality
    # dor_N items get weight DOR_WEIGHT each
}

DOR_WEIGHT: float = 0.75
