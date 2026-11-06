import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.TARGET_URL || 'http://target-app:8000';

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-vus',
      startVUs: 2,
      stages: [
        { duration: '10s', target: 2 },
        { duration: '30s', target: 60 },
        { duration: '30s', target: 2 },
        { duration: '10s', target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.05'],
  },
};

export default function () {
  const response = http.get(`${BASE}/work`);
  check(response, { 'status is 200': (r) => r.status === 200 });
}
