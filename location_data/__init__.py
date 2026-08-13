"""Location-data subsystem (W1) — the ČÚZK RÚIAN registry mirror loaders.

Backend / service-role only: nothing in this package is reachable from the anon or
authenticated roles. The one module the scraper reaches into is `payload_norm`
(W2a-0's churn instrument), lazy-imported by scraper.db behind a default-off flag.
"""
