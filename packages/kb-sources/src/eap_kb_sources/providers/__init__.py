"""Built-in provider adapters. Other providers are separate distributions found through entry points."""


def register_builtin_adapters() -> None:
    from eap_kb_sources.providers.github import GithubRepositoryDocsAdapter
    from eap_kb_sources.registry import register_adapter

    register_adapter("repository_docs", "github", GithubRepositoryDocsAdapter)
