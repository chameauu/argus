import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.TARGET_URL || 'http://target-app:8000';

export const options = {
  vus: 1,
  iterations: 5,
  thresholds: {
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const response = http.get(`${BASE}/work`);
  check(response, {
    'status is 200': (r) => r.status === 200,
    'body reports done': (r) => r.body && r.body.includes('done'),
  });
  sleep(1);
}
