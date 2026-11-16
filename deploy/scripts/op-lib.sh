#!/usr/bin/env bash
# Shared 1Password helpers for the deploy scripts.

# op_set_fields VAULT ITEM: set the fields given as a JSON object {label: value} on stdin,
# keeping every other field of the item.
#
# `op item edit --template` REPLACES all of an item's fields with the template's (verified
# against op 2.41 on 2026-11-16), so a template holding only the changed fields would delete
# the rest. Read the whole item, merge, write the whole item back. Values travel only through
# pipes and process substitution: never argv, never a file on disk, never printed.
op_set_fields() {
  local vault=$1 item=$2 merged
  merged=$(python3 -I -c '
import json, sys
item = json.load(open("/dev/fd/3"))
new = json.load(sys.stdin)
seen = set()
for f in item.get("fields", []):
    if f.get("label") in new:
        f["value"] = new[f["label"]]
        seen.add(f["label"])
for label in sorted(set(new) - seen):
    item.setdefault("fields", []).append({"label": label, "type": "CONCEALED", "value": new[label]})
json.dump(item, sys.stdout)
' 3< <(op item get "$item" --vault "$vault" --format json)) || return 1
  op item edit "$item" --vault "$vault" --template <(printf '%s' "$merged") </dev/null >/dev/null
  local rc=$?
  merged=""
  return $rc
}
