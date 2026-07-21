# Security policy

fiscus is a read-only public-data client. It reads optional credentials from environment variables, makes outbound GET requests to cataloged sources, and writes only beneath its configured data directory.

Do not put credentials in catalog YAML, command-line parameters, fixtures, logs, or issue reports. Report suspected secret leakage or request-redaction defects through the repository's private vulnerability-reporting channel when available. There is no response-time guarantee.
