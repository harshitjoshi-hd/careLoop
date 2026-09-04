# consultation fixtures — frozen 2026-09-04

Journey #2. All values are k-anonymised PRODUCTION AGGREGATES pulled read-only from
monetization.modeled_fact_consultation_transaction via de-central (read-only Metabase, Redshift db 39). window 2026-08-27..09-02 (prev 2026-08-20..26), transaction_created_time, is_test=false, one row per transaction_id. No row-level data, no PII.
`reviews_scrubbed.json` is the same 600 PII-masked Play Store reviews as pd_checkout —
they are reviews of the Halodoc app as a whole and the consultation themes are in them.

| file | what |
|---|---|
| snapshot.json | two-window funnel created -> confirmed -> started -> completed, cancellation reasons, 19 real `consultation.*` CT events |
| cohort_cuts.json | 6 drill-down cuts, ALL rate-bearing (converted = confirmed) |

## The headline, and why it is demo-grade

created -> confirmed loses **87,511 of 226,615 (38.6%)**. After confirmation the pipeline is
tight: 96.1% of confirmed consults start, 99.97% of started complete. The whole story is the
payment step.

**54,015 of those 87,511 (61.7%) are `abandoned by system`** — the timer-driven abandon script
whose SQL is `GET_ABANDON_CONSULTATION` at `bintan/consultation ... ConsultationDao.java:146`,
the one mechanism this project hand-verified before any code was written. Code Scout can
reach it: `GET_ABANDON_CONSULTATION` (3 hits), `abandon` (13), `ConsultationDao` (17).

## Golden asserts (tests enforce)
- created->confirmed 61.38%; confirmed->started 96.06%
- payer: cash 46.5% vs insurance 79.0% (−32.5pp); free 100% by construction
- interface_type: web 35.6% vs ios 63.6% / android 60.8%
- scheduling: scheduled 42.3% vs walk_in 62.8%
- hour_of_day: 22h 45.1% vs 09h 64.9%
- consultation_trigger: pd_erx_consultation 100.0% (auto-confirmed) — exclude when judging the cash step

## Verified code hints (bintan/consultation, project 311, GitLab blob search 2026-09-04)
abandon 13 · GET_ABANDON_CONSULTATION 3 · ConsultationDao 17 · approve 13 · timeout 16 · reminder 16 ·
notification 14 · erx 20 · prescription 14 · session 14 · payment 20 · paymentFailed 7 · doctor 20 ·
schedule 13 · appointment 15 · insurance 17 · reassign 19. Zero hits: requestApproved, noDoctor.

## Payments (added 2026-09-04)

Two cuts from `dwh.fact_scrooge_payments` (latest attempt per transaction, `service_reference_id` = `transaction_code`,
`service_type = 'contact_doctor'`, same window, `is_test = false`).

- `payment_method` (rate-bearing, attempts only): wallet 96.2% · bank_transfer 92.2% · card 92.4% · qris 67.7% (n=31).
- `payment_funnel` (distribution only): 83,449 of 226,615 (36.8%) never reach a payment attempt; 69,649 attempted and settled;
  49,460 free and 14,900 insurance-covered needed no payment; 2,437 voided, 1,604 voided-and-abandoned, 2,539 refunded, 30 reversed.
  Read with `payer`: the cash step is where the loss is, and 83,449 of it happens before any payment attempt.

Verified code hints: bintan/consultation paymentTimeout 16, PAYMENT_FAILED 47, PaymentStatus 50, expiry 50, retry 50, paymentFailed 7.
