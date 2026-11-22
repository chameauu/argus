import http from 'k6/http';
import { check, sleep } from 'k6';

// On-demand demo of the two deliberately unhealthy target-app endpoints.
// Do not run this against a stack that must stay quiet: /slow's own p99 and
// /error-prone's error share will (correctly) trigger their RED alerts.
//
//   ./k6/run.sh slow
//
// 10 iterations over ~20s: each one hits /slow once and /error-prone once,
// enough to show both behaviours without generating sustained load.
const BASE = __ENV.TARGET_URL || 'http://target-app:8000';

export const options = {
  vus: 1,
  iterations: 10,
};

export default function () {
  const slow = http.get(`${BASE}/slow`);
  check(slow, {
    'slow status is 200': (r) => r.status === 200,
  });

  const errorProne = http.get(`${BASE}/error-prone`);
  check(errorProne, {
    '/error-prone returns 200 or 500': (r) => r.status === 200 || r.status === 500,
  });

  sleep(1);
}
