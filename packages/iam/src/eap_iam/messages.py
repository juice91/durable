"""Fixed refusal texts, shared by the plug-ins and the entry points so an answer never drifts."""

from eap_iam.chain import NO_USER_PROVIDER_DETAIL

INVALID_API_KEY_DETAIL = "Invalid API key"  # malformed, unknown, wrong-secret, revoked or expired key
ORIGIN_NOT_ALLOWED_DETAIL = "Origin not allowed for this API key"
DEV_AUTH_DISABLED_DETAIL = NO_USER_PROVIDER_DETAIL
