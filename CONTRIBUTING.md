# Contributing to v2pi

v2pi is a FastAPI + Svelte control panel for a Raspberry Pi VPN gateway. Contributions are welcome.

## Development

**Backend** (Python ≥ 3.13, [uv](https://docs.astral.sh/uv/)):

```bash
cd backend
uv lock --check
uv run --locked pytest -q
```

**Frontend** (Node 20.19+, 22.13+, or 24+):

```bash
cd frontend
npm ci
npm run check             # TypeScript contract check
npm test                  # vitest
npm run build             # build the SPA into the backend's static dir
```

Run the whole app locally without Docker:

```bash
cd backend
PI_GW_BIND_HOST=127.0.0.1 PI_GW_NET_BACKEND=dryrun uv run --locked python -m pi_gw_panel
```

The loopback development server uses HTTP. A non-loopback bind uses configured or generated TLS; see
`.env.example`. E2E always launches its own server with a throwaway data directory and dry-run network
backend, so stop any process already using its configured port first.

## Pull requests

- Keep each PR to one logical change.
- Add or update tests for any behavior change (backend: `pytest`; frontend: `vitest`).
- Make sure `uv lock --check`, `uv run --locked pytest`, `npm run check`, `npm test`, and
  `npm run build` all pass. Run `uv audit --locked` and `npm audit --audit-level=high` when lock files
  change. CI blocks every vulnerability reported by `uv audit`, and npm findings at high or critical
  severity.
- Match the existing style: hand-rolled and dependency-light — please discuss before adding a new runtime dependency.

Container/release changes additionally require `docker compose config` with a digest-valued
`V2PI_IMAGE`, a Docker build, and `scripts/container-smoke.sh image@sha256:digest`. Portable CI can
smoke amd64 and QEMU arm64 artifacts; native Pi data-path/cutover and forced rollback remain required
deployment acceptance checks.

## Reporting issues

Open a GitHub issue with steps to reproduce, expected vs. actual behavior, and your environment (OS / arch, Docker version).
