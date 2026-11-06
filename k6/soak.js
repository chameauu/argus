import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.TARGET_URL || 'http://target-app:8000';

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-vus',
      vus: 5,
      duration: '30m',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<250'],
  },
};

export default function () {
  const response = http.get(`${BASE}/work`);
  check(response, { 'status is 200': (r) => r.status === 200 });
  sleep(1);
}
