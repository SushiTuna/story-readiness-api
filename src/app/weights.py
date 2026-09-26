"""Default weight map for assessment checks."""

WEIGHTS: dict[str, float] = {
    "ac_quality":       1.5,
    "has_persona":      1.0,   # blocker — weight unused in quality calc but defined
    "value_statement":  1.0,
    "failure_handling": 1.0,
    "safe_rollout":     1.0,
    "title_clarity":    0.5,
    "scope_size":       0.0,   # flag — excluded from quality
    # AI-agent readiness: stories are mostly implemented by coding agents, so
    # these weigh as much as the core checks (together about half the score).
    # A confident failure of any of them also caps the verdict at
    # needs_refinement (see engine.AGENT_CHECK_IDS).
    "agent_no_open_decisions": 1.0,
    "agent_verifiable":        1.0,
    "agent_code_context":      1.0,
    "agent_self_contained":    1.0,
    # dor_N items get weight DOR_WEIGHT each
}

DOR_WEIGHT: float = 0.75
