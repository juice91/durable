"""The IAM runtime of one API process: the validated selection, the chain, the engine and the membership source."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from eap_iam.chain import IdentityChain
from eap_iam.config import IamSettings, require_iam_configured
from eap_iam.protocols import (
    GROUP_IDENTITY,
    GROUP_MEMBERSHIP,
    GROUP_POLICY,
    IdentityProvider,
    MembershipSource,
    PolicyEngine,
)
from eap_iam.registry import PluginContext, installed
from eap_iam.types import IamConfigError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IamRuntime:
    settings: IamSettings
    providers: tuple[IdentityProvider, ...]
    chain: IdentityChain
    policy: PolicyEngine
    membership: MembershipSource | None

    @property
    def mode(self) -> str:
        return self.settings.mode

    def describe(self) -> dict[str, Any]:
        """The plug-ins and features for `GET /api/v1/iam/capabilities`. Names and flags only, no configuration."""
        membership = (
            {"name": self.settings.membership, "mutable": bool(getattr(self.membership, "mutable", False))}
            if self.membership is not None and getattr(self.membership, "configured", True)
            else None
        )
        return {
            "mode": self.settings.mode,
            "identity": [
                {"name": name, "kinds": sorted(p.kinds)} for name, p in zip(self.settings.identity, self.providers)
            ],
            "policy": {
                "name": self.settings.policy,
                "transitional": bool(self.policy.transitional),
                "features": sorted(self.policy.features),
            },
            "membership": membership,
        }


def check_iam(env: Mapping[str, str] | None = None) -> IamSettings:
    """Validate the configuration against the installed plug-ins without building any: `create_app` calls it first, so a
    misconfigured deployment refuses before it touches the database."""
    env = dict(os.environ if env is None else env)
    return require_iam_configured(
        env,
        identity=installed(GROUP_IDENTITY, env),
        policy=installed(GROUP_POLICY, env),
        membership=installed(GROUP_MEMBERSHIP, env),
    )


def load_iam(engine: Any = None, env: Mapping[str, str] | None = None) -> IamRuntime:
    """Validate the configuration against the installed plug-ins and build them. IamConfigError refuses start-up."""
    env = dict(os.environ if env is None else env)
    identity = installed(GROUP_IDENTITY, env)
    policy = installed(GROUP_POLICY, env)
    membership = installed(GROUP_MEMBERSHIP, env)
    settings = require_iam_configured(env, identity=identity, policy=policy, membership=membership)
    source = membership[settings.membership](PluginContext(engine=engine, env=env)) if settings.membership else None
    ctx = PluginContext(engine=engine, env=env, membership=source)
    providers = tuple(identity[name](ctx) for name in settings.identity)
    engine_plugin = policy[settings.policy](ctx)
    if engine_plugin.transitional:
        if settings.mode == "production" and not settings.allow_transitional:
            raise IamConfigError(
                f"the policy engine {settings.policy!r} is transitional (every authenticated user is admin) and is "
                "refused in production; set EAP_IAM_ALLOW_TRANSITIONAL=1 to run it anyway"
            )
        log.warning("IAM policy engine %r is transitional: every authenticated user is admin", settings.policy)
    return IamRuntime(
        settings=settings,
        providers=providers,
        chain=IdentityChain(providers),
        policy=engine_plugin,
        membership=source,
    )
