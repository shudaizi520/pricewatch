# PriceWatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-ready, single-container PriceWatch application for TrueNAS that monitors up to ten Dell US-first product URLs, records trusted configuration/price history, and sends change-only Feishu notifications.

**Architecture:** A single FastAPI process serves Jinja2/HTMX pages, runs one APScheduler instance, stores state in SQLite, fetches pages through an HTTP-first/Playwright-fallback pipeline, and delegates notifications to Apprise. Site adapters return typed snapshots to a state engine that owns trust, comparison, deduplication, and persistence.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTMX, SQLAlchemy, Alembic, SQLite, APScheduler, HTTPX, Beautiful Soup, Playwright Chromium, Apprise, Argon2, cryptography, pytest, Ruff, mypy, Docker Compose, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-21-pricewatch-design.md`

## Global Constraints

- Target runtime is TrueNAS on `linux/amd64`; the first public image supports `linux/amd64`.
- Runtime topology is one application container, one application process, and one persistent `/data` mount.
- SQLite is the only database; Redis, PostgreSQL, queues, and external frontend services are prohibited.
- Default timezone is `Asia/Shanghai`; default checks run at 04:00, 10:00, 16:00, and 22:00 local time.
- At most one product check may execute concurrently.
- Product capacity target is ten active monitors, with sequential access and bounded jitter.
- Public page price is authoritative; taxes, shipping, member prices, account login, checkout, and CAPTCHA bypass are excluded.
- Registration closes after the first administrator is created.
- Static assets are local; the application performs no runtime CDN fetches.
- Price history is indefinite; notifications retain 90 days, checks 30 days, and diagnostic artifacts 7 days.
- Reuse maintained libraries and preserve third-party license/attribution records; do not copy unidentified source.
- All feature work follows red-green-refactor and ends with a focused commit.

## Review Focus

- Redirects and DNS rebinding must never let a supplied product URL reach loopback, private, link-local, multicast, or metadata endpoints; Task 5 pins initial and redirected-host tests.
- A page containing financing, savings, list price, and sale price must not silently choose the wrong number; Tasks 6 and 7 pin confidence and ambiguity tests.
- A Dell URL whose configuration changes under the same path must enter `needs_attention` without generating a false price comparison; Task 8 pins this transition.
- Restart, overdue jobs, and simultaneous manual checks must not create parallel checks or duplicate notifications; Task 10 pins locking and idempotency tests.
- Passwords, cookies, webhook tokens, application secrets, and proxy credentials must never appear in logs, exports, or rendered HTML; Tasks 3, 9, and 12 pin redaction tests.

---

## Planned File Map

```text
pricewatch/
├── .dockerignore
├── .env.example
├── .github/workflows/ci.yml
├── .github/workflows/release.yml
├── .gitignore
├── Dockerfile
├── LICENSE
├── README.md
├── THIRD_PARTY_NOTICES.md
├── alembic.ini
├── docker-compose.yml
├── pyproject.toml
├── src/pricewatch/
│   ├── __init__.py
│   ├── app.py
│   ├── cli.py
│   ├── config.py
│   ├── db/{base.py,models.py,session.py}
│   ├── domain/{events.py,products.py}
│   ├── fetching/{browser.py,http.py,safety.py,types.py}
│   ├── adapters/{base.py,dell_us.py,generic.py,registry.py}
│   ├── services/{auth.py,backups.py,checks.py,crypto.py,notifications.py,retention.py,scheduler.py}
│   ├── web/{dependencies.py,forms.py,routes_auth.py,routes_products.py,routes_settings.py,routes_status.py}
│   ├── templates/{base.html,initialize.html,login.html,dashboard.html,product_add.html,product_detail.html,settings.html,status.html}
│   └── static/{app.css,app.js,vendor/htmx.min.js,vendor/chart.umd.min.js}
└── tests/
    ├── conftest.py
    ├── fixtures/dell/{product.html,product-browser.html}
    ├── unit/{test_auth.py,test_crypto.py,test_dell_adapter.py,test_generic_adapter.py,test_state_engine.py,test_url_safety.py}
    ├── integration/{test_backups.py,test_checks.py,test_notifications.py,test_scheduler.py,test_web_flows.py}
    └── e2e/test_dashboard.py
```

Each module owns one responsibility. Web routes call services; services depend on adapter and repository interfaces; adapters never write the database or send notifications.

### Task 1: Application Foundation and Reproducible Tooling

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.dockerignore`
- Create: `src/pricewatch/__init__.py`
- Create: `src/pricewatch/config.py`
- Create: `src/pricewatch/app.py`
- Create: `src/pricewatch/cli.py`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `tests/conftest.py`
- Create: `tests/integration/test_web_flows.py`

**Interfaces:**
- Produces: `Settings(data_dir: Path, database_url: str, timezone: str, app_secret_key: SecretStr, external_url: AnyHttpUrl | None)`.
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`.
- Produces: `GET /healthz -> {"status": "ok", "version": str}`.

- [ ] **Step 1: Write the failing health/configuration tests**

```python
def test_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_secret_rejects_short_value(tmp_path):
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, app_secret_key="short")
```

- [ ] **Step 2: Run the focused tests and confirm missing-package/import failures**

Run: `python -m pytest tests/integration/test_web_flows.py -q`  
Expected: FAIL because `pricewatch.app` and `Settings` do not exist.

- [ ] **Step 3: Add the package metadata and minimum application factory**

```python
class Settings(BaseSettings):
    data_dir: Path = Path("/data")
    database_url: str = "sqlite:////data/pricewatch.db"
    timezone: str = "Asia/Shanghai"
    app_secret_key: SecretStr = Field(min_length=32)
    external_url: AnyHttpUrl | None = None

