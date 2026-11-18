## What does this pull request do?

<!-- One or two sentences. Link the issue: "Closes #123". -->

## Type

- [ ] New provider
- [ ] Delivery, storage, API or CLI change
- [ ] Bug fix
- [ ] Documentation

## Checklist

- [ ] Tests added or updated (`pytest` passes; tests use `httpx.MockTransport` and no network)
- [ ] `ruff check . && ruff format --check .` passes
- [ ] New providers have valid and invalid signature tests, ideally with a published test vector,
      and a row in the README provider table
- [ ] No secret values are logged, returned by the API or stored in plain config
- [ ] No real secrets in tests (use obvious placeholders such as `test-github-secret`)
- [ ] `examples/config.yaml` and the README config reference updated for new options
- [ ] CHANGELOG.md updated under "Unreleased"
