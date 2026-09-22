# PriceWatch GHCR Blank Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a versioned PriceWatch image and make a fresh one-file Compose installation work without source files or a manually supplied internal secret.

**Architecture:** A small startup helper resolves the application secret from an existing environment variable or a restricted file in `/data`; the existing app still receives the same environment variable. A dedicated pull-only Compose file mounts `/data` to a named volume. The first-run form validates a repeated password server-side; the existing GitHub release gate publishes the image after tests.

**Tech Stack:** Python 3.12, FastAPI, pytest, Docker Compose, GitHub Actions/GHCR.

**Spec:** `docs/superpowers/specs/2026-09-22-pricewatch-ghcr-blank-install-design.md`

## Global Constraints

- Preserve the existing NAS environment-variable secret and data; do not deploy this work over the current instance.
- Fresh installation must need only the published image and one YAML, with no `build:` or `/app/src` bind mount.
- Keep internal secret out of logs, UI, image, and GitHub; use a 0600 file in `/data` only when no explicit secret exists.
- Preserve existing admin password length, hashing, login throttling, CSRF, and signed-release security gates.
- First published image supports `linux/amd64` only; publish an exact version, not `latest` alone.
- Dell proxy remains optional; do not claim unproxied Dell requests avoid 403.

## Review Focus

- An existing database with no secret must fail before migration rather than silently generate a new key (Task 1 test).
- A symlink or world-readable secret file must be rejected without printing its contents (Task 1 test).
- Two simultaneous first-start processes must converge on one complete key (Task 1 test).
- A missing or mismatched confirmation must not create an administrator (Task 2 test).
- A published image must be anonymously pullable and independently start from fresh state (Task 3 remote smoke).

---

### Task 1: Persistent internal secret for fresh containers

**Files:** Create `src/pricewatch/runtime_secret.py`, `tests/unit/test_runtime_secret.py`; modify `docker/start.sh`, `tests/integration/test_container_contract.py`.

**Interfaces:** `resolve_application_secret(data_dir: Path, configured: str | None, database_path: Path) -> str` returns the explicit secret unchanged, or reads/creates `/data/.pricewatch-secret`. The CLI `python -m pricewatch.runtime_secret` prints only the resolved secret to stdout for shell capture. An error prints a non-secret diagnostic to stderr and exits nonzero.

- [ ] **Step 1: Write failing tests** for fresh 64-hex secret and 0600 mode, stable reload, explicit override, existing database refusal, symlink and loose permissions refusal, and concurrent processes returning the same complete key. Use `tmp_path`, `ThreadPoolExecutor`, `os.stat`, and literal assertions. Add a container-contract test that the startup script resolves the secret before `python -m pricewatch.bootstrap`.
- [ ] **Step 2: Verify RED.** Run `.venv/bin/python -m pytest -q tests/unit/test_runtime_secret.py tests/integration/test_container_contract.py --tb=short`; expect the new tests to fail because the module/behavior does not yet exist.
- [ ] **Step 3: Implement minimal resolver.** Return configured value when present; otherwise create `/data` if needed, reject a pre-existing database with no secret, write a `secrets.token_hex(32)` value to a 0600 temporary file and atomically hard-link it to `.pricewatch-secret`, then unlink the temporary file. Read existing files using `O_NOFOLLOW`, `fstat` regular-file and permission checks, and length validation. CLI reads `PRICEWATCH_DATA_DIR`, `PRICEWATCH_DATABASE_URL`, and `PRICEWATCH_APP_SECRET_KEY`. In `docker/start.sh`, capture the helper output into `PRICEWATCH_APP_SECRET_KEY` before migration. Do not echo or trace the secret.
- [ ] **Step 4: Verify GREEN.** Run the same targeted pytest command; expect all targeted tests pass. Run `.venv/bin/ruff check src tests` and `.venv/bin/mypy src/pricewatch`.
- [ ] **Step 5: Commit.** Stage only Task 1 files and commit `feat: persist generated app secret for fresh installs`.

