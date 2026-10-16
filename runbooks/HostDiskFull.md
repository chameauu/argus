# Runbook: HostDiskFull

## What it means
A mounted filesystem is more than 90% used, sustained for 5 minutes:
`(1 - (node_filesystem_avail_bytes / node_filesystem_size_bytes)) * 100 > 90`
(tmpfs/overlay/squashfs are excluded). A full disk stops Prometheus, Loki, Tempo and Docker
from writing and can take the whole stack down.

## How to investigate
1. Read the alert labels for the offending `instance` and `mountpoint`.
2. Query Prometheus:
   `http://localhost:19090/api/v1/query?query=(1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"})) * 100`
3. On the host, find the biggest consumers: `docker system df`, then `du -sh`.
4. Look at the observability data first: `prometheus-data`, `loki-data`, `tempo-data`,
   `grafana-data` (Docker volumes `argus_*`).

## Steps to resolve
- Reclaim space: `docker system prune`; trim old Loki/Tempo blocks that are safe to drop.
- Lower retention: Loki is already 7 days; consider reducing Prometheus
  `storage.tsdb.retention.time`.
- If the volume is legitimately growing, resize the underlying disk or move Docker's data root.
- Never delete the live TSDB/WAL directories of a running Prometheus.

## How to silence during maintenance
```
docker compose -f docker-compose/docker-compose.yml exec alertmanager amtool silence add alertname="HostDiskFull" component="host" --duration=1h --comment="disk cleanup in progress"
```
