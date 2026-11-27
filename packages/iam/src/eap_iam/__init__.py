"""Core IAM interfaces: Protocols, types, the identity chain, plug-in discovery and start-up rules.

Imports no web framework, no database library and no `eap_iam_*` plug-in."""

from eap_iam.chain import (
    INVALID_AUTHORIZATION_DETAIL,
    MISSING_AUTHORIZATION_DETAIL,
    NO_USER_PROVIDER_DETAIL,
    IdentityChain,
)
from eap_iam.config import IamSettings, auth_mode, require_iam_configured
from eap_iam.constraints import ACTION_SCOPES, ROLE_SCOPES, has_required_scope, permits, scopes_for_roles
from eap_iam.protocols import (
    GROUP_IDENTITY,
    GROUP_MEMBERSHIP,
    GROUP_MIGRATIONS,
    GROUP_POLICY,
    ExplainingPolicyEngine,
    GrantingPolicyEngine,
    IdentityProvider,
    MembershipSource,
    PasswordSignInProvider,
    PolicyEngine,
    ResourceCatalog,
)
from eap_iam.registry import PluginContext, register_local
from eap_iam.runtime import IamRuntime, check_iam, load_iam
from eap_iam.types import (
    ACTION_INGEST_OTLP,
    ACTION_INGEST_WEBHOOK,
    ACTION_MCP_MANAGE,
    ACTION_MCP_READ,
    ACTION_RUN_AGUI,
    ACTION_RUN_EXTERNAL,
    ALL_KINDS,
    AccessRequirement,
    Action,
    All,
    AuthError,
    CatalogEntry,
    Decision,
    Except,
    GRANT_RELATIONS,
    GRANT_SUBJECT_ROLE,
    GRANT_SUBJECT_USER,
    Grant,
    GrantableRelation,
    GrantError,
    IamConfigError,
    IdFilter,
    MEMBER_ROLES,
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_VIEWER,
    Member,
    MembershipError,
    Only,
    PolicyUnavailable,
    Principal,
    RequestView,
    ResourceRef,
    SessionTokens,
    SignInError,
    SignInResult,
)
from eap_iam.use_grants import (
    ENFORCE_ENV,
    FEATURE_AGENT_GRANTS,
    USE_RESOURCE_KINDS,
    USE_SUBJECT_KINDS,
    AgentGrantEngine,
    UseGrant,
    enforcement_active,
    enforcement_requested,
    load_policy,
    supports_grants,
)

__all__ = [name for name in dir() if not name.startswith("_")]
