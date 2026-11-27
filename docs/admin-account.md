# The built-in admin account

A deployment without an identity provider signs people in with **one built-in admin account** (username and password),
from the identity plug-in `localadmin` (`plugins/iam-localadmin`). There is no user management and no other role: whoever
holds this account is the administrator. For several users or roles, [switch to ZITADEL](#switching-to-zitadel).

## The three Studio sign-in modes

`EAP_STUDIO_AUTH` says how Studio signs people in. Each mode goes with an API identity provider in `EAP_IAM_IDENTITY`.

| | `local` | `zitadel` | `access` |
|---|---|---|---|
| Who it is for | one administrator, no identity provider | several users and roles | a single tenant behind Cloudflare Access |
| Identity provider (`EAP_IAM_IDENTITY`) | `localadmin` | `zitadel` | `cfaccess` |
| Membership (`EAP_IAM_MEMBERSHIP`) | `localadmin` (one admin, read only) | `zitadel` | none |
| What Studio needs | nothing more | `EAP_STUDIO_ZITADEL_CLIENT_ID`, `EAP_STUDIO_ZITADEL_PROJECT_ID` (and the issuer) | Cloudflare Access in front of Studio and the API |
| What the API needs | `EAP_LOCAL_AUTH_SIGNING_KEY` | the [ZITADEL plug-in settings](../plugins/iam-zitadel/README.md) | the [Access plug-in settings](../plugins/iam-cfaccess/README.md) |
| Tokens | issued by the API, in Studio's memory | issued by ZITADEL, in Studio's memory | the Access cookie; Studio sends nothing |

Nothing falls back to another mode. When the setting is missing or unknown, `zitadel` lacks its settings, or `local`
finds no `localadmin` provider in the API, Studio shows **"No sign-in method is configured"** (its server log, or the
page, says where to look). A page reached over a port-forward on a loopback address keeps the development bearer in every
mode.

## Quick start

1. Deploy: `deploy/scripts/up.sh --secrets-env <your secrets file>`. Leave the three `EAP_LOCAL_*` fields empty.
2. Open the link the script prints at the end (`…/setup#code=…`; without a published address it first shows two
   `kubectl port-forward` commands to run).
3. Choose a username and a password. You are signed in.

With Docker Compose ([quick start](quickstart-compose.md)): `docker compose up -d --wait`, then open the link
`docker compose exec api setup-link` prints. The stack generates the signing key itself, and with `EAP_STUDIO_URL` set
(it is, there) the line the API logs ends with the link.

Local development is the same with `scripts/dev-up.sh`: add `localadmin` to `EAP_IAM_IDENTITY` in `.dev.env` (for
example `localadmin,apikey,devloopback`) and open the link it prints.

### The signing key

`EAP_LOCAL_AUTH_SIGNING_KEY` signs the sign-in tokens. **No value of it is ever in the repository: there is no default.**

- `up.sh` keeps the value the cluster's Secret `pydanticos-secrets` already has; a value in your secrets file replaces
  it; only when neither exists does it generate one (`openssl rand`), straight into the Secret, without printing it.
- `dev-up.sh` generates one into `.dev/local-auth-signing-key` (ignored by git) when none is set.
- Deploying without these scripts, create it once yourself, then deploy or restart the API:

  ```sh
  kubectl -n pydanticos patch secret pydanticos-secrets -p "{\"stringData\":{\"EAP_LOCAL_AUTH_SIGNING_KEY\":\"$(openssl rand -base64 48)\"}}"
  ```

  A mounted file works too: `EAP_LOCAL_AUTH_SIGNING_KEY_FILE` names it (used when the variable is empty).

The API **refuses to start** when the key is missing, shorter than 43 characters (64 if hexadecimal), a repeated
character, or contains a placeholder word (`change`, `example`, `default`, `secret`, `placeholder`, `xxx`).

**Rotating** it signs everyone out (the password is unaffected): put a new value
(`openssl rand -base64 48`; never commit it) in your secrets file and deploy; or, if `up.sh` generated it, remove the
field and deploy, which generates a new one:

```sh
kubectl -n pydanticos patch secret pydanticos-secrets --type json -p '[{"op":"remove","path":"/data/EAP_LOCAL_AUTH_SIGNING_KEY"}]'
```

In development, delete `.dev/local-auth-signing-key` and restart the API with `dev-up.sh`.

### The setup code

While there is no admin account, setup needs a one-time code (160 random bits; only its hash is stored). Without it
nobody can claim the instance, however they reach Studio. The deploy script gets one from the CLI in the API pod and
prints it; every API start also prints one to its standard error (the pod log, never the OpenTelemetry / Loki
pipeline). **The newest code is the valid one.** To print a new one:

```sh
kubectl -n pydanticos exec deploy/api -- python -m eap_iam_localadmin setup-code
```

(Exit status 3: the account already exists.) Creating the account destroys the code, and the setup endpoint then answers
404. Of two people setting up at once exactly one wins. Whoever can read the deploy output or the pod log, or run
commands in the pod, can set up the account: those people already control the deployment. A deploy run in CI leaves the
code in the CI log, so create the account right after such a deploy. The link carries the code after `#`, which browsers
never send to a server.

If you publish Studio under your own address, run `up.sh` with `PYDANTICOS_STUDIO_URL=https://studio.example.com` so it
prints that link.

### Automated installs

Set `EAP_LOCAL_ADMIN_USERNAME` **and** `EAP_LOCAL_ADMIN_INITIAL_PASSWORD` in the deploy secrets and the API makes the
account at its first start; no setup code is printed. The first sign-in forces a password change, and until then the API
logs a warning at every start (an error under `EAP_AUTH_MODE=production`). It does not refuse to start, because the change
needs a running API. Until the change, a corrected initial password in the secrets replaces the old one at the next start;
afterwards the value is ignored, and you can remove it. When an account already exists, it wins over both values.

## Settings

| Variable | | Meaning |
|---|---|---|
| `EAP_LOCAL_AUTH_SIGNING_KEY` | required (the scripts make it) | signs access tokens and keys the stored refresh-token hashes ("The signing key") |
| `EAP_LOCAL_AUTH_SIGNING_KEY_FILE` | optional | a file holding the key, read when the variable is empty |
| `EAP_LOCAL_ADMIN_USERNAME` | optional pair | 1 to 64 of `A-Z a-z 0-9 _ . @ -` |
| `EAP_LOCAL_ADMIN_INITIAL_PASSWORD` | optional pair | the initial password (see "Automated installs") |
| `EAP_LOCAL_ADMIN_TENANT` | `studio` | the tenant the admin belongs to |
| `EAP_LOCAL_AUTH_SESSION_HOURS` | `12` | how long a sign-in lasts however often it is renewed (1 to 720) |
| `EAP_STUDIO_URL` | none | where people reach Studio; the printed setup code then ends with the setup link |

**Password policy:** 12 to 128 characters, not the username (in any case), and a new password differs from the current
one. Hashes are argon2id.

## Sessions, throttling and audit

- Sign-in returns a 15-minute access token and a refresh token that rotates at every use. Studio keeps both in memory
  only, so a page reload asks for the password again. No cookie is used.
- A refresh token used twice ends its session. Sign-out, a password change and a reset end sessions at once: every
  request checks the session.
- 10 failures for a username, or 20 from one client address, within 15 minutes lock it for 15 minutes (the setup code:
  per address only). Behind a proxy every client shares the proxy's address unless you set `EAP_TRUSTED_PROXY_CIDRS`
  ([sender addresses](expose.md#sender-addresses-behind-a-proxy)). Someone who can reach the sign-in page can lock the
  account for 15 minutes at a time; `reset-password` clears the lock, and so does waiting.
- Audit actions: `local_setup`, `local_sign_in`, `local_password_change`, `local_sign_in_lockout`,
  `local_session_reuse`, `local_sign_out`, `local_setup_code_issued`, `local_password_reset`. Attempts made while locked
  write no row.

The API routes, all under `/api/v1/auth/local` and all 404 without the plug-in: `GET` (is setup needed?), `POST /setup`,
`/sign-in`, `/password`, `/refresh`, `/sign-out`.

## Resetting the password

In the API pod. The password is prompted for (or read from stdin), never taken from the command line:

```sh
kubectl -n pydanticos exec -it deploy/api -- python -m eap_iam_localadmin reset-password
```

It ends every session, clears the lockouts and forces a change at the next sign-in. The username does not change.

## Policy engines

The admin is the principal `localadmin:<username>`, kind user, role `admin`, in the configured tenant.

- `openaccess`: every signed-in user may do everything.
- `openfga`: the role travels with every request, so the account is the tenant admin with **nothing to bootstrap**. For
  resources that existed before the switch, the backfill links them to the tenant and makes the account their owner:
  `python -m eap_iam_openfga.backfill --tenant studio --owner localadmin:<username> --apply`
  ([Switching to OpenFGA](iam.md#switching-to-openfga)).

## Switching to ZITADEL

1. Set up ZITADEL ([the plug-in's page](../plugins/iam-zitadel/README.md)) and put its values in the deploy secrets.
2. In the ConfigMap: `EAP_IAM_IDENTITY: "zitadel,apikey,devloopback"`, `EAP_IAM_MEMBERSHIP: zitadel`,
   `EAP_STUDIO_AUTH: zitadel`. Deploy.
3. The built-in account stops working at once. What it created stays; rows name `localadmin:<username>`. Under OpenFGA,
   give a ZITADEL admin ownership with the backfill, and revoke API keys the old account owned (without the membership
   source naming it they stop working).

If you list both providers, put `localadmin` **before** `zitadel`: the ZITADEL provider refuses every JWT it did not
issue, while `localadmin` leaves other tokens to the next provider. Studio signs in with one mode at a time.
