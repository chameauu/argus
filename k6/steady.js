import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.TARGET_URL || 'http://target-app:8000';

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: 10,
      timeUnit: '1s',
      duration: '2m',
      preAllocatedVUs: 10,
      maxVUs: 30,
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
}