### Task 2: Confirm password on first administrator creation

**Files:** Modify `src/pricewatch/templates/initialize.html`, `src/pricewatch/web/routes_auth.py`, `tests/unit/test_auth.py`, `.github/workflows/ci.yml`.

**Interfaces:** `POST /initialize` requires `confirm_password` equal to `password`; mismatch returns 422 and leaves the administrator table empty. Existing login stays unchanged.

- [ ] **Step 1: Write failing tests.** Change the `create_admin` test helper to submit `confirm_password=password`; test missing and mismatched confirmation, successful matching confirmation, and form containing a second password input without echoing either password. Update the CI browser smoke to fill `确认密码` after the feature exists.
- [ ] **Step 2: Verify RED.** Run `.venv/bin/python -m pytest -q tests/unit/test_auth.py --tb=short`; expect mismatch or missing confirmation test to fail against the old route.
- [ ] **Step 3: Implement minimal route and template change.** Add `<input name="confirm_password" type="password" autocomplete="new-password" required>`, add a FastAPI `Form()` parameter, and return the existing form with a safe mismatch message before storing admin. Never place either password in the template context.
- [ ] **Step 4: Verify GREEN.** Run `.venv/bin/python -m pytest -q tests/unit/test_auth.py --tb=short`; expect all pass. Update the CI smoke fill call, then run the full pytest suite in a context that permits loopback sockets plus ruff and mypy.
- [ ] **Step 5: Commit.** Stage only Task 2 files and commit `feat: confirm admin password on first setup`.

### Task 3: Pull-only Compose distribution and verified publication

**Files:** Create `compose.ghcr.yml`; modify `README.md`, `tests/integration/test_container_contract.py`, `.github/workflows/ci.yml`, `pyproject.toml`, and `src/pricewatch/__init__.py`.

**Interfaces:** `compose.ghcr.yml` runs `ghcr.io/shudaizi520/pricewatch:1.0.0` with a named `/data` volume, no `build:` or source bind, and localhost-only default port. Package and health versions become `1.0.0`. Existing `docker-compose.yml` remains supported for source builds.

- [ ] **Step 1: Write failing deployment-contract tests.** Parse `compose.ghcr.yml` and assert one service, exact GHCR image reference, named `/data` volume, no `build`, no `/app/src` mount, restart and healthcheck. In CI, add a fresh-container smoke that omits `PRICEWATCH_APP_SECRET_KEY`, observes `/initialize`, creates admin with confirmation, restarts, and logs in again.
- [ ] **Step 2: Verify RED.** Run `.venv/bin/python -m pytest -q tests/integration/test_container_contract.py --tb=short`; expect the new Compose contract to fail because the file does not exist.
- [ ] **Step 3: Add the YAML and docs.** Use the existing container limits and healthcheck, image reference pinned to the release version, and named volume. Document same-host/lan access, Docker Desktop or x86-64 host requirement, blank data semantics, optional Dell proxy, versioned update/rollback, and where the non-logged internal key resides. Keep any credentials out of the public YAML.
- [ ] **Step 4: Verify local and remote.** Run targeted and full pytest, ruff, mypy, `docker compose -f compose.ghcr.yml config` and Docker build where Docker exists. Push the reviewed branch/PR to GitHub as requested; use the existing protected signed-tag release workflow rather than bypassing it. If required GitHub configuration is unavailable, report the precise blocker. After release, anonymously pull the versioned GHCR image and run an isolated blank Compose instance, checking health, setup, restart/login, and secret persistence; remove only the known disposable test instance and volume.
- [ ] **Step 5: Commit.** Commit distribution files and documentation as `feat: add GHCR pull-only blank install` before GitHub publication.

## Execution note

The user explicitly delegated technical decisions and requested GitHub publication. Execute inline without technical approval pauses; do not weaken a security gate or claim publication before anonymous pull and isolated smoke prove it.
