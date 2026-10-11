# Security policy

fiscus is a read-only public-data client. It reads optional credentials from environment variables and queries public sources over HTTP (including POST for the BLS API). Cached responses stay beneath its configured data directory. Explicit artifact exports write to the caller's chosen destination; private local-file catalog overlays read files already on disk.

Do not put credentials in catalog YAML, command-line parameters, fixtures, logs, or issue reports. Report suspected secret leakage or request-redaction defects through the repository's private vulnerability-reporting channel when available. There is no response-time guarantee.