def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="PriceWatch", docs_url=None, redoc_url=None)
    app.state.settings = resolved

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
```

Declare runtime and test dependencies in `pyproject.toml` (including `pytest-repeat` for concurrency repetition), configure Ruff/mypy/pytest, expose `pricewatch = "pricewatch.cli:app"`, add an empty Typer command group that returns a useful help screen, and start the third-party notice inventory with direct Python dependencies and their license/source URLs.

- [ ] **Step 4: Create the virtual environment, install the project, and run checks**

Run: `python3.12 -m venv .venv`  
Run: `.venv/bin/python -m pip install -e '.[test]'`  
Run: `.venv/bin/python -m pytest tests/integration/test_web_flows.py -q`  
Run: `.venv/bin/ruff check src tests`  
Expected: all commands PASS.

- [ ] **Step 5: Commit the foundation**

```bash
git add pyproject.toml .gitignore .dockerignore THIRD_PARTY_NOTICES.md src tests
git commit -m "chore: scaffold PriceWatch application"
```

### Task 2: SQLite Schema, Repositories, and Migrations

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial.py`
- Create: `src/pricewatch/db/base.py`
- Create: `src/pricewatch/db/models.py`
- Create: `src/pricewatch/db/session.py`
- Create: `tests/unit/test_database.py`

**Interfaces:**
- Consumes: `Settings.database_url` and `Settings.data_dir`.
- Produces: `create_engine_and_session(settings: Settings) -> tuple[Engine, sessionmaker[Session]]`.
- Produces SQLAlchemy models `Administrator`, `Setting`, `Product`, `Observation`, `CheckRun`, `NotificationDelivery`, and `BackupRecord`.
- Product statuses: `active`, `paused`, `needs_attention`, `archived`.

- [ ] **Step 1: Write failing schema and persistence tests**

```python
def test_sqlite_enables_foreign_keys_and_wal(session_factory, engine):
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"

def test_money_is_stored_as_integer_minor_units(db_session):
    product = Product(source_site="dell-us", requested_url="https://www.dell.com/x", status="active")
    db_session.add(product)
    db_session.flush()
    db_session.add(Observation(product_id=product.id, currency="USD", price_minor=299999, trusted=True))
    db_session.commit()
    assert db_session.scalar(select(Observation.price_minor)) == 299999
```

- [ ] **Step 2: Verify the tests fail before models exist**

Run: `.venv/bin/python -m pytest tests/unit/test_database.py -q`  
Expected: FAIL on missing database modules.

- [ ] **Step 3: Implement typed models, constraints, indexes, and SQLite pragmas**

Use UTC-aware timestamps, integer minor-unit money, unique event keys, indexed due/health fields, cascade rules only where permanent deletion explicitly requires them, and JSON columns only for bounded normalized configuration/evidence.

```python
class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    price_minor: Mapped[int] = mapped_column(CheckConstraint("price_minor >= 0"))
    configuration_fingerprint: Mapped[str] = mapped_column(String(64))
    trusted: Mapped[bool] = mapped_column(default=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
```

- [ ] **Step 4: Generate/apply the initial Alembic migration and rerun tests**

Run: `.venv/bin/alembic upgrade head`  
Run: `.venv/bin/python -m pytest tests/unit/test_database.py -q`  
Expected: PASS with schema at revision `0001`.

- [ ] **Step 5: Commit the storage layer**

```bash
git add alembic.ini alembic src/pricewatch/db tests/unit/test_database.py
git commit -m "feat: add SQLite persistence and migrations"
```

### Task 3: First-Run Administrator and Session Security

**Files:**
- Create: `src/pricewatch/services/auth.py`
- Create: `src/pricewatch/services/crypto.py`
- Create: `src/pricewatch/web/dependencies.py`
- Create: `src/pricewatch/web/forms.py`
- Create: `src/pricewatch/web/routes_auth.py`
- Create: `src/pricewatch/templates/initialize.html`
- Create: `src/pricewatch/templates/login.html`
- Create: `tests/unit/test_auth.py`
- Create: `tests/unit/test_crypto.py`
- Modify: `src/pricewatch/app.py`
- Modify: `src/pricewatch/cli.py`

**Interfaces:**
- Produces: `hash_password(password: str) -> str`, `verify_password(password: str, encoded: str) -> bool`.
- Produces: `SecretBox.encrypt(value: str) -> str` and `SecretBox.decrypt(token: str) -> str`.
- Produces: `require_admin(request: Request) -> Administrator`.
- Produces routes `GET/POST /initialize`, `GET/POST /login`, and `POST /logout`.

- [ ] **Step 1: Write failing initialization, login, CSRF, rate-limit, and redaction tests**

```python
def test_registration_closes_after_first_admin(client):
    create_admin(client, "owner", "a-strong-password")
    assert client.get("/initialize").status_code == 404

def test_post_without_csrf_is_rejected(initialized_client):
    assert initialized_client.post("/logout").status_code == 403

def test_logs_never_contain_credentials(caplog, client):
    secret = "feishu-secret-not-for-logs"
    client.post("/login", data={"username": "owner", "password": secret})
    assert secret not in caplog.text
```

