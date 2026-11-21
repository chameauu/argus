# Platform architecture

**Status:** design, phase 0 built. Written 2026-11-21.
**Companion documents:** `PLAN.md` (phases, work items, gates) · `contract/v1.md` (the rules a service must satisfy).

Two different things, worth separating from the start: the **control plane** (what makes onboarding cheap) and the **runtime** (what the containers do). The finished system adds **no new runtime component** — no portal, no operator, no database. It is a generator plus a CI gate in front of the same twelve containers.

---

## 1. The finished shape

### Control plane — git and CI

```
 services/foo.yaml ──► platform/generate.py ──► five generated artefacts
       │                      │
       │                      └── deterministic: same descriptor ⇒ byte-identical output
       │
       └──► platform/contract_check.py ──► PASS / FAIL with evidence ──► CI gate
```

### Runtime — unchanged containers

```
 foo ──OTLP──────────► otel-collector ──► Tempo            spans
  │                         └── resource.service.name = foo
  ├──stdout───────────► Alloy ─────────► Loki             log lines, trace_id
  │
  └──/metrics─────────► Prometheus ─┬──► recording rules ──► slo:* series
                                    ├──► alert rules ──────► Alertmanager ──► webhook
                                    └──► Grafana ($service variable)
```

The `service` label is stamped at **scrape time** by `metric_relabel_configs`, not by the application. The platform owns identity; the application owns behaviour. A forgotten label cannot silently break a rule, because the app never writes it.

### Repository layout when done

```
platform/
  contract/v1.md              the five rules, frozen
  contract_check.py           conformance, exit code for CI
  generate.py                 descriptor → artefacts
  templates/
    slo.sloth.yml.j2          Sloth spec skeleton
    dashboard.py              Grafana Foundation SDK builder
    runbook.md.j2
    scrape.json.j2
services/
  target-app.yaml             the only per-service files that exist
  custom-exporter.yaml
  webhook-receiver.yaml
alert-rules/
  rules.yml                   host + platform rules (hand-written, generic)
  generated/                  slo-<name>.yml, red-<name>.yml — never edited by hand
dashboards/
  argus-*.json                hand-written overviews
  generated/<name>.json       one per service
docker-compose/configs/prometheus/
  prometheus.yml              no static app jobs at all
  targets.d/<name>.json       file_sd
runbooks/
  generated/<name>.md
```

---

## 2. Onboarding a service — six steps

Only step 1 is the newcomer's job.

```bash
# 1. Write one file, and satisfy the five contract rules in the app itself.
vim services/foo.yaml

# 2. Prove compliance. The checker prints evidence, not opinions.
python3 platform/contract_check.py --service foo=http://localhost:18000
#   metrics_reachable     PASS  200, 44 series, all labelled service="foo"
#   service_label         PASS  0 series missing the label
#   healthz               PASS  200 in 3 ms
#   otlp_traces           PASS  12 traces in 15m
#   trace_id_log_format   PASS  180/180 request lines carry trace_id
#   EXIT=0

# 3. Generate.
python3 platform/generate.py            # services/foo.yaml → everything below
#   docker-compose/configs/prometheus/targets.d/foo.json
#   alert-rules/generated/slo-foo.yml        Sloth: recording + MWMB burn alerts
#   alert-rules/generated/red-foo.yml        error rate, latency, down
#   dashboards/generated/foo.json            one dashboard, $service pre-wired
#   runbooks/generated/foo.md                stub with real ports and queries

# 4. Review what the platform decided for them.
git diff

# 5. Apply. Targets are picked up from file_sd without a reload; rules need one.
docker compose up -d
curl -X POST localhost:19090/-/reload

# 6. Merge. CI enforces the loop.
```

**CI gate, in order:**

1. `contract_check.py` — every service still conforms.
2. `sloth validate` + `promtool check rules` + `promtool test rules` — generated rules are valid and still satisfy their tests.
3. `pint` — PromQL lint against the live rules.
4. **Regeneration is a no-op** — `generate.py` runs, `git diff --exit-code` passes. Hand-editing a generated file fails the build and tells the author to edit the descriptor instead.

Step 6's fourth check is what stops the platform decaying into the thing it replaced. Without it, the first urgent incident produces a hand-edited dashboard, and the generator becomes a historical curiosity.

