"""Exact registered agent read aliases; equivalence never grants invoice/action scope."""


def canonical_read_name(name: str | None) -> str | None:
    match name:
        case "get_payment_records":
            return "get_verified_payments"
        case "find_collection_cases":
            return "find_existing_cases"
        case "get_policy":
            return "get_collection_policy"
        case _:
            return name


def equivalent_read_names(name: str) -> tuple[str, ...]:
    canonical = canonical_read_name(name)
    assert canonical is not None
    aliases = ("get_payment_records", "find_collection_cases", "get_policy")
    return (canonical, *(alias for alias in aliases if canonical_read_name(alias) == canonical))
