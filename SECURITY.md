# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's **Report a vulnerability** button on this
repository's **Security** tab (private vulnerability reporting). Don't open a public issue or pull request
for a suspected vulnerability.

Include what you found, how to reproduce it, and the version or commit. We'll acknowledge the report, keep
you updated while we investigate, and credit you in the release notes if you'd like.

## Supported versions

Security fixes land on the latest release. This is a Couchbase Labs project without long-term support
branches.

## Deployment guidance

This application holds credentials for production databases and can modify destination data, so:

- set `API_AUTH_TOKEN` for any deployment reachable by more than one person;
- keep `STATE_ENCRYPTION_KEY` in a secret store and back it up with the state volume;
- leave TLS certificate verification on outside test environments;
- give source users read-only access unless you use post-cutover fallback sync;
- don't expose the API or agent-memory ports publicly.

See [Security](README.md#security) in the README for how credentials are handled.
