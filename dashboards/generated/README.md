# Generated dashboards — do not edit by hand

`slo-overview.json` in this directory is **vendored** from an upstream project
(see below). It is not rendered or diffed by `platform/generate.py`; treat it as
read-only and change it only when re-vendoring a new upstream revision.

Per-service RED dashboards (`red-<name>.json`) were retired: every service is
now covered by the single templated `../argus-service.json` (uid
`argus-service`), filtered through its `service` variable. `generate.py` still
treats a leftover `red-<name>.json` as an orphan it owns, so a stale copy is
pruned rather than left in the provisioned directory.

Regenerate the platform artefacts with:

```bash
python3 platform/generate.py
python3 platform/generate.py --check   # exit 0 = tree matches the descriptor render
```

## Vendored

`slo-overview.json` is the Sloth reference dashboard, copied unmodified apart
from the adaptations listed below.

- Source URL: <https://grafana.com/api/dashboards/14348/revisions/latest/download>
- Grafana.com dashboard id: **14348**, slug `slo-detail`, title `SLO / Detail`
- Revision: **5** (revision id `23087`, updated 2021-12-06)
- Author: Xabier Larrakoetxea (`slok`), description <https://sloth.dev>
- Licence: the downloaded dashboard JSON carries **no licence metadata** — no
  `license` key at the top level and nothing under `__inputs`/`__requires`, and
  the grafana.com listing API exposes none either. The upstream Sloth project
  that publishes it is Apache-2.0 (<https://github.com/slok/sloth/blob/main/LICENSE>).

Adaptations made while vendoring (nothing else changed — no panel, query or
template variable was touched):

1. Removed the Grafana import scaffolding `__inputs`, `__requires`, `__elements`
   and `id`; file provisioning does not resolve `__inputs`.
2. Resolved the `DS_PROMETHEUS` input by replacing every `${DS_PROMETHEUS}`
   datasource uid with `prometheus` (the uid in `provisioning/datasources/`).
3. Changed the uid from `slo-detail` to `slo-overview` so it matches this file
   and cannot collide with an imported copy.
4. Re-serialised as 2-space-indented JSON for reviewable diffs.

It drives the `$service` / `$slo` variables from `sloth_service` and
`sloth_slo`, so it needs the Sloth recording rules in
`alert-rules/generated/slo-<name>.yml` to be loaded in Prometheus.
