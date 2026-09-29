# Contributing

Thanks for helping improve the Couchbase Onboarding Agent. Bug reports, source-specific fixes, docs and new
connectors are all welcome.

## Before you start

- Search existing issues first; for anything larger than a small fix, open an issue describing the change so
  we can agree on the approach before you invest in it.
- Security problems: don't open a public issue -- follow [SECURITY.md](SECURITY.md).
- Never include real credentials, connection strings, customer data or sample documents from production
  systems in issues, logs, tests or screenshots.

## Development setup

See [Development](README.md#development) in the README. In short:

```bash
cd backend && pip install -r requirements-dev.txt && ruff check app tests && pytest
cd frontend && npm ci && npm run build
```

`helm lint deploy/helm/couchbase-onboarding-agent` checks the chart.

## Pull requests

- Keep changes focused; one topic per PR.
- Add or update tests. Engine behavior (sync ordering, retries, cutover, rollback) is tested against the fakes
  in `backend/tests/fakes.py`; connectors against in-process emulators where one exists (`moto`, `fakeredis`).
- Keep both color themes working: use the CSS variables in `frontend/src/theme/theme.css` (or `colors` /
  `tint()` from `theme/tokens.ts`), never hard-coded colors.
- Update the README when behavior, configuration or supported versions change, and add a line to
  [CHANGELOG.md](CHANGELOG.md).
- CI must pass (lint, tests, UI build, chart lint).

## Connector checklist

A new or changed source connector should:

- keep forward migration read-only against the source;
- record its change position in `capture_change_position()` *before* the bulk copy, so nothing written during
  the copy is missed;
- make `stream_changes()` resumable from `MigrationRecord.checkpoint` where the source allows it, and yield a
  heartbeat while idle;
- produce deterministic document keys, and implement `list_keys()` for delete reconciliation;
- record type hints for values JSON can't represent, if it implements `apply_reverse()` for fallback;
- declare accurate `capabilities` -- the UI and validation present them to operators.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
