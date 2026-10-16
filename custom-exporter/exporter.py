import random
import threading
import time

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

app = FastAPI(title="custom-exporter")

active_users_total = Gauge("active_users_total", "Current active users")
job_queue_depth = Gauge("job_queue_depth", "Current queue depth")
webhook_delivery_success_ratio = Gauge(
    "webhook_delivery_success_ratio",
    "Rolling ratio of successful webhook deliveries",
)
jobs_queued_total = Counter("jobs_queued_total", "Total jobs queued")
jobs_processed_total = Counter(
    "jobs_processed_total",
    "Total jobs processed",
    labelnames=("status",),
)
webhook_delivery_duration_seconds = Histogram(
    "webhook_delivery_duration_seconds",
    "Webhook delivery duration",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)


def _simulate_business_metrics():
    queue_depth = 20
    success_count = 0
    total_count = 0

    while True:
        active_users_total.set(random.randint(50, 500))

        arrivals = random.randint(0, 8)
        drained = random.randint(0, 6)
        queue_depth = max(0, queue_depth + arrivals - drained)
        job_queue_depth.set(queue_depth)

        for _ in range(arrivals):
            jobs_queued_total.inc()

        for _ in range(drained):
            failed = random.random() < 0.05
            status = "failure" if failed else "success"
            jobs_processed_total.labels(status=status).inc()

        for _ in range(random.randint(1, 5)):
            duration = random.uniform(0.05, 3.0)
            webhook_delivery_duration_seconds.observe(duration)
            total_count += 1
            if duration < 2.5:
                success_count += 1

        if total_count > 0:
            webhook_delivery_success_ratio.set(success_count / total_count)

        time.sleep(5)


@app.on_event("startup")
def start_background_thread():
    thread = threading.Thread(target=_simulate_business_metrics, daemon=True)
    thread.start()


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"status": "ok"}
