# SMS consent from booking confirmation

The website booking form must visibly disclose acceptance of service and marketing
SMS before submitting `sms_consent: true` to `POST /bookings`. This explicit boolean
applies to bookings with any master, not a selected master ID. Successful creation
records both transactional and marketing consent as `opted_in`, the acceptance
time, and the booking ID in the same transaction. Failed requests cannot grant consent.

The field defaults to `false` for older clients. With `false` or an omitted field,
successful web bookings still record service consent, but preserve existing marketing
consent; a new preference starts with marketing `unknown`. No historical backfill occurs.

Accepted waitlist offers retain service-only consent until their payload contract
explicitly captures the same choice. Joining a waitlist alone grants no marketing
consent. Manual/admin, Telegram and unknown-source bookings do not infer acceptance,
even if their payload carries `sms_consent: true`. Existing explicit marketing or
transactional opt-outs, do-not-contact and blacklist restrictions block all consent
and audit updates, even with explicit `true`. The atomic customer-key upsert preserves
these restrictions when concurrent updates commit before consent recording.
Language, Telegram details and repeat-booking opt-out remain unchanged.

The existing review scheduler uses marketing consent, so a booking can receive
review SMS only when marketing consent exists, including this explicit website choice,
subject to frequency limits.
Review SMS remain restricted to 10:00–20:00 Kyiv time, with normal scheduling for
the following morning. Consent alone does not send a message.

Rollout requires migration `0106_booking_sms_consent` before the updated backend
runs. The migration only adds audit fields and leaves existing consent values
unchanged. This contract adds no further migration or global consent-default change.
The expanded opt-in requires a frontend release with `sms_consent: true` and updated
visible disclosure; backend deployment alone keeps older clients service-only.
