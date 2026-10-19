"""Validate the effective configuration without starting the service or contacting any external system.

Exit codes: 0 ok (warnings allowed), 1 errors. Secrets are never printed.
Usage: python scripts/validate_config.py [--json] [--strict]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from config.settings import Settings, load_policy_file  # noqa: E402


def main(argv: list[str]) -> int:
    as_json, strict = "--json" in argv, "--strict" in argv
    result = {"errors": [], "warnings": [], "settings": None}
    try:
        s = Settings()
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "settings"
            msg = err["msg"].removeprefix("Value error, ")
            result["errors"].append(f"{loc.upper()}: {msg}")  # pydantic omits input values for secret fields
        s = None
    if s is not None:
        errs, warns = s.problems()
        result["errors"] += errs
        result["warnings"] += warns
        result["settings"] = s.redacted()
        try:
            load_policy_file()
        except Exception as e:
            result["errors"].append(f"config/telephony.yaml: {type(e).__name__}")
        if s.env in ("production", "staging") and s.docs_enabled:
            result["warnings"].append("docs_enabled in production-like env")
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for w in result["warnings"]:
            print(f"WARN  {w}")
        for e in result["errors"]:
            print(f"ERROR {e}")
        print("config OK" if not result["errors"] else f"config INVALID ({len(result['errors'])} error(s))")
    return 1 if result["errors"] or (strict and result["warnings"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