- [ ] **Step 2: Run the auth tests and confirm route/service failures**

Run: `.venv/bin/python -m pytest tests/unit/test_auth.py tests/unit/test_crypto.py -q`  
Expected: FAIL because auth and encryption interfaces do not exist.

- [ ] **Step 3: Implement Argon2, Fernet-compatible encryption, signed sessions, CSRF, and bounded login throttling**

```python
def hash_password(password: str) -> str:
    return PasswordHasher().hash(password)

def require_admin(request: Request) -> Administrator:
    admin_id = request.session.get("admin_id")
    if admin_id is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return load_active_admin(admin_id)
```

Use an HTTP-only, same-site session cookie, enable `Secure` when the configured external URL is HTTPS, rotate the session after login, and compare CSRF values with `secrets.compare_digest`.

- [ ] **Step 4: Add terminal password reset command and run security tests**

Run: `.venv/bin/pricewatch admin reset-password --username owner --password-file /tmp/pricewatch-password`  
Run: `.venv/bin/python -m pytest tests/unit/test_auth.py tests/unit/test_crypto.py -q`  
Expected: PASS; the command does not echo the password.

- [ ] **Step 5: Commit authentication**

```bash
git add src/pricewatch/services src/pricewatch/web src/pricewatch/templates tests/unit
git commit -m "feat: add secure single-admin initialization"
```

### Task 4: Product Domain Types and Adapter Contract

**Files:**
- Create: `src/pricewatch/domain/products.py`
- Create: `src/pricewatch/domain/events.py`
- Create: `src/pricewatch/fetching/types.py`
- Create: `src/pricewatch/adapters/base.py`
- Create: `tests/unit/test_product_domain.py`

**Interfaces:**
- Produces immutable `Money(currency: str, minor: int)`.
- Produces `ProductIdentity(site: str, sku: str | None, model: str | None)`.
- Produces `ProductConfiguration(cpu, gpu, memory, storage, display, os, extras)`, `fingerprint() -> str`, and `summary() -> str`.
- Produces `ProductSnapshot(identity, canonical_url, name, configuration, price, list_price, discount_text, coupon_text, availability, evidence, confidence)`.
- Produces protocol `ProductAdapter.supports(url: URL) -> bool` and `extract(page: AcquiredPage) -> ProductSnapshot`.

- [ ] **Step 1: Write failing normalization and fingerprint tests**

```python
def test_configuration_fingerprint_ignores_case_and_whitespace():
    left = ProductConfiguration(gpu=" NVIDIA  RTX 5090 ", memory="64 GB")
    right = ProductConfiguration(gpu="nvidia rtx 5090", memory="64GB")
    assert left.fingerprint() == right.fingerprint()

def test_money_rejects_floating_point():
    with pytest.raises(TypeError):
        Money(currency="USD", minor=2999.99)
```

- [ ] **Step 2: Run domain tests and verify missing-type failures**

Run: `.venv/bin/python -m pytest tests/unit/test_product_domain.py -q`  
Expected: FAIL on missing domain modules.

- [ ] **Step 3: Implement the immutable types and deterministic normalization**

```python
@dataclass(frozen=True, slots=True)
class Money:
    currency: str
    minor: int

    def __post_init__(self) -> None:
        if type(self.minor) is not int or self.minor < 0:
            raise TypeError("minor must be a non-negative integer")
```

Fingerprint input uses normalized semantic fields, stable JSON serialization, and SHA-256. It excludes display punctuation, price, coupon, stock, and URL.

- [ ] **Step 4: Run type, unit, and lint checks**

Run: `.venv/bin/python -m pytest tests/unit/test_product_domain.py -q`  
Run: `.venv/bin/mypy src/pricewatch/domain src/pricewatch/adapters/base.py`  
Expected: PASS.

- [ ] **Step 5: Commit the domain contract**

```bash
git add src/pricewatch/domain src/pricewatch/fetching/types.py src/pricewatch/adapters/base.py tests/unit/test_product_domain.py
git commit -m "feat: define trusted product snapshot contract"
```

### Task 5: URL Safety and HTTP Acquisition

**Files:**
- Create: `src/pricewatch/fetching/safety.py`
- Create: `src/pricewatch/fetching/http.py`
- Create: `tests/unit/test_url_safety.py`
- Create: `tests/unit/test_http_fetcher.py`

**Interfaces:**
- Produces: `validate_public_url(url: str, resolver: Resolver) -> URL`.
- Produces: `HttpFetcher.fetch(url: URL) -> AcquiredPage`.
- `AcquiredPage` includes requested/final URL, status, bounded headers/body, acquisition method, and UTC timestamp.

- [ ] **Step 1: Write failing public-host, redirect, rebinding, size, and timeout tests**

```python
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://[::1]/x", "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.1/x", "http://user:pass@example.com/x",
])
def test_rejects_unsafe_targets(url, fake_resolver):
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url, fake_resolver)

def test_redirect_target_is_revalidated(fetcher, respx_mock):
    respx_mock.get("https://shop.example/item").mock(return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/private"}))
    with pytest.raises(UnsafeUrlError):
        fetcher.fetch(URL("https://shop.example/item"))
```

