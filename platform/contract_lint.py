#!/usr/bin/env python3
"""Offline descriptor lint for the argus platform contract v1.

Validates every ``services/*.yaml`` through the shared parser in
``platform/descriptor.py`` and reports one PASS / FAIL line per descriptor.
It needs nothing but a Python interpreter: no Prometheus, no Tempo, no Loki,
no collector and no Docker. That makes it the gate an app team can run in its
own CI, and the pre-commit/CI gate that runs before the stack starts.

Usage:
  python3 platform/contract_lint.py [--services-dir services]

Exit code: 0 when every descriptor is valid, 2 when any is invalid or the
directory holds no descriptors. Standard library only.
"""

import argparse
import sys

from descriptor import DescriptorError, load_services


def evidence(descriptor):
    """The same schema summary the live ``descriptor`` check prints."""
    data = descriptor.data
    port = data["scrape"]["port"]
    routes = data.get("routes_allowlist")
    if isinstance(routes, list):
        return f"schema v1 valid (port {port}, {len(routes)} routes)"
    return f"schema v1 valid (port {port}, routes_allowlist not declared)"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate service descriptors offline (no stack required)."
    )
    parser.add_argument(
        "--services-dir",
        default="services",
        help="directory holding <name>.yaml descriptors (default services)",
    )
    args = parser.parse_args(argv)

    try:
        descriptors = load_services(args.services_dir)
    except DescriptorError as exc:
        print(f"contract-lint: {exc}", file=sys.stderr)
        return 2

    width = max(len(descriptor.path) for descriptor in descriptors)
    invalid = 0
    for descriptor in descriptors:
        if descriptor.error:
            detail = descriptor.error
            invalid += 1
        elif descriptor.problems:
            count = len(descriptor.problems)
            detail = f"{count} problem(s): " + "; ".join(descriptor.problems)
            invalid += 1
        else:
            detail = evidence(descriptor)
        verdict = "FAIL" if descriptor.error or descriptor.problems else "PASS"
        print(f"{descriptor.path:<{width}}  {verdict:<4}  {detail}")

    total = len(descriptors)
    valid = total - invalid
    if invalid:
        print(f"{valid}/{total} descriptors valid, {invalid} invalid")
        return 2
    print(f"{valid}/{total} descriptors valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
