#!/usr/bin/env python3
"""Golden tests for the scrape-time cardinality guard.

Every descriptor-generated job carries a ``labelkeep`` relabel rule that drops
any label the platform does not know about. The exclusions are deliberate:

* ``__name__`` must survive; dropping it drops the whole sample.
* ``job`` and ``instance`` are target labels Prometheus copies onto the sample
  before ``metric_relabel_configs`` run, so they must be kept.
* ``service`` is stamped by the rule that follows the guard.
* ``route``/``endpoint`` are the two contract route labels, ``status`` feeds the
  RED rules, ``le``/``quantile`` are required by histograms/summaries, and
  ``version`` is the one bounded build label exporters commonly add.

The infrastructure jobs (prometheus, node-exporter, alloy) are intentionally
exempt: their label sets are large and legitimately varied.

Standard library only, so the test runs under plain ``python3`` in CI and from
``platform/test_rules.sh`` before the promtool container gate.
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "platform/templates/app-job.yml.tmpl"
RENDERED = REPO_ROOT / "docker-compose/configs/prometheus/prometheus.yml"
SERVICES_DIR = REPO_ROOT / "services"

EXPECTED_LABELS = {
    "__name__",
    "service",
    "route",
    "endpoint",
    "status",
    "le",
    "quantile",
    "job",
    "instance",
    "version",
}
INFRA_JOBS = {"prometheus", "node-exporter", "alloy"}
LABELKEEP_RE = re.compile(r"^\s*- action: labelkeep\s*$")
REGEX_RE = re.compile(r'^\s*regex: "([^"]*)"\s*$')
JOB_RE = re.compile(r'^\s*- job_name: "([^"]+)"\s*$')


def template_regex():
    """The single labelkeep regex carried by the per-service job template."""
    matches = re.findall(r'^\s*regex: "([^"]*)"\s*$', TEMPLATE.read_text(encoding="utf-8"), re.M)
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one labelkeep regex in {TEMPLATE}, got {matches}")
    return matches[0]


def parse_jobs(text):
    """Map job name -> labelkeep regex for the rendered scrape_configs.

    Comments and blank lines are ignored so the prose in the template cannot
    make an exempt infrastructure job look guarded.
    """
    jobs = {}
    current = None
    expecting_regex = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        job = JOB_RE.match(raw)
        if job:
            current = job.group(1)
            jobs[current] = None
            expecting_regex = False
            continue
        if current is None:
            continue
        if LABELKEEP_RE.match(raw):
            expecting_regex = True
            jobs[current] = ""
            continue
        if expecting_regex:
            match = REGEX_RE.match(raw)
            if match:
                jobs[current] = match.group(1)
            expecting_regex = False
    return jobs


class CardinalityGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.regex = template_regex()
        cls.pattern = re.compile(cls.regex)
        cls.jobs = parse_jobs(RENDERED.read_text(encoding="utf-8"))

    def test_template_covers_exactly_the_intended_labels(self):
        labels = self.regex.split("|")
        self.assertEqual(
            len(labels),
            len(set(labels)),
            f"duplicate alternatives in labelkeep regex: {self.regex!r}",
        )
        self.assertEqual(
            set(labels),
            EXPECTED_LABELS,
            f"labelkeep regex drifted: {sorted(set(labels) ^ EXPECTED_LABELS)}",
        )

    def test_regex_keeps_platform_labels(self):
        for label in EXPECTED_LABELS:
            self.assertIsNotNone(self.pattern.fullmatch(label), f"labelkeep regex drops {label!r}")

    def test_regex_drops_unbounded_labels(self):
        # The failure this guard exists for: per-request identity in a label.
        for label in ("order_id", "user_id", "path", "trace_id", "container", "pod"):
            self.assertIsNone(
                self.pattern.fullmatch(label), f"labelkeep regex keeps unbounded {label!r}"
            )

    def test_every_descriptor_job_is_guarded(self):
        descriptors = sorted(path.stem for path in SERVICES_DIR.glob("*.yaml"))
        self.assertTrue(descriptors)
        app_jobs = sorted(name for name in self.jobs if name not in INFRA_JOBS)
        self.assertEqual(app_jobs, descriptors, "rendered app jobs do not match descriptors")
        for name in descriptors:
            self.assertEqual(
                self.jobs[name],
                self.regex,
                f'job "{name}" does not carry the canonical labelkeep regex',
            )

    def test_infrastructure_jobs_are_exempt(self):
        for name in sorted(INFRA_JOBS):
            self.assertIn(name, self.jobs, f'infrastructure job "{name}" disappeared')
            self.assertIsNone(
                self.jobs[name],
                f'infrastructure job "{name}" must not carry labelkeep',
            )

    def test_service_stamp_still_present_on_every_app_job(self):
        text = RENDERED.read_text(encoding="utf-8")
        for name in sorted(path.stem for path in SERVICES_DIR.glob("*.yaml")):
            block = re.search(
                rf'^  - job_name: "{re.escape(name)}"\n(.*?)(?=^  - job_name:|\Z)',
                text,
                re.M | re.S,
            )
            self.assertIsNotNone(block, f'job "{name}" not found in rendered config')
            body = "\n".join(
                line for line in block.group(1).splitlines() if not line.strip().startswith("#")
            )
            self.assertIn("- target_label: service", body, f'job "{name}" lost its service stamp')
            self.assertIn(f"replacement: {name}", body, f'job "{name}" stamps the wrong service')


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