---

## 3. What happens to one request

`GET /work` on `foo`:

| Signal | Path | Freshness |
|---|---|---|
| Span | app → collector `:4318` → Tempo (batch export) | seconds to ~30 s (batch timeout) |
| Log line | app stdout → Alloy → Loki, carrying `trace_id=<hex>` | ~1 s |
| Metrics | `/metrics` scraped every 15 s → Prometheus, stamped `service="foo"` | ≤15 s |
| SLO series | recording rules over `[5m]…[6h]` windows | ≤30 s |
| Alert | rule eval every 15 s + `for:` duration + Alertmanager `group_wait` | 1–3 min |

A request is *traceable* immediately, *visible* in metrics within seconds, and *alertable* within a couple of minutes. That floor is set by scrape and evaluation intervals, not by the platform — no amount of generation makes a 15 s scrape faster.

---

## 4. What happens when it breaks

Every hop carries the same `service` identity, which is the entire point:

1. **Alert fires** with `service="foo"` in its labels, and a link to `runbooks/generated/foo.md` — which already contains foo's ports, its real queries, and its first three diagnostic commands.
2. **Runbook step 1 is the dashboard**, opened with `$service=foo` already selected. No hunting through panels to work out whether the fault is the app or the host.
3. **Correlate**: error rate up → Loki line for that minute → click `trace_id` → the failing span → the dependency that was slow.

Today each of those steps requires knowing which of 19 rules, 3 dashboards and 7 runbooks applies to the thing that just broke. Three of those runbooks currently point at `argus-application`, which has no relevant panel — that is what a hand-maintained mapping looks like after a few months. After phase 3 the mapping is generated, so it cannot be stale.

---

## 5. Today vs. finished, per artefact

| Artefact | Today | Finished |
|---|---|---|
| Scrape config | 4 `static_configs` blocks, hand-edited | one JSON file per service in `targets.d/` |
| SLO rules | 12 rules hardcoding `/work` | Sloth-generated from each descriptor |
| Alerts | `AppHighErrorRate`, `TargetAppDown` — named after one app | keyed on `job`/`service`, generated per service |
| Dashboards | 3, zero template variables | one templated overview + one generated per service |
| Runbooks | 7, three pointing at the wrong dashboard | generated stub, correct links by construction |
| Onboarding | ~19 file edits | 1 descriptor file + a pull request |

---

## 6. The boundary — what this is not

- **Not self-service.** Onboarding is a pull request a human merges. A portal would be a new UI, a new auth story, and a new way to be wrong.
- **Not multi-tenant.** One Grafana, one Alertmanager, shared auth. Teams would need RBAC, quotas and isolation, none of which exist.
- **Not an operator.** Compose, one host, no reconciliation loop. If a container is dead, it stays dead until someone restarts it.
- **Not a catalog.** `services/*.yaml` is a directory of files, not a queryable inventory with owners and scorecards.

Each of those is a separate project. Any one of them, claimed before it exists, dies to a single follow-up question.

---

## 7. Why the design looks like this

Three constraints drove it, all measured rather than assumed:

1. **Identity must be platform-owned.** 0/43 and 0/20 app series carried a `service` label before the checker existed. Asking app authors to label every collector and call site is a tax that fails silently at the first forgotten site.
2. **Rules cannot contain Grafana variables.** `{service=~"$service"}` is invalid at evaluation time — a mistake one council advisor made outright. Rules are generated or grouped `by (service)`; only panels get variables.
3. **The correlation link is a regex, not a contract.** If it is not pinned and tested, a service that logs JSON produces a link that looks identical to "this service has no traces" — worse than a visible error, because nobody investigates silence.

---

## 8. When this document becomes true

| Milestone | Claim |
|---|---|
| Phase 0 (now) | "Contract and checker exist; the stack fails 10 of 15 checks." |
| Phase 2 | "Rules, targets and dashboards are generated from descriptors; target-app regenerates identically." |
| Phase 3 | "An app I did not write was onboarded from one descriptor, and a non-conforming app was rejected." |
| Phase 4 | "Drift fails CI; one app cannot exhaust the host's cardinality budget." |

Until phase 3 runs, the diagrams above are a design, not a description. The gate exists because the design is easy and the proof is not.
