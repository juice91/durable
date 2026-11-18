"""Provider adapters. Importing this package registers every provider's adapter in the registry."""

from eap_kb_sources.providers import ti  # noqa: F401  (registers ("product_catalog", "acme_catalog"))
