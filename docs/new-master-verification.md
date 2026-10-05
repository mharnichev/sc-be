# New Master backend verification

Executed on 2026-10-05 in `/Users/markgarnicev/sc/sc-be`. Tests use local disposable PostgreSQL 16 (`127.0.0.1:55439`, database names containing `test`), per-test isolated schemas and fake SMS provider transport. No production database, real customer run or real SMS was used. Initial local files/imports/uploads and `.idea/vcs.xml` were preserved.

## Final checks

| Check | Actual result | Log |
| --- | --- | --- |
| `python3 -m pytest -q` | exit 0; 935 passed, 152 skipped, 14 deprecation warnings | terminal |
| PostgreSQL segment/history/first-visit/SMS-queue/offer/analytics suites | exit 0; 135 passed | terminal |
| Offer/contract/data/analytics/migration suites | exit 0; 20 passed | `/tmp/sc-campaign-final-offers.log` |
| Real booking service integration plus offer lifecycle/contract | exit 0; 10 passed | `/tmp/sc-campaign-booking-final.log` |
| Explicit cancellation/no_show release and completed entitlement/booking suite | exit 0; 6 passed | `/tmp/sc-campaign-no-show-final.log` |
| Final schema/quote contract checks | exit 0; 10 passed | `/tmp/sc-campaign-contract-final.log` |
| Final seed duplication/data/migration tests | exit 0; 4 passed | `/tmp/sc-campaign-data-verified.log` |
| Final offer transport and launch-control tests after adding persisted eligibility evidence | exit 0; 18 passed | `/tmp/sc-campaign-eligibility-final.log` |
| Focused booking/transactional SMS/run/dispatch/contract checks | exit 0; 212 passed, 1 PostgreSQL test skipped without opt-in URL | `/tmp/sc-campaign-final-unit-focused.log` |
| Final finalized migration upgrade from 0098 to 0099 | exit 0 | `/tmp/sc-campaign-migration-final.log` |
| Full clean migration upgrade and feature downgrade | exit 0 for both, before adding nullable redemption snapshot columns; final schema roundtrip subsequently covered by PostgreSQL data tests | `/tmp/sc-campaign-migration.log`, `/tmp/sc-campaign-downgrade.log` |
| `python3 -m compileall -q app alembic/versions/0099_new_master_campaign_offers.py` | exit 0 | terminal |
| `git diff --check` | exit 0 | terminal |
| `app.openapi()` and scoped export | succeeded; 14 paths, 45 dependency schemas | `docs/new-master-openapi.json` |

Counts overlap across commands; do not sum them as distinct tests. The default suite's skipped tests require opt-in integration environments; the relevant PostgreSQL suites were enabled and executed separately. Other skipped inventory/recovery/interval integrations were outside this change's applicable gate.

Exact final PostgreSQL regression command:

```sh
SEGMENTS_TEST_DATABASE_URL=<isolated-local-postgresql-test-url> python3 -m pytest \
  tests/test_segments_integration.py tests/test_segments_history_integration.py \
  tests/test_first_visit_integration.py tests/test_sms_queue_integration.py \
  tests/test_campaign_offer_dispatch_integration.py -q
```

Additional new data/offer tests:

```sh
SEGMENTS_TEST_DATABASE_URL=<isolated-local-postgresql-test-url> python3 -m pytest \
  tests/test_new_master_data_integration.py tests/test_new_master_contract.py \
  tests/test_campaign_offers.py tests/test_campaign_offers_integration.py \
  tests/test_campaign_offer_dispatch_integration.py tests/test_campaign_launch_controls.py -q
```

Covered behavior includes exact inclusive 3/12 calendar-month boundaries, imported/unknown history, broad-audience mandatory intersection, future bookings, 800→560 pricing, another master/customer/service rejection, expiry/reschedule, concurrent entitlement reservation, cancellation/no_show release, permanent completion and immutable financial snapshots, technical vs confirmed opens, duplicate event protection, real booking attribution, draft initialization and duplication, migration roundtrip preserving legacy transactional recipients, 10:00/18:00 boundaries, DST, retry/restart, campaign pause/cancel, provider uncertainty and transactional priority/notifications.

## Review and graph

Independent read-only review found and verified fixes for expiry/rescheduling, repricing, historical deletion/analytics, launch bypasses, audience restrictions, channel strategy and offer-only global analytics. Final reviewer reported no remaining actionable findings. A separate scoped simplification pass retained the small service/runtime boundaries; audience policy is shared by filtering and persisted explanatory facts. Workers inherited the available primary configuration; preferred TERRA model identifiers were unavailable in the collaboration schema and effective worker model/effort was not exposed. Review independence came from a separate agent context.

`graphify update .` succeeded after setting a writable Numba cache directory. The first attempt failed because the installed dependency tried to cache outside writable locations; the rerun preserved and refreshed the graph. HTML visualization was skipped by graphify's 5,000-node limit. A final contract check first referenced a nonexistent `tests/test_booking_quote.py` and ran no tests; the corrected command used the actual `tests/test_campaign_offers.py` quote tests and passed 10 tests. Opening the Obsidian vault was attempted but Obsidian is not installed. No vault contents were removed.

## Remaining production gates

Backend code is verified locally; production readiness is not claimed. Deploy the migration/backend/frontend in the order documented in `new-master-campaign-api.md`. Confirm production scheduler/worker activity and a restricted authorized test run. Readiness uses successful local task iterations; it does not prove distributed remote workers or valid provider account credentials. A deployment separating API/worker processes needs persisted remote heartbeats before relying on readiness there. Provider adapter has no authoritative tariff/balance capability, so estimated cost is unavailable and balance unsupported. Offer validity dates remain administrator-required; initialization deliberately leaves them unset.
