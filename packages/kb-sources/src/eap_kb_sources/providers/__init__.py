"""Provider adapters. Importing this package registers every provider's adapter in the registry."""

from eap_kb_sources.providers import ti  # noqa: F401  (registers ("product_catalog", "acme_catalog"))
from eap_kb_sources.providers import github  # noqa: F401,E402  (registers ("repository_docs", "github"))
