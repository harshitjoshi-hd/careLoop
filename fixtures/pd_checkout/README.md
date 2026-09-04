# pd_checkout fixtures — frozen 2026-09-03

All values are k-anonymized production AGGREGATES (no row-level data, no PII;
reviews PII-scrubbed at capture). Sources: Redshift via the de-central proxy
(read-only) + Play Store public reviews. Windows: current 2026-08-27..09-02,
previous 2026-08-20..26, UTC, `created_time >= start AND < end`, all channels.

| file | what |
|---|---|
| snapshot.json | Fetcher-shaped two-window funnel + reasons + CT events |
| cohort_cuts.json | AggregateTool slices (consultation_required has converted counts; category/price/reason are distribution-only — see in-file `source` notes) |
| baseline.json | the full verified baseline incl. the delivered right-censoring caveat |
| reviews_scrubbed.json | 600 newest Play Store reviews, phones/emails masked |
| sphere_ids.json | project 7121 / use-case + template ids (templates @ v4, gpt-5-mini) |

Known golden asserts (tests enforce): confirmed rate 35.48%; rx confirm 30.0%
vs non-rx 39.0% (−9pp); artifact share of abandons ≈15%; VoC escalations at
threshold 20 = payment/refund (41) and consultation/doctor (21).

## Payments (added 2026-09-04)

Two cuts from `dwh.fact_scrooge_payments` (latest attempt per transaction, `service_reference_id` = `transaction_code`,
`service_type = 'pharmacy_delivery'`, same window). Totals reconcile with the snapshot: 647,199 created / 229,630 confirmed vs 647,191 / 229,622.

- `payment_method` (rate-bearing, attempts only): bank_transfer 93.5% · wallet 97.2% · card 90.8% · qris 99.5% · cash 100% · prepaid 100%.
- `payment_funnel` (distribution only): **412,477 of 647,191 created carts (63.7%) never reach a payment attempt** — that is the top gap almost entirely.
  119,402 confirm without a scrooge record (COD / other flows), 102,229 attempted and settled, 3,434 voided, 1,691 voided-and-abandoned, 3,296 refunded, 4,669 insurance-covered.

Verified code hints (GitLab blob search 2026-09-04): timor/oms PAYMENT_FAILED 45, PaymentStatus 50, expiry 50, retry 50;
scrooge/payment-service voided 50, VOIDED 50, PaymentStatus 50, PAYMENT_FAILED 14, bank_transfer 50, virtual_account 9. Zero hits: paymentTimeout, payment_expired, is_abandoned, abandonPayment.
