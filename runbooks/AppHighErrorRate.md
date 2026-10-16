# Runbook: AppHighErrorRate

## What it means
More than 5% of `target-app` requests return a 5xx status, sustained for 2 minutes:
`(sum(rate(target_app_http_requests_total{status=~"5.."}[2m])) / sum(rate(target_app_http_requests_total[2m]))) * 100 > 5`.

## How to investigate
1. Grafana -> `argus-application` dashboard -> error-ratio panel, to confirm scope
   (all endpoints or one) and the time window.
2. Find the failing endpoint:
   `http://localhost:19090/api/v1/query?query=sum by (endpoint,status) (rate(target_app_http_requests_total{status=~"5.."}[5m]))`
3. Tempo -> `argus-traces` dashboard -> filter traces by `status=error`; the waterfall shows
   which span failed.
4. From a failing trace, jump to its Loki log lines via the `trace_id` derived field to read
   the actual exception.
5. Remember `/error-prone` intentionally fails ~15% of the time - check whether the errors
   come only from that demo endpoint.

## Steps to resolve
- Bad deploy: roll back `target-app`.
- Downstream dependency (DB, third-party API): check it first, then add retries/backoff.
- Only `/error-prone` is failing: expected demo traffic - silence rather than "resolve."
- Genuine bug: fix and redeploy; watch the error-ratio panel until it clears.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="AppHighErrorRate" component="application" --duration=1h --comment="expected from /error-prone demo traffic"
```
