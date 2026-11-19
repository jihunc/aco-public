"""Entry point kept outside pytest so default collection cannot opt into live IO."""

from aco.integrations.provider_smoke.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
