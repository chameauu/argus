# Platform plan

**Status:** phase 0 complete, phase 0.5 next. Updated 2026-11-21.
**Supersedes:** `docs/council/PLATFORM-ROADMAP.md` (the council draft — kept for the reasoning, not the plan).
**Scope:** multi-app, single-operator. One host, one Grafana, one Alertmanager. Multi-team is out of scope, permanently.

---

## 1. What "platform" means here

> A new service is onboarded by adding one descriptor file. Metrics, dashboard, SLOs, alerts and a runbook appear without editing any platform config.

If onboarding requires editing `prometheus.yml`, a dashboard JSON, a rule file, or a runbook by hand, the platform has failed its own test.

Today it fails: 4 static scrape jobs, 3 dashboards with zero template variables, 12 of 19 rules hardcoding `target_app_http_requests_total{endpoint="/work"}`, 3 of 7 alerts true on a healthy stack. Onboarding is roughly nineteen hand edits.

**The platform is the diff between "onboard a service" and "edit the stack."** Not a portal, not a catalog, not an operator.

---

## 2. Decisions locked

| # | Decision | Why |
|---|---|---|
| 1 | **Contract first, generator second** | The gap list from the checker orders all later work; guessing the order is how you generalise to an imaginary service |
| 2 | **`platform/` is tracked** | `.gitignore` excludes `scripts/`, `docs/`, `*.md`. A generator git can't see isn't a platform. Added `!platform/**/*.md` so the contract is committable |
| 3 | **Sloth generates SLO rules** | v0.16.0 emits 8 recording + 7 metadata rules and multi-window multi-burn alerts per SLO, and has `validate` for CI. We lose `slo:availability:ratio5m` names; we gain correct burn math |
| 4 | **Descriptor is the only per-service artifact** | Everything else generated, committed, and CI-checked for no-op regeneration |
| 5 | **`file_sd`, not `static_configs`** | Onboarding becomes "drop a file", not "edit the scrape config" |
| 6 | **Compose, not Kubernetes** | One host, 12 containers. An operator buys nothing demonstrable here |

### Open decision

**Rule 2 — who stamps the `service` label.** Recommendation: the platform, at scrape time, via `metric_relabel_configs`, not the app. Requiring every app author to label every collector and every call site is a permanent tax and breaks silently on the first forgotten site. Contract text currently says the app does it; amendment pending.

---

## 3. Contract v1

`platform/contract/v1.md` — frozen, five rules:

| # | Rule | Test |
|---|---|---|
| 1 | OTLP to collector, `service.name` / `service.namespace` / `deployment.environment` | Tempo has spans for that service in the last 15 min |
| 2 | `/metrics` on the declared port: request counter + latency histogram (seconds), `route` and `status` labels | 200 + zero app series missing `service` |
| 3 | `/healthz` → 200 in <1 s, no downstream checks | 200 |
| 4 | Logs with pinned `trace_id=<32 lowercase hex>` on the same line | ≥1 match in Loki over 15 min |
| 5 | `services/<name>.yaml` descriptor, schema v1 | schema validation |

Non-goals, permanent: multi-tenancy · RBAC · portal · Kubernetes/operator · HA · long-term storage · per-team routing · Terraform · per-app exporters · auto-derived SLOs.

---

## 4. Phase 0 — contract + checker ✅

**Commit:** `f4061d3` — `platform/contract/v1.md` (96 lines), `platform/contract_check.py` (347 lines, stdlib only, ruff clean).

```bash
python3 platform/contract_check.py \
  --service target-app=http://localhost:18000 \
  --service custom-exporter=http://localhost:19200 \
  --service webhook-receiver=http://localhost:18090
```

Exit 0 iff no FAIL; `--json` for CI; `--timeout` per request. Verified against a Prometheus control (913 series parsed, no crash) and against dead ports, unreachable Tempo/Loki, malformed exposition, and timeouts.

### Result: 4/15 pass, 1 skipped, 10 fail

| Check | Pass | Failure |
|---|---|---|
| `metrics_reachable` | target-app, custom-exporter | **webhook-receiver**: 404, no `/metrics` at all |
| `service_label` | — | **target-app 0/43**, **custom-exporter 0/20** app series carry `service` |
| `healthz` | — | **all three** 404; each serves `/health` instead |
| `otlp_traces` | target-app | **custom-exporter, webhook-receiver**: 0 traces |
| `trace_id_log_format` | target-app (partial) | **custom-exporter 0/129**, **webhook-receiver 0/72**; target-app only **51–54/200** |

---

## 5. Work items, in order

| # | Item | Effort | Effect |
|---|---|---|---|
| 1 | **Stamp `service` via `metric_relabel_configs`** on each generated scrape job; amend rule 2; checker reads Prometheus, not `/metrics` | 1 h | clears 2 failures with zero app code |
| 2 | **`/healthz` alias** in all three apps | 15 min | clears 1 failure ×3 |
| 3 | **target-app: 100% `trace_id` coverage.** uvicorn's access log bypasses the OTel logging instrumentation — route it through the filter, or disable it and emit our own access line | 2 h | makes rule 4 honest rather than partial |
| 4 | **webhook-receiver: `/metrics` + OTel.** Counter `webhook_received_total{route,status}`, OTLP to the collector | 1 h | clears 3 failures |
| 5 | **custom-exporter: OTel + `trace_id` logs** | 1 h | clears 2 failures |
| 6 | **Phase 0.5 checker gaps**: descriptor schema validation, `route` template rule (assert label exists and values are bounded), series budget as a real verdict | 2 h | coverage the checker currently disclaims |

