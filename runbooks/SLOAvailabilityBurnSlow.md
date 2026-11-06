# Runbook: SLOAvailabilityBurnSlow

## What it means
The `/work` availability objective is 99.5%. This alert fires when the burn rate stays above
6x for both a 30-minute and a 6-hour window — more than 3% of requests failing across six hours.

This is the "slow leak" companion to `SLOAvailabilityBurnFast`: it does not page, because at
this rate there is time to investigate before the monthly budget is gone.

## How to investigate
1. Grafana -> `argus-slo` dashboard -> availability over 6h and the error-budget panel.
2. Compare with the request rate: a low-volume endpoint makes ratios noisy.
3. Check whether failures cluster on one status code:
   `sum by (status) (rate(target_app_http_requests_total{endpoint="/work"}[30m]))`
4. Look for a pattern in the logs — `logger.error("injected failure on /work")` indicates a
   forgotten fault injection, while other messages point at real application errors.
5. Confirm no chaos run is still active: `curl -s http://localhost:18000/admin/fault`.

## Steps to resolve
- Clear any injected fault, then watch the 30m ratio recover — the 6h window lags by design.
- Fix the underlying cause (deploy, dependency, capacity) and keep the alert open until the
  30m window is back above 99.5%.
- If the leak is accepted for now, record it and silence explicitly rather than leaving it firing.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="SLOAvailabilityBurnSlow" component="slo" --duration=4h --comment="investigating slow availability burn"
```
