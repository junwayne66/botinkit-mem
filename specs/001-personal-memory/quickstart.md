# Validation Quickstart

```bash
uv sync --extra dev
uv run bmem --home /tmp/bmem-demo init --skip-models --skip-service
uv run bmem --home /tmp/bmem-demo start --port 18790
```

Use another terminal with the same home and `--url http://127.0.0.1:18790`.

```bash
uv run bmem --home /tmp/bmem-demo --url http://127.0.0.1:18790 declare '一期使用 SQLite' --fact-key phase1.storage
uv run bmem --home /tmp/bmem-demo --url http://127.0.0.1:18790 search SQLite
uv run pytest tests -q
```

Run runtime installation against the isolated data home, inspect real status and model digest, then
enable semantic search and execute the five Chinese paraphrase cases. Run connect with
BMEM_CONFIG_ROOT pointing at a temporary home containing test Harness configs. Exercise generated
callbacks with real CLI invocation and MCP client. Open the running service in Chrome for seven-page,
375px, keyboard and reduced-motion validation. Record commands, review coverage and actual results
in validation.md. Default init performs full provisioning. Skip flags support deterministic local
development and offline diagnosis.