- [ ] **Step 2: Run focused fetch tests and confirm failures**

Run: `.venv/bin/python -m pytest tests/unit/test_url_safety.py tests/unit/test_http_fetcher.py -q`  
Expected: FAIL on missing validators/fetcher.

- [ ] **Step 3: Implement scheme/credential checks, DNS classification, redirect revalidation, response limits, and timeouts**

```python
def is_forbidden_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any((address.is_private, address.is_loopback, address.is_link_local,
                address.is_multicast, address.is_reserved, address.is_unspecified))
```

Pin each connection attempt to the validated resolution where supported, re-resolve every redirect host, cap redirects at five, body at 8 MiB, and total request time at 30 seconds.

- [ ] **Step 4: Run safety tests and the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit/test_url_safety.py tests/unit/test_http_fetcher.py -q`  
Run: `.venv/bin/python -m pytest tests/unit -q`  
Expected: PASS.

- [ ] **Step 5: Commit safe acquisition**

```bash
git add src/pricewatch/fetching tests/unit/test_url_safety.py tests/unit/test_http_fetcher.py
git commit -m "feat: add bounded public URL acquisition"
```

### Task 6: Dell US Adapter

**Files:**
- Create: `src/pricewatch/adapters/dell_us.py`
- Create: `src/pricewatch/adapters/registry.py`
- Create: `tests/fixtures/dell/product.html`
- Create: `tests/fixtures/dell/product-browser.html`
- Create: `tests/unit/test_dell_adapter.py`
- Create: `tests/fixtures/dell/PROVENANCE.md`

**Interfaces:**
- Consumes: `AcquiredPage` and the Task 4 adapter contract.
- Produces: `DellUsAdapter.extract(page) -> ProductSnapshot`.
- Produces: `AdapterRegistry.for_url(url: URL) -> ProductAdapter`.

- [ ] **Step 1: Capture sanitized, minimal Dell fixtures with provenance**

Record the supplied URL, capture date, acquisition method, redactions, and the fact that fixtures are test-only extracts. Remove scripts, tracking values, cookies, personal data, and unrelated page content while retaining the exact structured fields the parser exercises.

- [ ] **Step 2: Write failing identity, configuration, candidate-price, stock, canonical-link, and ambiguity tests**

```python
def test_extracts_reference_alienware_snapshot(dell_page):
    snapshot = DellUsAdapter().extract(dell_page)
    assert snapshot.identity.site == "dell-us"
    assert snapshot.identity.sku == "aa18250_reg_01"
    assert "RTX 5090" in snapshot.configuration.gpu
    assert snapshot.price.currency == "USD"
    assert snapshot.confidence >= Decimal("0.90")

def test_financing_and_savings_are_not_selected_as_sale_price(dell_page_with_candidates):
    snapshot = DellUsAdapter().extract(dell_page_with_candidates)
    assert snapshot.price.minor == 299999
    assert snapshot.price.minor not in {18900, 12500}
```

- [ ] **Step 3: Run Dell tests and verify extraction failures**

Run: `.venv/bin/python -m pytest tests/unit/test_dell_adapter.py -q`  
Expected: FAIL because the Dell adapter does not exist.

- [ ] **Step 4: Implement prioritized evidence extraction and conservative validation**

Use Dell structured/embedded data before visible fallback selectors. Normalize the URL-embedded SKU, require USD for Dell US price comparison, reject payment/installment/savings contexts, and attach evidence keys such as `jsonld.offers.price` or `embedded.product.price`.

```python
PRICE_PRIORITY = (
    "embedded.product.sale_price",
    "jsonld.offers.price",
    "meta.product.price.amount",
    "visible.purchase_price",
)
```

When high-priority candidates conflict without a clear current-sale context, raise `AmbiguousExtraction` rather than select one.

- [ ] **Step 5: Run adapter tests, unit suite, and fixture license/provenance check**

Run: `.venv/bin/python -m pytest tests/unit/test_dell_adapter.py -q`  
Run: `.venv/bin/python -m pytest tests/unit -q`  
Expected: PASS.

- [ ] **Step 6: Commit the Dell adapter**

```bash
git add src/pricewatch/adapters tests/fixtures/dell tests/unit/test_dell_adapter.py
git commit -m "feat: add Dell US product adapter"
```

### Task 7: Generic Adapter and Playwright Fallback

**Files:**
- Create: `src/pricewatch/adapters/generic.py`
- Create: `src/pricewatch/fetching/browser.py`
- Create: `tests/unit/test_generic_adapter.py`
- Create: `tests/unit/test_browser_fetcher.py`
- Modify: `src/pricewatch/adapters/registry.py`

**Interfaces:**
- Produces: `GenericAdapter.extract(page: AcquiredPage) -> ProductSnapshot`.
- Produces: `BrowserFetcher.fetch(url: URL) -> AcquiredPage` with method `browser`.
- Produces: `AcquisitionPipeline.acquire(url, adapter) -> tuple[AcquiredPage, ProductSnapshot]`.

- [ ] **Step 1: Write failing JSON-LD, partial-config, ambiguity, and browser-lifecycle tests**

```python
def test_generic_adapter_accepts_schema_org_product(generic_product_page):
    snapshot = GenericAdapter().extract(generic_product_page)
    assert snapshot.name == "OMEN MAX 16"
    assert snapshot.price == Money("USD", 349999)

