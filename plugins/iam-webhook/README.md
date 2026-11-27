# eap-iam-webhook

Identity plug-in (`eap.iam.identity`, name `webhook`): a delivery to `POST /api/v1/hooks/{trigger_id}` is authenticated
by the trigger's HMAC signature and nothing else. The result is a `kind="webhook"` principal whose subject is the trigger
id; its grant is the trigger row (start a run of exactly its workflow). The provider answers only the route that accepts
the `webhook` kind and the action `ingest.webhook`; every other route ignores it, and the hook route ignores every other
credential (an `Authorization` header is never read).

Answers are uniform: an unknown, disabled, archived or not-approved trigger is `not_found` (404); anything signature
related is `invalid signature` (401). The reason (`signature_mismatch`, `missing_header`, ...) travels in
`AuthError.reason` for counters and is never sent to the caller. Unknown triggers do the same HMAC work as real ones.

Secrets are `secret:<name>` references resolved per delivery in the trigger's tenant; no value is logged or returned.
