"""The built-in admin (`eap-iam-localadmin`, subject `localadmin:<username>`) as a principal of the backfill."""

from __future__ import annotations

from eap_iam import CatalogEntry

from eap_iam_openfga.names import is_email_subject, is_principal_subject, user_ref
from eap_iam_openfga.reconcile import desired_tuples


def test_the_built_in_admin_is_a_principal_and_never_an_email():
    assert is_principal_subject("localadmin:admin")
    assert is_principal_subject("localadmin:admin@example.test")
    assert not is_email_subject("localadmin:admin@example.test")
    assert not is_principal_subject("localadmin:has space")


def test_the_backfill_makes_the_admin_owner_of_what_it_created_and_of_what_the_owner_flag_names():
    entries = [CatalogEntry("agent", "a1", "studio", "localadmin:admin"), CatalogEntry("workflow", "w1", "studio", "operator")]
    tuples, legacy, skipped = desired_tuples(entries, ["localadmin:admin"], creators=True)
    owners = {(t.user, t.object) for t in tuples if t.relation == "owner"}
    assert owners == {(user_ref("localadmin:admin"), "agent:studio/a1"), (user_ref("localadmin:admin"), "workflow:studio/w1")}
    assert (legacy, skipped) == (1, 0)