def test_ambiguous_prices_require_confirmation(multi_price_page):
    with pytest.raises(AmbiguousExtraction):
        GenericAdapter().extract(multi_price_page)

async def test_browser_is_closed_after_failed_navigation(browser_factory):
    with pytest.raises(AcquisitionError):
        await BrowserFetcher(browser_factory).fetch(URL("https://shop.example/item"))
    assert browser_factory.last_browser.closed is True

async def test_browser_blocks_private_subresource(browser_fetcher, public_page_with_private_iframe):
    page = await browser_fetcher.fetch(public_page_with_private_iframe)
    assert "private iframe content" not in page.body
    assert browser_fetcher.blocked_requests == ["http://169.254.169.254/latest/meta-data"]
```

- [ ] **Step 2: Run tests and confirm missing implementations**

Run: `.venv/bin/python -m pytest tests/unit/test_generic_adapter.py tests/unit/test_browser_fetcher.py -q`  
Expected: FAIL.

- [ ] **Step 3: Implement structured generic extraction and browser fallback**

Generic extraction uses Product/Offer JSON-LD first, then product meta tags. It permits missing configuration but not missing/ambiguous currency or price. The pipeline calls Chromium only for client-rendered/incomplete acquisition categories, not for validation failures that a browser cannot fix.

Browser limits: one context, 30-second navigation, blocked downloads, bounded HTML, no persistent cookies, no browser reuse across products, and close in `finally`. Route interception validates every document, redirect, frame, XHR, and fetch destination with the Task 5 public-address policy before network access.

- [ ] **Step 4: Run unit tests and one local Playwright smoke test**

Run: `.venv/bin/playwright install chromium`  
Run: `.venv/bin/python -m pytest tests/unit/test_generic_adapter.py tests/unit/test_browser_fetcher.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit generic and browser acquisition**

```bash
git add src/pricewatch/adapters src/pricewatch/fetching tests/unit/test_generic_adapter.py tests/unit/test_browser_fetcher.py
git commit -m "feat: add generic extraction and browser fallback"
```

### Task 8: Trusted State Engine and History

**Files:**
- Create: `src/pricewatch/services/checks.py`
- Create: `tests/unit/test_state_engine.py`
- Create: `tests/integration/test_checks.py`

**Interfaces:**
- Produces: `CheckService.check_product(product_id: int, trigger: CheckTrigger) -> CheckOutcome`.
- Produces event types `InitialObservation`, `PriceChanged`, `OfferChanged`, `StockChanged`, `ConfigurationChanged`, `CheckFailed`, and `CheckRecovered`.
- Produces deterministic `event_key(event) -> str`.

- [ ] **Step 1: Write failing tests for initial, unchanged, price, stock, configuration, failure, recovery, and daily-checkpoint behavior**

```python
def test_configuration_change_pauses_comparison(check_service, product_with_snapshot):
    changed = snapshot(configuration=ProductConfiguration(gpu="RTX 5080"), price=Money("USD", 249999))
    outcome = check_service.accept_snapshot(product_with_snapshot.id, changed)
    assert isinstance(outcome.event, ConfigurationChanged)
    assert outcome.product.status == "needs_attention"
    assert not outcome.events_of_type(PriceChanged)

def test_unchanged_checks_store_at_most_one_daily_checkpoint(check_service, product):
    for hour in (1, 7, 13, 19):
        check_service.accept_snapshot(product.id, same_snapshot(), observed_at=utc_at(hour))
    assert observation_count(product.id) == 1

def test_unique_dell_sku_redirect_updates_canonical_url(check_service, dell_product):
    outcome = check_service.accept_snapshot(
        dell_product.id,
        same_dell_snapshot(canonical_url="https://www.dell.com/en-us/new-path/aa18250_reg_01"),
    )
    assert outcome.product.canonical_url.endswith("/aa18250_reg_01")

def test_non_unique_sku_lookup_requires_attention(check_service, missing_dell_product, dell_lookup):
    dell_lookup.results = [candidate("aa18250_reg_01", "RTX 5090"), candidate("aa18250_reg_01", "RTX 5080")]
    outcome = check_service.check_product(missing_dell_product.id, "scheduled")
    assert outcome.product.status == "needs_attention"
```

- [ ] **Step 2: Run state tests and confirm service failures**

Run: `.venv/bin/python -m pytest tests/unit/test_state_engine.py tests/integration/test_checks.py -q`  
Expected: FAIL.

- [ ] **Step 3: Implement transactional trust, comparison, lifecycle, and idempotency logic**

```python
def event_key(event: DomainEvent) -> str:
    payload = event.identity_payload()
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
```

Never modify the last trusted observation on acquisition or extraction failure. Increment consecutive failures transactionally; emit failure only when crossing from two to three. Emit recovery only when a previously reported failure returns to trusted state. Persist redirected canonical URLs only after identity/configuration confirmation. Permit one bounded Dell SKU lookup after a missing page and auto-migrate only a unique, configuration-consistent result.

- [ ] **Step 4: Run state, database, and adapter suites**

