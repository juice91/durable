# eap-iam-openfga

Enterprise IAM policy plug-in: OpenFGA answers `check`, `check_many` and `filter_ids`, stores resource links and
per-resource grants, and ships the authorization model, the bootstrap and the reconcile and backfill tools. Design
sections 2, 5.4 to 5.6 and 11 of `docs/superpowers/specs/2026-11-22-iam-design.md`; the operator view is `docs/iam.md`,
"OpenFGA (`openfga`)".

Entry point: `eap.iam.policy` / `openfga`. It owns no platform table (the tuples live in OpenFGA's own database). It
imports `eap_iam` and, for the read-only resource catalog of the reconcile CLI, `eap_run_store`.

| Module | What it is |
|---|---|
| `model/durable.fga`, `model/durable.fga.yaml` | the model and its tests (`fga model test --tests plugins/iam-openfga/model/durable.fga.yaml`) |
| `eap_iam_openfga.engine` | the `PolicyEngine`, plus `register_resource`, `unregister_resource`, `grant`, `revoke`, `list_grants` |
| `eap_iam_openfga.client` | the HTTP client (fail closed, no secret in an error) |
| `eap_iam_openfga.dsl` | a DSL to JSON transformer for the subset the model uses (CI checks it against `fga model transform`) |
| `eap_iam_openfga.bootstrap` | `python -m eap_iam_openfga.bootstrap [--dry-run]`: find or create the store, write the model when it changed |
| `eap_iam_openfga.reconcile`, `.backfill` | `python -m eap_iam_openfga.reconcile` / `.backfill`: diff the platform's data against OpenFGA; dry run unless `--apply` |

Tests: `uv run pytest -p no:eap_db -q plugins/iam-openfga` (a fake OpenFGA over `httpx2.MockTransport`; no network). The
integration tests run against a real OpenFGA when `EAP_TEST_OPENFGA_URL` is set, or start the pinned container when
`EAP_TEST_OPENFGA_DOCKER=1`; otherwise they are skipped with the reason.
