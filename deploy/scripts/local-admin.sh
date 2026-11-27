#!/usr/bin/env bash
# The built-in admin account (docs/admin-account.md): functions shared by up.sh (the public tree) and scripts/dev-lib.sh.
# Nothing here prints the signing key, passes it on a command line, or writes it anywhere but the file the caller names.

LOCAL_ADMIN_KEY_NAME=EAP_LOCAL_AUTH_SIGNING_KEY

# 256 random bits from the platform's CSPRNG, in hex (it can never look like a placeholder).
local_admin_new_key() { openssl rand -hex 32; }

# ensure_cluster_signing_key SECRET_FILE
# SECRET_FILE is the mode-600 input of the Secret pydanticos-secrets. A key already in it wins; else the Secret's own
# value is kept; only when neither has one is a new one generated. Needs kubectl_for_profile, NAMESPACE and dotenv_get.
ensure_cluster_signing_key() {
  local file=$1 current rest
  [[ -z "$(dotenv_get "$file" "$LOCAL_ADMIN_KEY_NAME")" ]] || return 0
  current=$(kubectl_for_profile -n "$NAMESPACE" get secret pydanticos-secrets --ignore-not-found \
    -o "jsonpath={.data.${LOCAL_ADMIN_KEY_NAME}}") || {
    printf 'Could not read the Secret pydanticos-secrets, so no signing key was generated over it. Check the cluster and run the deploy again.\n' >&2
    return 1
  }
  if [[ -n "$current" ]]; then
    current=$(printf '%s' "$current" | base64 --decode) || return 1
  else
    current=$(local_admin_new_key) || return 1
    printf 'Generated the sign-in signing key into the Secret pydanticos-secrets (field %s).\n' "$LOCAL_ADMIN_KEY_NAME"
  fi
  # Drop an empty line of the same name (a Secret input may not repeat a key). Rewritten in place: no second copy.
  rest=$(grep -v "^${LOCAL_ADMIN_KEY_NAME}=" "$file" || true)
  printf '%s\n%s=%s\n' "$rest" "$LOCAL_ADMIN_KEY_NAME" "$current" >"$file"
}

# The code in the line `python -m eap_iam_localadmin setup-code` prints, or nothing.
local_admin_parse_code() {
  sed -n 's/.*setup code: \([A-Z2-7-]\{20,\}\).*/\1/p' <<<"$1" | head -n 1
}

# local_admin_setup_block STUDIO_URL CODE NEW_CODE_COMMAND [LINE...]: LINEs are what to do before opening the URL.
local_admin_setup_block() {
  local url=${1%/} code=$2 again=$3 line
  shift 3
  printf '\n==================================================================\n'
  printf 'Create your admin account\n\n'
  for line in "$@"; do printf '  %s\n' "$line"; done
  printf '  Open  %s/setup#code=%s\n' "$url" "$code"
  printf '  Setup code (if the page asks): %s\n\n' "$code"
  printf '  New code: %s\n' "$again"
  printf '==================================================================\n'
}

# The end of a cluster deployment: the block while there is no admin. Never fails the deploy. Needs
# kubectl_for_profile, NAMESPACE and EXPECTED_CONTEXT. PYDANTICOS_STUDIO_URL: where people reach Studio, if published.
print_cluster_setup() {
  local kubectl="kubectl --context ${EXPECTED_CONTEXT} -n ${NAMESPACE}"
  local again="${kubectl} exec deploy/api -- python -m eap_iam_localadmin setup-code"
  local url=${PYDANTICOS_STUDIO_URL:-http://127.0.0.1:13000}
  local output="" code="" status=0 attempt
  for attempt in 1 2 3 4 5 6; do
    status=0
    output=$(kubectl_for_profile -n "$NAMESPACE" exec deploy/api -- python -m eap_iam_localadmin setup-code 2>/dev/null) || status=$?
    [[ $status != 0 && $status != 3 ]] || break
    ((attempt == 6)) || sleep "${LOCAL_ADMIN_RETRY_SECONDS:-5}"
  done
  if ((status == 3)); then
    printf '\nThe admin account exists: sign in at %s\n' "${url%/}"
    printf 'Forgot the password? %s exec -it deploy/api -- python -m eap_iam_localadmin reset-password\n' "$kubectl"
    return 0
  fi
  code=$(local_admin_parse_code "$output")
  if [[ -z "$code" ]]; then
    printf '\nCould not read a setup code yet. Once the API is running, print one with:\n  %s\nthen open %s/setup\n' "$again" "${url%/}"
    return 0
  fi
  if [[ -n "${PYDANTICOS_STUDIO_URL:-}" ]]; then
    local_admin_setup_block "$url" "$code" "$again"
  else
    local_admin_setup_block "$url" "$code" "$again" \
      "Studio is not published. In another terminal, keep these two running:" \
      "  ${kubectl} port-forward svc/studio 13000:3000 &" \
      "  ${kubectl} port-forward svc/api 18000:8000" \
      ""
  fi
}
