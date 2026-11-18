"""GitHub repository documents adapter (provider code lives only in this package)."""

from eap_kb_sources.providers.github.adapter import GithubRepositoryDocsAdapter
from eap_kb_sources.registry import register_adapter

register_adapter("repository_docs", "github", GithubRepositoryDocsAdapter)

__all__ = ["GithubRepositoryDocsAdapter"]