Expected after 1–5: ~13/15, with the remainder being the descriptor check (item 6).

---

## 6. Phase 1 — retire Promtail (1 day)

Promtail reached **EOL 2026-03-02**. Replace with Alloy or the collector's `filelog` receiver. Keep `compose_project` so existing LogQL keeps working.

**Gate:** `{compose_project="argus"}` still returns fresh lines; 11 services present; dashboards unchanged.

---

## 7. Phase 2 — the spine (1–1.5 days)

`services/<name>.yaml` → generated artefacts:

| Output | Path | Replaces |
|---|---|---|
| Scrape target | `docker-compose/configs/prometheus/targets.d/<name>.json` | 4 `static_configs` blocks |
| Sloth spec → rules | `alert-rules/generated/slo-<name>.yml` | 12 hardcoded recording rules |
| Burn-rate + RED alerts | `alert-rules/generated/alerts-<name>.yml` | 4 burn alerts, `AppHighErrorRate`, `AppHighLatency`, `TargetAppDown` |
| Dashboard | `dashboards/generated/<name>.json` | per-app hand-built dashboard |
| Runbook stub | `runbooks/generated/<name>.md` | hand-written runbook |

Dashboard emitted through the **Grafana Foundation SDK** (typed builders → JSON), with one `$service` variable — one templated dashboard, not three hand-built ones. Generated files carry a "do not edit" header; CI rejects direct edits.

**Gate:** regenerate `target-app` from its descriptor and diff against today's rules. Non-empty diff is expected; every difference gets explained, not silenced. Dual-eval one hour and compare firing sets.

---

## 8. Phase 3 — the honest proof (2 days)

1. Onboard a service **you did not write**, different shape, touching only its descriptor.
2. Negative test: a deliberately non-conforming app must be **rejected loudly** by `contract_check`.

Onboarding `custom-exporter` proves plumbing. Rejecting a bad app proves the platform.

**Gate:** elapsed time recorded, zero edits outside the descriptor, violator fails.

---

## 9. Phase 4 — enforcement (1 day)

1. `contract_check` in each app's CI (and this repo's CI for the demo apps).
2. `metric_relabel_configs` allowlist per job — drop unknown high-cardinality labels instead of storing them.
3. Platform self-monitoring: scrape failures, collector drop rate, Loki lag, TSDB headroom, Alertmanager delivery failures. The pager for the pager.
4. Written cardinality budget: series per app, total series, retention, maximum onboardable services.

**Gate:** a violating app fails in CI, not production; one app cannot OOM Prometheus.

---

## 10. Acceptance test

> Hand a colleague the contract page and a container they wrote. Start a timer. Done when metrics appear, the dashboard renders via `$service`, an alert fires on injected errors, and a `trace_id` copied from Loki lands in Tempo — with no edits outside `services/<name>.yaml`.

Caveat: **burn-rate alerts cannot fire in 15 minutes on a fresh service** — multi-window burn rates need baseline. Gate on 30–60 min of seeded traffic at a known error rate, or accept a single-window bootstrap alert as the proof and note the multi-window one is pending.

---

## 11. How to describe it

**Now:**
> "Reference implementation of a full observability pipeline — metrics, logs, traces, SLOs, alerting — with one instrumented service and an open OTel intake."

**After phase 3:**
> "One descriptor per service; metrics, dashboards, SLOs, alerts and runbooks are generated. Onboarding an app I didn't write took N minutes and zero edits to platform config."

**Never:**
> "A platform you can put any app on." It dies to "walk me through onboarding my service," and takes the rest of the interview with it.

---

## 12. Open questions

- **Metric naming:** migrate to OTel semconv (`http.server.request.duration`) or keep `prometheus_client` names behind the generator? Recommendation: the generator emits PromQL against whatever the descriptor declares, so migration is per-service and reversible.
- **Batch/non-HTTP services:** request-ratio SLOs don't fit a job. Recommendation: restrict v1 to HTTP, document the exclusion.
- **Generated files:** committed (reviewable, diffable) vs build-time. Recommendation: committed, with a CI check that regeneration is a no-op.
- **Baseline alert noise:** `AppHighLatency` and `CustomExporterQueueHigh` fire on a healthy stack, `AppHighErrorRate` sits pending. Fix the traffic mix and the simulator, or document the noise. Unresolved — it undermines every "now trigger the alert" demo.

---

## 13. Reading

- `platform/contract/v1.md` — the contract.
- `docs/labs/README.md` — what the 24 lab files exposed about this stack.
- `docs/council/council-2-report-2026-11-20.html` — the reasoning, the five advisors, and what to refuse.
- `docs/council/PLATFORM-ROADMAP.md` — the council's original draft.
