# eap-iam

The core interfaces of the pluggable IAM design (an internal design note): the
`IdentityProvider`, `PolicyEngine` and `MembershipSource` Protocols, the `Principal`, `ResourceRef`, `Decision` and
`IdFilter` types, the ordered provider chain, plug-in discovery over the entry-point groups `eap.iam.identity`,
`eap.iam.policy` and `eap.iam.membership`, and the start-up rules (`require_iam_configured`).

It imports no web framework and no database library, and never imports an `eap_iam_*` plug-in. `eap_iam.testing` has
fakes for unit tests.