Run: `.venv/bin/python -m pytest tests/unit/test_state_engine.py tests/integration/test_checks.py tests/unit/test_database.py tests/unit/test_dell_adapter.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit trusted change detection**

```bash
git add src/pricewatch/services/checks.py src/pricewatch/domain tests/unit/test_state_engine.py tests/integration/test_checks.py
git commit -m "feat: add trusted price and configuration state engine"
```

### Task 9: Apprise/Feishu Notifications

**Files:**
- Create: `src/pricewatch/services/notifications.py`
- Create: `tests/integration/test_notifications.py`
- Modify: `src/pricewatch/services/checks.py`
- Modify: `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- Produces: `NotificationService.test_feishu(webhook: SecretStr) -> DeliveryResult`.
- Produces: `NotificationService.deliver(event: DomainEvent) -> DeliveryResult`.
- Consumes encrypted Feishu configuration and persisted event keys.

- [ ] **Step 1: Write failing message, Apprise URL, deduplication, retry, and redaction tests**

```python
def test_feishu_webhook_is_converted_without_logging_token(notification_service, caplog):
    token = "0123456789abcdef0123456789abcdef"
    notification_service.test_feishu(SecretStr(f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"))
    assert token not in caplog.text

def test_duplicate_event_is_not_delivered_twice(notification_service, price_event):
    notification_service.deliver(price_event)
    notification_service.deliver(price_event)
    assert notification_service.transport.calls == 1
```

- [ ] **Step 2: Run notification tests and verify missing service failures**

Run: `.venv/bin/python -m pytest tests/integration/test_notifications.py -q`  
Expected: FAIL.

- [ ] **Step 3: Implement Feishu normalization, concise Chinese templates, persisted delivery state, and bounded retries**

```python
def feishu_apprise_url(webhook: str) -> str:
    token = webhook.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", token):
        raise InvalidNotificationTarget("invalid Feishu webhook")
    return f"feishu://{token}?redirect=no"
```

Messages include name, configuration summary, old/new price, delta, stock, Beijing timestamp, and canonical link. Store a failed delivery for retry, but never regenerate a second logical event.

- [ ] **Step 4: Run mocked delivery and secret scans**

Run: `.venv/bin/python -m pytest tests/integration/test_notifications.py tests/unit/test_crypto.py -q`  
Run: `rg -n 'open-apis/bot|feishu://[A-Za-z0-9]' . --glob '!tests/**' --glob '!.git/**'`  
Expected: tests PASS and scan shows no real token.

- [ ] **Step 5: Commit notifications**

```bash
git add src/pricewatch/services tests/integration/test_notifications.py THIRD_PARTY_NOTICES.md
git commit -m "feat: add change-only Feishu notifications"
```

### Task 10: Single-Runner Scheduler and Manual Check Coalescing

**Files:**
- Create: `src/pricewatch/services/scheduler.py`
- Create: `tests/integration/test_scheduler.py`
- Modify: `src/pricewatch/app.py`

**Interfaces:**
- Produces: `SchedulerService.start()`, `stop()`, `schedule_product(product_id)`, and `request_check(product_id, trigger) -> JobReference`.
- Consumes: `CheckService.check_product`.

- [ ] **Step 1: Write failing Beijing-slot, restart, jitter, one-runner, and coalescing tests**

```python
def test_default_slots_are_beijing_04_10_16_22(scheduler):
    assert scheduler.default_local_hours == (4, 10, 16, 22)

async def test_manual_and_due_check_coalesce(scheduler, blocking_check_service):
    first = scheduler.request_check(7, "scheduled")
    second = scheduler.request_check(7, "manual")
    assert first.key == second.key
    assert blocking_check_service.max_concurrency == 1
```

- [ ] **Step 2: Run scheduler tests and verify failures**

Run: `.venv/bin/python -m pytest tests/integration/test_scheduler.py -q`  
Expected: FAIL.

- [ ] **Step 3: Implement one executor slot, persistent due times, coalescing, bounded jitter, and safe startup/shutdown**

APScheduler triggers enqueue product IDs into an in-process capacity-one runner. A product-level in-flight registry merges manual and scheduled requests. Startup computes the next valid slot and does not replay every missed interval.

- [ ] **Step 4: Run scheduler/state/notification integration tests repeatedly**

Run: `.venv/bin/python -m pytest tests/integration/test_scheduler.py tests/integration/test_checks.py tests/integration/test_notifications.py -q --count=3`  
Expected: PASS without timing flakes or duplicate delivery rows.

- [ ] **Step 5: Commit scheduling**

```bash
git add src/pricewatch/services/scheduler.py src/pricewatch/app.py tests/integration/test_scheduler.py
git commit -m "feat: add sequential six-hour monitoring schedule"
```

### Task 11: Approved Dashboard, Product Workflows, and Dark Mode

**Files:**
- Create: `src/pricewatch/web/routes_products.py`
- Create: `src/pricewatch/templates/base.html`
- Create: `src/pricewatch/templates/dashboard.html`
- Create: `src/pricewatch/templates/product_add.html`
- Create: `src/pricewatch/templates/product_detail.html`
- Create: `src/pricewatch/static/app.css`
- Create: `src/pricewatch/static/app.js`
- Create: `src/pricewatch/static/vendor/htmx.min.js`
- Create: `src/pricewatch/static/vendor/chart.umd.min.js`
- Create: `tests/integration/test_product_web.py`
- Create: `tests/e2e/test_dashboard.py`
- Modify: `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- Produces authenticated routes `/`, `/products/new`, `/products/{id}`, `/products/{id}/check`, `/products/{id}/pause`, `/products/{id}/archive`, and permanent delete confirmation.
- Produces routes `/products/{id}/confirm-configuration` and `/products/{id}/history.csv`.
- Produces theme values `system`, `light`, `dark` stored on `Administrator`.

- [ ] **Step 1: Write failing route and browser tests for the approved workflows**

```python
def test_add_product_requires_preview_confirmation(admin_client, fake_pipeline):
    preview = admin_client.post("/products/preview", data={"url": DELL_URL, "csrf_token": csrf(admin_client)})
    assert preview.status_code == 200
    assert "Alienware 18 Area-51" in preview.text
    assert product_count() == 0

