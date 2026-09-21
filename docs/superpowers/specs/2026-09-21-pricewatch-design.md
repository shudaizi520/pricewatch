# PriceWatch Design Specification

**Status:** Draft for written-spec review  
**Date:** 2026-09-21  
**Target:** TrueNAS, x86-64, Intel Core i3-12100  

## 1. Purpose

PriceWatch is a small, self-hosted product-price monitor for one administrator. Its primary target is Dell's United States store, especially Alienware computers. It accepts product URLs, identifies the product and important hardware configuration, records public price and availability history, and sends Feishu notifications only when something meaningful changes.

The application must be easy to deploy with Docker Compose, pleasant to use behind the owner's reverse proxy, and straightforward to publish and update through a public GitHub repository and GHCR image.

The initial reference product is:

- `https://www.dell.com/en-us/shop/laptop-computers/spd/alienware18area51aa18250/aa18250_reg_01`

## 2. Success Criteria

The first production release succeeds when it can:

1. Initialize one administrator account through the browser, then permanently close public registration.
2. Add the reference Dell US URL and extract its canonical URL, name, SKU/model, major configuration, public price, list price, discount, coupon when clearly present, and stock status.
3. Send one Feishu confirmation after the first trusted extraction.
4. Recheck up to ten products on the default six-hour schedule without concurrent scraping.
5. Send no notification when trusted product state is unchanged.
6. Send a clear Feishu message when price, discount, coupon, or stock changes.
7. Detect an unexpected configuration change and require confirmation instead of comparing unlike configurations.
8. Preserve history, settings, and credentials across container recreation and version upgrades.
9. Provide the approved responsive dashboard in light, dark, and system theme modes.
10. Recover from a failed upgrade by restoring the pre-migration backup and previous versioned image.

## 3. Scope and Constraints

### Included

- Dell US-specific extraction and identity handling.
- Best-effort generic extraction for occasional HP or other public product pages.
- Manual correction of a generic product's display name and configuration after extraction.
- Price and stock history, historical-low calculation, CSV export, and a simple trend chart.
- Feishu custom-bot notifications through a mature notification library.
- First-run admin initialization, login, password change, and terminal-based password reset.
- Docker Compose, local persistent storage, health checks, backups, migrations, tests, and public GHCR releases.

### Excluded from version 1

- Automatic purchasing, cart actions, or retailer-account login.
- CAPTCHA bypass or aggressive anti-bot circumvention.
- Taxes, shipping, location-specific checkout totals, and member-only prices.
- Guaranteed support for arbitrary e-commerce sites.
- Multi-user accounts, roles, public registration, OIDC, or SSO.
- Mobile applications, browser extensions, AI extraction, currency conversion, and complex analytics.
- Redis, PostgreSQL, message queues, Kubernetes, and distributed workers.

## 4. Reuse-First Engineering Policy

PriceWatch should reuse maintained open-source libraries for solved infrastructure concerns and keep custom code focused on Dell product identity, configuration parsing, state comparison, and the approved user experience.

Planned reusable components include:

- **FastAPI** for the web application and internal endpoints.
- **Jinja2 and HTMX** for server-rendered, progressively enhanced pages.
- **SQLAlchemy and Alembic** for SQLite access and schema migrations.
- **APScheduler** for the single-process schedule.
- **HTTPX plus HTML/structured-data parsers** for lightweight acquisition.
- **Playwright Chromium** only as a fallback for dynamic pages.
- **Apprise** for notifications; its Feishu integration accepts the custom-bot token without PriceWatch implementing the protocol from scratch.
- **Argon2** for password hashing and a maintained cryptography library for sensitive settings.
- A locally packaged, maintained chart library for the price trend. No runtime CDN is required.

The following existing applications were evaluated as references:

- [changedetection.io](https://github.com/dgtlmoon/changedetection.io): mature acquisition, scheduling, price/restock, and Apprise patterns.
- [PriceStalker](https://github.com/mikeknight85/PriceStalker): price-candidate selection, retailer adapters, and operational-state patterns.
- [Centsible](https://github.com/aloglu/centsible): compact price-history and backup ideas.
- [devoidx/price-tracker](https://github.com/devoidx/price-tracker): FastAPI/Playwright integration and error visibility ideas.

They will not be used as the application's base because their data models, deployment footprint, or user experience do not fit the approved scope. Source code must not be copied casually. Before adopting code rather than a library, implementation must record the exact source, version/commit, license, attribution requirement, and local changes. CI will produce a dependency/license inventory and reject known incompatible or missing-license dependencies.

## 5. Architecture

PriceWatch is one application container and one persistent `/data` mount.

The process contains six bounded components:

1. **Web UI and application service** — authentication, forms, pages, validation, and orchestration.
2. **Scheduler** — calculates due products and runs at most one check at a time.
3. **Acquisition pipeline** — HTTP-first fetching with optional Chromium fallback.
4. **Site adapters** — Dell US first, then a generic structured-data adapter.
5. **State engine** — configuration fingerprinting, trusted comparisons, history, deduplication, and lifecycle transitions.
6. **Notification service** — renders event messages and delegates delivery to Apprise.

The server runs with one application worker. This prevents duplicate in-process schedulers and is sufficient for fewer than ten products. Static assets are served locally by the application. Redis, external databases, and a separate frontend service are intentionally absent.

## 6. User Interface

The approved visual direction is a restrained, professional dashboard named **PriceWatch**.

### Appearance

- Default theme follows the operating system.
- The administrator can choose light, dark, or system mode.
- The choice is stored with the administrator profile.
- Cards, tables, charts, dialogs, validation states, and login/initialization pages have native dark variants.
- Text remains comfortably readable; technical details are collapsed rather than displayed as dense helper copy.

### Pages

1. **First-run initialization** — create the only administrator username and password.
2. **Login** — username and password with rate-limited failures.
3. **Monitor dashboard** — summary, last/next check, immediate-check action, add-product action, and card/list switch.
4. **Add/confirm product** — paste a URL, show extraction progress, review recognized identity/configuration/price, then confirm monitoring.
5. **Product detail** — current state, major configuration, history chart, historical low, recent changes, errors, edit/pause/archive actions, and immediate check.
6. **Notifications** — Feishu bot configuration, masked secret, test action, and event toggles.
7. **Settings** — timezone, default check interval, theme, retention, external base URL, password change, and backup download.
8. **System status** — concise job health and expandable diagnostics.

The dashboard supports mobile widths but is optimized for desktop administration. It does not mimic a consumer shopping site and does not contain unrelated widgets.

## 7. Product Acquisition and Extraction

### 7.1 URL safety

Only `http` and `https` URLs are accepted. The application rejects credentials embedded in URLs and blocks loopback, link-local, private-network, multicast, and cloud-metadata destinations. DNS is resolved and validated again after every redirect to prevent server-side request forgery. Response size and request duration are bounded.

### 7.2 Acquisition order

Each check uses the cheapest trustworthy method first:

1. Normalized HTTP request with realistic but honest headers and bounded redirects.
2. Parse JSON-LD, OpenGraph/product metadata, embedded application data, and stable HTML fields.
3. If required fields remain unavailable or the page is client-rendered, run Playwright Chromium for that check.
4. If blocked, challenged, ambiguous, or inconsistent, record a failure or `needs_attention`; do not invent a result.

Chromium is closed after each fallback check. Checks are sequential and receive small bounded jitter. Retries use backoff and do not repeatedly hammer a blocked site.

### 7.3 Adapter contract

An adapter receives the requested URL and acquired page representation. It returns a typed result containing:

- source site and canonical URL;
- product name, SKU/model, and optional image;
- normalized configuration fields and human-readable summary;
- configuration fingerprint;
- currency, current public price, optional list price, and discount;
- clearly displayed coupon text, if any;
- availability state;
- evidence source for each important value;
- confidence and warnings.

Monetary values are stored as integer minor units with an ISO currency code. Raw untrusted page text is never used directly as HTML.

### 7.4 Dell US adapter

The Dell adapter recognizes the Dell US product URL family and attempts to extract:

- Dell SKU/configuration code and canonical product family;
- CPU, GPU, memory, storage, display, operating system, and other stable major options;
- public current price, struck-through/list price, displayed discount, coupon, and stock status.

Identity is based primarily on site, SKU/model, and a normalized configuration fingerprint rather than the URL string alone.

When Dell redirects to a new canonical URL, PriceWatch updates the stored URL after a successful identity match. If the saved page disappears, PriceWatch may perform one bounded Dell-site lookup using the saved SKU. It updates automatically only when the match is unique and the configuration is consistent. Otherwise it marks the product `needs_attention`.

### 7.5 Generic adapter

The generic adapter uses structured product metadata and well-supported price hints. It is allowed to return partial configuration. The administrator can correct the display name and configuration summary, but version 1 does not expose raw CSS-selector programming in the normal UI. Extraction ambiguity is shown for confirmation instead of silently selecting a suspicious number.

## 8. Trusted State and Change Detection

A check result is trusted only when identity, currency, price format, and evidence pass validation. Failed or untrusted checks never overwrite the last trusted state.

Meaningful event types are:

- initial trusted observation;
- public price increase or decrease;
- list price or displayed discount change;
- coupon added, changed, or removed;
- stock transition;
- unexpected configuration change;
- three consecutive failures;
- recovery after a reported failure.

Configuration changes pause ordinary price comparison until the administrator confirms whether the product should adopt the new configuration or be treated as a separate product. Notification events have deterministic idempotency keys so retries cannot send the same message twice.

Unchanged checks update health timestamps. To control database growth, PriceWatch retains meaningful changes plus at most one unchanged daily checkpoint per active product.

## 9. Scheduling

The default timezone is `Asia/Shanghai`. The default six-hour check slots are 04:00, 10:00, 16:00, and 22:00 Beijing time, preserving the requested 10:00 morning check.

Adding a product triggers an immediate initial check. The administrator may choose a simple 1, 3, 6, 12, or 24-hour interval per product. Arbitrary cron syntax is excluded from version 1.

Only one product check runs at a time. On restart, the scheduler resumes safely without launching a burst of duplicate overdue jobs. Manual and scheduled requests for the same product coalesce.

## 10. Notifications

Feishu uses a group custom-bot webhook represented internally through Apprise. The UI accepts the normal Feishu webhook and stores only the required token in encrypted form. It provides a test-message action.

Default behavior:

- Send one confirmation after the product's first trusted observation.
- Send only meaningful changes after that; unchanged checks remain silent.
- Send one failure alert after three consecutive failures, not on every failure.
- Send one recovery message when the product becomes healthy again.
- Permit per-product notification disablement and an optional target-price rule.

Messages contain the product name, compact major configuration, old and new values, amount/percentage change where applicable, stock state, timestamp in Beijing time, and product link. Secrets, raw cookies, and diagnostic HTML are never included.

Apprise remains behind an internal notification interface so PriceWatch can add other supported destinations later without changing the state engine or product model.

## 11. Data Model

SQLite stores the following logical records:

- **Administrator** — username, password hash, theme, and timestamps.
- **Application settings** — timezone, schedule defaults, external URL, encrypted notification configuration, and retention policy.
- **Product** — requested/canonical URLs, site, stable identity, display data, current configuration, status, schedule, notification policy, and health counters.
- **Observation** — trusted price/list-price/discount/coupon/stock/configuration snapshot, evidence summary, and timestamp.
- **Check run** — bounded operational result, duration, acquisition method, warning/error category, and timestamp.
- **Notification delivery** — event identity, destination type, delivery status, attempts, and timestamp.
- **Backup metadata** — file name, reason, schema version, checksum, and timestamp.

SQLite runs in WAL mode. Database access is transactional, and migrations are versioned.

## 12. Retention and Backup

Default retention is:

- products and price history: indefinite until deliberate deletion;
- archived products: indefinite;
- notification delivery records: 90 days;
- check-run diagnostics: 30 days;
- failed-page fragments and screenshots: 7 days with a total-space cap;
- expired sessions and temporary files: removed automatically.

Deleting a monitored product offers two explicit actions: archive it while preserving history, or permanently delete it with its history.

The application creates one database backup each day, retains seven daily and four weekly backups, and creates an additional pre-migration backup before schema changes. The UI can create and download a backup. Restoration remains a documented terminal operation to avoid accidental destructive replacement. The entire `/data` dataset is compatible with TrueNAS snapshots.

## 13. Authentication and Security

- The first browser visit creates the single administrator; after that, initialization and registration routes close.
- Passwords use Argon2 with maintained parameters and are never logged.
- Password reset is an explicit container command executed from the TrueNAS terminal.
- Sessions use signed, HTTP-only, same-site cookies. Secure-cookie behavior is enabled for the configured HTTPS external URL.
- State-changing requests require CSRF protection.
- Login failures are rate-limited without creating a permanent denial-of-service lockout.
- Proxy headers are trusted only according to explicit deployment configuration.
- The Feishu token and any future proxy secret are encrypted using a required `APP_SECRET_KEY` stored in the local deployment environment, outside the database. Installation documentation provides a one-command secure generator and startup rejects missing or weak values.
- Logs redact credentials, authorization headers, cookies, webhook tokens, and query secrets.
- The container runs as a non-root user with only `/data` writable.

## 14. Deployment, Releases, and Upgrades

The repository includes:

- `docker-compose.yml` using a versioned public GHCR image;
- `.env.example` with safe explanations and no secrets;
- a multi-stage Dockerfile;
- a persistent `/data` mount;
- an HTTP health check;
- TrueNAS installation, reverse-proxy, backup, restore, and upgrade documentation.

The public GitHub repository is named `pricewatch`. GitHub Actions runs tests and security/license checks, then publishes versioned `linux/amd64` images under the user's GitHub Container Registry namespace, for example `ghcr.io/GITHUB_ACCOUNT/pricewatch:1.0.0`. `latest` may exist for convenience, but the documented TrueNAS deployment pins a numbered release.

An upgrade pulls the chosen image and recreates the application container. Startup verifies the data directory, creates a pre-migration backup when necessary, applies forward migrations, and exposes healthy status only after initialization. Rollback uses the previous image plus the corresponding pre-migration backup when a migration is not backward-compatible.

Automatic self-updating is excluded. The owner updates manually or uses an existing container-update tool after reviewing release notes.

## 15. Resource Control

- One application process and at most one active product check.
- HTTP parsing is the normal path; Chromium starts only when required and exits afterward.
- Browser context, response, download, and screenshot sizes are bounded.
- Build stages remove compilers, test tools, caches, and unused browser packages from the runtime image.
- No runtime CDN or external database is required.
- Actual compressed image size, idle memory, browser-fallback peak memory, and check duration will be measured on the release candidate and documented rather than guessed.

## 16. Error Handling and Observability

Errors are categorized as network, redirect/URL safety, HTTP block, browser challenge, extraction ambiguity, identity mismatch, configuration change, notification failure, storage failure, or internal failure.

The dashboard shows last success, next check, current health, and a short actionable message. Full diagnostic details are available only in the status or product-detail view. Errors never expose secrets. Persistent failures do not destroy the last trusted price.

## 17. Verification Strategy

Automated coverage includes:

- unit tests for Dell and generic adapters using versioned, sanitized fixtures;
- price/currency parsing and configuration normalization tests;
- state-transition, idempotency, retention, and scheduler tests;
- authentication, CSRF, rate-limit, SSRF, secret-redaction, and authorization tests;
- SQLite migration, backup, restore, and rollback tests;
- mocked Apprise/Feishu delivery tests;
- Playwright UI tests for initialization, login, add/confirm, dashboard, product detail, settings, light/dark/system themes, and mobile layout;
- container health and persistent-volume integration tests.

CI does not repeatedly scrape Dell. A live Dell smoke test is opt-in and rate-limited because retailer availability and bot protection are external conditions. Final acceptance includes a deployment-side check from the owner's TrueNAS network against the supplied Dell URL.

## 18. Known Risks and Mitigations

- **Retailer markup or API changes:** isolate site adapters, store evidence categories, use fixtures, and fail closed.
- **Bot protection:** prefer low-frequency HTTP access, use a browser fallback, support an optional proxy, and never claim a challenge was a valid product result.
- **Wrong price among many candidates:** validate context and structured data; request confirmation when ambiguous.
- **Link or configuration drift:** persist SKU and configuration fingerprints; pause comparison on mismatch.
- **Duplicate notifications:** deterministic event IDs and persisted delivery state.
- **Public exposure through reverse proxy:** first-run closure, strong password hashing, secure cookies, CSRF, rate limiting, and explicit proxy configuration.
- **Dependency or copied-code risk:** pin dependencies, scan licenses/security, preserve attribution, and prefer public APIs over copied implementations.

## 19. Final Design Decision

Build PriceWatch as a focused single-container application rather than deploying or forking a larger generic tracker. Reuse mature libraries—especially Apprise, Playwright, FastAPI, SQLAlchemy/Alembic, APScheduler, and HTMX—while keeping custom logic limited to the approved Dell-first product model, trusted change detection, and simple administration experience.
