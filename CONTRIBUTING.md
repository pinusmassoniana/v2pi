# Contributing to v2pi

v2pi is a FastAPI + Svelte control panel for a Raspberry Pi VPN gateway. Contributions are welcome.

## Development

**Backend** (Python ≥ 3.13, [uv](https://docs.astral.sh/uv/)):

```bash
cd backend
uv run pytest -q          # run the test suite
```

**Frontend** (Node 20+):

```bash
cd frontend
npm ci
npm test                  # vitest
npm run build             # type-check + build the SPA into the backend's static dir
```

Run the whole app locally without Docker:

```bash
python -m pi_gw_panel     # serves the API + bundled SPA on http://0.0.0.0:8080
```

## Pull requests

- Keep each PR to one logical change.
- Add or update tests for any behavior change (backend: `pytest`; frontend: `vitest`).
- Make sure `uv run pytest`, `npm test`, and `npm run build` all pass.
- Match the existing style: hand-rolled and dependency-light — please discuss before adding a new runtime dependency.

## Reporting issues

Open a GitHub issue with steps to reproduce, expected vs. actual behavior, and your environment (OS / arch, Docker version).