def test_unauthenticated_dashboard_redirects(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"

async def test_dark_theme_persists(page, initialized_server):
    await login(page, initialized_server)
    await page.get_by_role("button", name="深色").click()
    await page.reload()
    assert await page.locator("html").get_attribute("data-theme") == "dark"

def test_csv_export_contains_history_but_no_secrets(admin_client, configured_product, feishu_token):
    response = admin_client.get(f"/products/{configured_product.id}/history.csv")
    assert response.headers["content-type"].startswith("text/csv")
    assert b"price_minor" in response.content
    assert feishu_token.encode() not in response.content

def test_target_price_can_be_enabled_per_product(admin_client, configured_product):
    response = admin_client.post(
        f"/products/{configured_product.id}/settings",
        data={"target_price": "2800.00", "notify_mode": "target_or_change", "csrf_token": csrf(admin_client)},
    )
    assert response.status_code == 303
    assert load_product(configured_product.id).target_price_minor == 280000
```

- [ ] **Step 2: Run web/E2E tests and confirm route/template failures**

Run: `.venv/bin/python -m pytest tests/integration/test_product_web.py tests/e2e/test_dashboard.py -q`  
Expected: FAIL.

- [ ] **Step 3: Vendor pinned HTMX 2.0.10 and Chart.js 4.5.1 release assets with their license texts**

Download HTMX 2.0.10 and Chart.js 4.5.1 only from their official release artifacts, record version, source URL, license, and SHA-256 checksum in `THIRD_PARTY_NOTICES.md`, and verify the application templates contain no runtime CDN URL.

- [ ] **Step 4: Implement the approved dashboard and product pages**

Use semantic HTML, comfortable text sizes, responsive card/list layouts, keyboard-visible focus, compact status summaries, expandable diagnostics, and CSS custom properties for light/dark/system palettes. Product add is a two-stage preview/confirm transaction. Product settings expose only the approved intervals (1, 3, 6, 12, 24 hours), notification mode, and optional target price. Configuration-change confirmation offers adopt-new or create-new-product actions. Charts receive server-provided observations as escaped JSON, and CSV uses UTC ISO-8601 timestamps plus integer minor-unit and formatted price columns.

- [ ] **Step 5: Run UI, accessibility smoke, and static-asset tests**

Run: `.venv/bin/python -m pytest tests/integration/test_product_web.py tests/e2e/test_dashboard.py -q`  
Run: `rg -n 'https://(cdn|unpkg|cdnjs)' src/pricewatch/templates src/pricewatch/static`  
Expected: tests PASS and no runtime CDN references.

- [ ] **Step 6: Commit the user interface**

```bash
git add src/pricewatch/web src/pricewatch/templates src/pricewatch/static tests/integration/test_product_web.py tests/e2e/test_dashboard.py THIRD_PARTY_NOTICES.md
git commit -m "feat: add responsive PriceWatch dashboard"
```

### Task 12: Settings, Retention, Backups, and Status

**Files:**
- Create: `src/pricewatch/services/backups.py`
- Create: `src/pricewatch/services/retention.py`
- Create: `src/pricewatch/web/routes_settings.py`
- Create: `src/pricewatch/web/routes_status.py`
- Create: `src/pricewatch/templates/settings.html`
- Create: `src/pricewatch/templates/status.html`
- Create: `tests/integration/test_backups.py`
- Create: `tests/integration/test_settings.py`
- Modify: `src/pricewatch/cli.py`
- Modify: `src/pricewatch/app.py`

**Interfaces:**
- Produces: `BackupService.create(reason: BackupReason) -> BackupRecord` and `verify(path: Path) -> BackupManifest`.
- Produces: `RetentionService.run(now: datetime) -> RetentionReport`.
- Produces routes `/settings`, `/settings/notifications/test`, `/backups/create`, `/backups/{id}/download`, and `/status`.
- Produces CLI `pricewatch backup restore --file PATH --confirm-checksum HEX`.

- [ ] **Step 1: Write failing backup rotation, pre-migration, retention, encrypted-setting, export-redaction, and status tests**

```python
def test_rotation_keeps_seven_daily_and_four_weekly(backup_service, frozen_clock):
    create_daily_backups(backup_service, days=50)
    backup_service.rotate()
    assert len(backup_service.list("daily")) == 7
    assert len(backup_service.list("weekly")) == 4

def test_backup_download_does_not_expose_plaintext_webhook(admin_client, configured_feishu):
    response = admin_client.post("/backups/create", data={"csrf_token": csrf(admin_client)})
    archive = admin_client.get(response.json()["download_url"]).content
    assert configured_feishu.token.encode() not in archive
```

- [ ] **Step 2: Run settings/backup tests and confirm failures**

Run: `.venv/bin/python -m pytest tests/integration/test_backups.py tests/integration/test_settings.py -q`  
Expected: FAIL.

- [ ] **Step 3: Implement atomic SQLite backup, checksum manifest, rotation, retention, and settings/status routes**

Use SQLite's backup API into a temporary file within `/data/backups`, fsync, checksum, then atomic rename. Backups retain encrypted secrets, never plaintext. Restore requires a stopped scheduler, matching checksum, and explicit CLI confirmation; it first preserves the current database. Settings validate the external URL, optional HTTP/SOCKS proxy URL, timezone, approved intervals, and retention bounds before encryption/persistence.

- [ ] **Step 4: Run backup restore round-trip and retention tests**

Run: `.venv/bin/python -m pytest tests/integration/test_backups.py tests/integration/test_settings.py -q`  
Expected: PASS with product history identical after restore.

- [ ] **Step 5: Commit operations features**

```bash
git add src/pricewatch/services/backups.py src/pricewatch/services/retention.py src/pricewatch/web src/pricewatch/templates src/pricewatch/cli.py src/pricewatch/app.py tests/integration
git commit -m "feat: add settings retention and verified backups"
```

### Task 13: Docker, CI, Releases, Documentation, and Final Verification

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml`
- Create: `README.md`
- Create: `LICENSE`
- Complete: `THIRD_PARTY_NOTICES.md`
- Create: `tests/integration/test_container_contract.py`

**Interfaces:**
- Produces OCI image entrypoint that runs migrations/pre-migration backup and starts one Uvicorn worker.
- Produces health endpoint contract used by Docker health checks.
- Produces public GHCR tags `1.0.0`, `1.0`, `1`, and `latest` only for final releases.

- [ ] **Step 1: Write failing container contract tests**

```python
def test_compose_has_single_service_and_data_mount(compose_config):
    assert set(compose_config["services"]) == {"pricewatch"}
    service = compose_config["services"]["pricewatch"]
    assert any(volume.endswith(":/data") for volume in service["volumes"])
    assert service["healthcheck"]["test"][-1] == "http://localhost:8080/healthz"

def test_container_runs_non_root(container_inspect):
    assert container_inspect["Config"]["User"] not in {"", "0", "root"}
```

- [ ] **Step 2: Run contract tests before deployment files exist**

Run: `.venv/bin/python -m pytest tests/integration/test_container_contract.py -q`  
Expected: FAIL.

- [ ] **Step 3: Implement multi-stage image, Compose deployment, migration entrypoint, and health check**

The image installs only the Chromium browser/runtime needed by Playwright, copies locked Python dependencies, runs as an unprivileged UID, exposes 8080, writes only to `/data` and `/tmp`, and starts exactly one worker. Compose uses a numbered image placeholder in documentation and `restart: unless-stopped`.

- [ ] **Step 4: Add CI and release workflows**

CI runs Ruff, mypy, pytest with coverage, Alembic upgrade tests, container build, vulnerability scan, dependency/license inventory, and secret scan. Release runs only on signed/approved version tags, builds `linux/amd64`, attaches an SBOM, and pushes versioned GHCR tags.

- [ ] **Step 5: Write operator documentation**

README includes TrueNAS dataset setup, secure `APP_SECRET_KEY` generation, Compose installation, first-run initialization, reverse-proxy headers/HTTPS, Feishu bot setup, Dell live smoke test, updates, backup, restore, rollback, data retention, troubleshooting, and resource-measurement results.

- [ ] **Step 6: Run the full verification matrix**

Run: `.venv/bin/ruff check src tests`  
Run: `.venv/bin/mypy src/pricewatch`  
Run: `.venv/bin/python -m pytest -q --cov=pricewatch --cov-report=term-missing`  
Run: `.venv/bin/alembic upgrade head`  
Run: `docker compose config`  
Run: `docker build -t pricewatch:test .`  
Run: `docker compose up -d --wait`  
Run: `curl --fail http://127.0.0.1:8080/healthz`  
Run: `docker compose restart pricewatch`  
Run: `.venv/bin/python -m pytest tests/integration/test_container_contract.py -q`  
Expected: every command PASS, health returns `status=ok`, and data survives restart.

- [ ] **Step 7: Perform the deployment-side Dell acceptance check**

On the owner's TrueNAS network, add the supplied Alienware URL, verify the previewed SKU/configuration/price against the browser, confirm the first Feishu message, force an unchanged recheck and verify silence, and record measured image size, idle memory, browser peak memory, and check duration in README.

- [ ] **Step 8: Commit the production packaging**

```bash
git add Dockerfile docker-compose.yml .env.example .github README.md LICENSE THIRD_PARTY_NOTICES.md tests/integration/test_container_contract.py
git commit -m "feat: package PriceWatch for TrueNAS releases"
```

## Completion Gate

Before declaring version 1 complete:

1. Every task's focused tests pass.
2. The full local test, type, lint, migration, and container matrix passes from a clean checkout.
3. No secret or runtime CDN reference is present.
4. License inventory and third-party notices cover every redistributed asset and dependency.
5. The reference Dell URL passes a live TrueNAS-side extraction check.
6. First Feishu notification succeeds, unchanged recheck stays silent, and duplicate event replay sends nothing.
7. A backup/restore round-trip and previous-image rollback procedure are demonstrated.
8. Final implementation review reports no unresolved critical or high-severity findings.
