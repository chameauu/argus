#!/usr/bin/env python3
"""Golden test for route-allowlist escaping in ``platform/generate.py``.

The generator embeds ``routes_regex()`` inside a double-quoted PromQL string,
so every regex backslash has to survive PromQL unescaping as a single
backslash. A route template such as ``/orders/{id}`` is exactly the form the
contract pins, and it is the form that crashed the phase-3 onboarding; these
cases pin it so the escape path cannot regress silently.

Standard library only, so the test runs under plain ``python3`` in CI.
"""

import importlib
import re
import sys
import unittest
from pathlib import Path

PLATFORM_DIR = Path(__file__).resolve().parents[1]


def load_routes_regex():
    sys.path.insert(0, str(PLATFORM_DIR))
    try:
        module = importlib.import_module("generate")
    finally:
        sys.path.remove(str(PLATFORM_DIR))
    return module.routes_regex


routes_regex = load_routes_regex()


class RoutesRegexTest(unittest.TestCase):
    def assert_valid_re2(self, routes):
        """The PromQL-unescaped alternation compiles and matches literally."""
        escaped = routes_regex(routes)
        for run in re.findall(r"\\+", escaped):
            self.assertEqual(
                len(run) % 2,
                0,
                f"odd backslash run in PromQL string for {routes!r}: {escaped!r}",
            )
        unescaped = escaped.replace("\\\\", "\\")
        try:
            pattern = re.compile(unescaped)
        except re.error as exc:
            self.fail(f"unescaped pattern {unescaped!r} from {routes!r} is invalid: {exc}")
        for route in routes:
            self.assertIsNotNone(
                pattern.fullmatch(route),
                f"{unescaped!r} does not match its own route {route!r}",
            )
        return escaped, pattern

    def test_single_template_route(self):
        escaped, pattern = self.assert_valid_re2(["/orders/{id}"])
        self.assertEqual(escaped, "/orders/\\\\{id\\\\}")
        self.assertIsNotNone(pattern.fullmatch("/orders/{id}"))
        self.assertIsNone(pattern.fullmatch("/orders/1234"))

    def test_brace_free_routes(self):
        escaped, pattern = self.assert_valid_re2(["/work", "/slow"])
        self.assertEqual(escaped, "/work|/slow")
        self.assertIsNotNone(pattern.fullmatch("/work"))
        self.assertIsNone(pattern.fullmatch("/workshop"))

    def test_several_routes(self):
        routes = ["/work", "/slow", "/error-prone", "/orders/{id}"]
        escaped, pattern = self.assert_valid_re2(routes)
        self.assertEqual(escaped.count("|"), len(routes) - 1)
        for route in routes:
            self.assertIsNotNone(pattern.fullmatch(route))

    def test_hyphenated_route(self):
        escaped, pattern = self.assert_valid_re2(["/error-prone"])
        self.assertEqual(escaped, "/error-prone")
        self.assertIsNone(pattern.fullmatch("/errorprone"))

    def test_regex_metacharacters_are_literal(self):
        routes = ["/v1.0/items+detail", "/a(b)", "/x|y"]
        escaped, pattern = self.assert_valid_re2(routes)
        self.assertIn("\\\\", escaped)
        self.assertIsNotNone(pattern.fullmatch("/v1.0/items+detail"))
        self.assertIsNone(pattern.fullmatch("/v1x0/items+detail"))
        self.assertIsNone(pattern.fullmatch("/v1x0/itemsdetail"))
        self.assertIsNone(pattern.fullmatch("/ab"))
        self.assertIsNone(pattern.fullmatch("/x"))

    def test_foreign_app_allowlist(self):
        # Exact services/foreign-app.yaml allowlist, the phase-3 descriptor.
        routes = ["/orders", "/orders/{id}", "/checkout"]
        escaped, pattern = self.assert_valid_re2(routes)
        self.assertIn("/orders/\\\\{id\\\\}", escaped)
        self.assertIsNotNone(pattern.fullmatch("/orders/{id}"))
        self.assertIsNotNone(pattern.fullmatch("/orders"))
        self.assertIsNotNone(pattern.fullmatch("/checkout"))
        self.assertIsNone(pattern.fullmatch("/orders/{order_id}"))
        self.assertIsNone(pattern.fullmatch("/orders/42"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
