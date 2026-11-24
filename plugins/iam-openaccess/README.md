# eap-iam-openaccess

Throw-away policy engine (`eap.iam.policy`, name `openaccess`): every authenticated user is admin, so identity and policy
are separate plug-ins from the first day and exactly one engine is always active. It is flagged `transitional`: refused
in production unless `EAP_IAM_ALLOW_TRANSITIONAL=1`, and listed as transitional by `GET /api/v1/iam/capabilities`. It is
retired when a real engine (OpenFGA, design P2) ships.

API-key principals are constrained by their scopes and allowlists in core (`eap_iam.permits`) before any engine is asked.
