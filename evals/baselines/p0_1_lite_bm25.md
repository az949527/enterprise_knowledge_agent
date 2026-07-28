# P0-1 Evaluation Report

- Baseline ID: enterprise_policy_p0_1_v1
- Created: 2026-07-27T15:35:15
- Strategy: lite_bm25_local
- Dataset SHA-256: `75b231b4333a746b9f8faa4b045af95a8687a7df5054fa9c724ed02ecf8401ca`
- Document corpus SHA-256: `c67f6c713d8bd3d48eceacde6ae0c41d370c16511960f1af2fa5a4767bf907e6`
- Cases: 30

## Quality

| Metric | Value |
|---|---:|
| Recall@5 | 1.000 |
| MRR | 1.000 |
| Answer coverage | 1.000 |
| Citation accuracy | 0.200 |
| Refusal accuracy | not measured |

## Performance And Cost

| Metric | Value |
|---|---:|
| Average latency | 5.56 ms |
| P95 latency | 6.88 ms |
| Index elapsed | 45.09 ms |
| Index peak RSS | 39923712 bytes |
| Index RSS increase | 1908736 bytes |
| Query peak RSS | 40046592 bytes |
| Index disk | 195628 bytes |
| API tokens | 0 |
| API cost | 0.000000 USD |

## Case Types

| Type | Cases | Recall@5 | MRR | Coverage | Citation | Refusal |
|---|---:|---:|---:|---:|---:|---:|
| plain_text | 30 | 1.000 | 1.000 | 1.000 | 0.200 | not measured |
