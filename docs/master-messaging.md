# Master messaging channel policy

## Rule

Messages addressed to masters may be delivered only through Telegram and email.
SMS and WhatsApp are customer-facing channels and must never be used for a master
campaign, including as a fallback after a Telegram error.

This rule applies to campaigns created or edited in `/messaging`, seeded system
campaigns, background schedulers, retries, and manual resend flows.

## Scenario policy

| Scenario | Delivery |
| --- | --- |
| New booking | Telegram and email |
| Booking cancellation | Telegram; email may be added as a second master copy |
| Monthly reminder to open next month's schedule | Telegram only |
| Low-coverage recommendation below 30% | Part of the Telegram schedule reminder |
| Beginning-of-month repeat when no time was opened | Telegram only |

The monthly schedule campaign is deliberately Telegram-only. If the master has
no Telegram chat ID, the bot token is missing, or Telegram rejects the request,
the attempt is recorded as failed. It must not be rerouted to SMS.

## Implementation invariants

- Every master campaign stores `metadata_json.recipient = "master"`.
- Existing master campaigns are normalized to the `telegram` channel by migration.
- New master campaigns may use `telegram` or `email`; unsupported channels are
  normalized to `telegram` by the API.
- Booking lifecycle and monthly schedule services reject a non-Telegram campaign
  instead of invoking the SMS provider.
- Customer SMS campaigns and OTP messages are unaffected by this policy.
