# Customer activity backend implementation prompt

Implement a secure public self-service contract for a barbershop customer who
opens an SMS link and needs to view or cancel future bookings and active
waitlist requests.

Requirements:

1. Use an opaque, high-entropy capability token. Put the raw token only after
   `#` in the website URL and accept it only in `X-Customer-Activity-Token`.
   Persist only an HMAC hash with customer ownership, expiry, revocation/use
   audit, the linked message recipient, and safe source references. Treat the
   capability as a passwordless customer self-service session: default TTL 30
   days with a non-overridable 90-day maximum. A retry must revoke the prior
   capability for that message recipient; a failed/ambiguous provider attempt
   must revoke the capability created for that attempt. Never put raw tokens into query strings,
   structured logs, `MessageRecipient.rendered_message`, or `MessageLog`.
2. Add public-ID based management endpoints. The activity response must not
   reveal phone numbers, numeric customer IDs, or numeric booking IDs. It may
   list only future confirmed bookings and active/offered, unexpired waitlist
   requests. Cancellation must re-check ownership under a row lock.
3. A customer may cancel only their own future confirmed booking. Make the
   status/timestamp update atomic, commit it, then invoke freed-slot matching
   in a fresh transaction.
4. Cancelling an active/offered waitlist request cancels its live holds. Lock
   request then offers (the same ordering as offer claim), commit, then
   re-offer every released slot. Do not let claim and cancellation deadlock.
5. Send booking confirmation and waitlist-created SMS through existing
   `Campaign`, `MessageRecipient`, and `MessageLog` records with idempotency
   and provider delivery status. Store a redacted rendered body and inject the
   fragment link only transiently while handing off to the SMS provider. Apply
   transactional consent rules; waitlist-created notifications additionally
   require waitlist consent and defer during configured quiet hours.
6. Add migrations for public booking IDs, opaque capability records, and
   waitlist request/offer foreign keys on message records. Preserve old
   waitlist cancellation compatibility.
7. Add focused tests for token expiry/revocation, ownership and past/status
   guards, retry revocation, post-commit re-offer, migration upgrade/downgrade,
   and cancel-vs-claim locking behavior. Run the focused suite and update
   graphify after implementation.
