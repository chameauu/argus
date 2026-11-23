#!/usr/bin/env python3
"""Golden tests for the optional (metric-less) service descriptor.

Span-derived RED exists for every traced app, so a descriptor no longer has to
declare a metric contract. This test pins the consequence: a descriptor with
only name/owner/namespace/environment/scrape/health/runbook still validates,
still gets a scrape job and a Down alert, gets **no** Sloth SLO file, and its
runbook says the SLO is omitted because RED comes from spans.

It also pins the guards that keep the optional contract from becoming a quieter
gate: an empty ``routes_allowlist`` is still rejected, ``metrics`` and ``slos``
must be declared together, and a latency SLO still requires its allowlist.

Standard library only, so it runs under plain ``python3`` from
``platform/test_rules.sh`` before the promtool container gate.
"""

import sys
import tempfile
import unittest
from pathlib import Path

PLATFORM_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_DIR))

import contract_check  # noqa: E402
import generate  # noqa: E402

MINIMAL_YAML = """\
name: minimal-svc
owner: jmal
namespace: argus
environment: local

scrape:
  port: 8080
  path: /metrics
  interval: 15s

health:
  path: /healthz

runbook: runbooks/minimal-svc.md
"""


def minimal_data():
    return contract_check.parse_descriptor(MINIMAL_YAML)


def with_block(text, block):
    """Append a top-level YAML block to the minimal descriptor."""
    return text + block


class MinimalDescriptorValidationTest(unittest.TestCase):
    def test_minimal_descriptor_is_valid(self):
        self.assertEqual(contract_check.validate_descriptor(minimal_data(), "minimal-svc"), [])

    def test_empty_routes_allowlist_is_still_rejected(self):
        data = contract_check.parse_descriptor(with_block(MINIMAL_YAML, "\nroutes_allowlist: []\n"))
        problems = contract_check.validate_descriptor(data, "minimal-svc")
        self.assertTrue(
            any("routes_allowlist: must not be empty" in problem for problem in problems),
            problems,
        )

    def test_missing_scrape_is_rejected(self):
        data = minimal_data()
        del data["scrape"]
        problems = contract_check.validate_descriptor(data, "minimal-svc")
        self.assertIn("scrape: missing", problems)

    def test_metrics_and_slos_must_be_declared_together(self):
        metrics_only = contract_check.parse_descriptor(
            with_block(
                MINIMAL_YAML,
                "\nmetrics:\n  requests_total: minimal_http_requests_total\n",
            )
        )
        self.assertIn(
            "slos: required when metrics is declared",
            contract_check.validate_descriptor(metrics_only, "minimal-svc"),
        )
        slos_only = contract_check.parse_descriptor(
            with_block(
                MINIMAL_YAML,
                "\nslos:\n"
                "  availability:\n"
                "    objective: 0.995\n"
                "    window: 30d\n"
                "    error_selector: 'status=~\"5..\"'\n",
            )
        )
        self.assertIn(
            "metrics: required when slos is declared",
            contract_check.validate_descriptor(slos_only, "minimal-svc"),
        )

    def test_latency_slo_still_requires_the_allowlist(self):
        data = contract_check.parse_descriptor(
            with_block(
                MINIMAL_YAML,
                "\nmetrics:\n"
                "  requests_total: minimal_http_requests_total\n"
                "slos:\n"
                "  availability:\n"
                "    objective: 0.995\n"
                "    window: 30d\n"
                "    error_selector: 'status=~\"5..\"'\n"
                "  latency:\n"
                "    objective: 0.95\n"
                "    threshold_seconds: 0.5\n"
                "    histogram: minimal_http_request_duration_seconds\n",
            )
        )
        self.assertIn(
            "routes_allowlist: required when slos.latency is declared",
            contract_check.validate_descriptor(data, "minimal-svc"),
        )


class MinimalDescriptorGenerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = minimal_data()
        cls.rendered = generate.render_all([("minimal-svc", cls.data)])

    def test_no_slo_file_is_rendered(self):
        self.assertNotIn("alert-rules/generated/slo-minimal-svc.yml", self.rendered)

    def test_red_file_contains_only_the_down_rule(self):
        red = self.rendered["alert-rules/generated/red-minimal-svc.yml"]
        self.assertIn("- alert: MinimalSvcDown", red)
        self.assertNotIn("HighErrorRate", red)
        self.assertNotIn("HighLatency", red)
        self.assertEqual(red.count("- alert:"), 1)

    def test_scrape_job_is_still_rendered(self):
        prometheus = self.rendered["docker-compose/configs/prometheus/prometheus.yml"]
        self.assertIn('- job_name: "minimal-svc"', prometheus)
        self.assertIn('targets: ["minimal-svc:8080"]', prometheus)

    def test_runbook_explains_the_omitted_slo_and_links_the_dashboard(self):
        runbook = self.rendered["runbooks/generated/minimal-svc.md"]
        self.assertIn("declares no metric contract", runbook)
        self.assertIn("RED signals come from spans", runbook)
        self.assertIn("/d/argus-service?var-service=minimal-svc", runbook)
        self.assertNotIn("**Availability:**", runbook)


class RouteLabelWithoutAllowlistTest(unittest.TestCase):
    SAMPLES = [("minimal_http_requests_total", {"route": "critical", "status": "delivered"})]

    def test_absent_allowlist_passes_raw_id_and_cardinality_only(self):
        result = contract_check.check_route_label(self.SAMPLES, None)
        self.assertEqual(result.verdict, contract_check.PASS)
        self.assertIn(
            "routes_allowlist not declared; checked raw IDs and cardinality only",
            result.evidence,
        )

    def test_absent_allowlist_still_fails_on_a_raw_id(self):
        samples = [("minimal_http_requests_total", {"route": "/orders/42", "status": "200"})]
        result = contract_check.check_route_label(samples, None)
        self.assertEqual(result.verdict, contract_check.FAIL)
        self.assertIn("raw-ID", result.evidence)

    def test_absent_allowlist_still_fails_on_cardinality(self):
        samples = [
            ("minimal_http_requests_total", {"route": f"/r{index}", "status": "200"})
            for index in range(contract_check.ROUTE_VALUE_LIMIT + 1)
        ]
        result = contract_check.check_route_label(samples, None)
        self.assertEqual(result.verdict, contract_check.FAIL)
        self.assertIn("exceed", result.evidence)


class DescriptorEvidenceTest(unittest.TestCase):
    def test_metric_less_evidence_names_spans_without_failing(self):
        with tempfile.TemporaryDirectory(prefix="argus-minimal-") as temp_dir:
            path = Path(temp_dir) / "minimal-svc.yaml"
            path.write_text(MINIMAL_YAML, encoding="utf-8")
            result = contract_check.check_descriptor("minimal-svc", temp_dir)
        self.assertEqual(result.verdict, contract_check.PASS)
        self.assertIn("metrics not declared", result.evidence)
        self.assertIn("RED and SLO are span-only", result.evidence)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
