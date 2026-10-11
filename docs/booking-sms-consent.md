# SMS consent from booking confirmation

Confirmation of a public web booking accepts service notifications, not marketing.
Successful booking creation records transactional consent as `opted_in`,
the acceptance time, and the booking ID. Marketing consent is preserved unchanged.
The preferences are saved in the same transaction as the booking: failed booking
requests cannot grant consent. The existing booking form is unchanged.

This applies to new successful public web bookings, including accepted waitlist
offers. Joining a waitlist alone does not grant marketing consent. Existing records are not
backfilled; creating a new booking records service consent for returning clients too.
Manual/admin bookings do not infer client acceptance. Existing explicit marketing
or transactional opt-outs, do-not-contact and blacklist restrictions are preserved.
Language, Telegram details and repeat-booking opt-out remain unchanged.

The existing review scheduler uses marketing consent, so a booking can receive
review SMS only when separate marketing consent already exists, subject to frequency limits.
Review SMS remain restricted to 10:00–20:00 Kyiv time, with normal scheduling for
the following morning. Consent alone does not send a message.

Rollout requires migration `0106_booking_sms_consent` before the updated backend
runs. The migration only adds audit fields and leaves existing consent values
unchanged. No frontend release is required.
