# PriceWatch Configurations and Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Monitor separate Dell configurations from one model URL with verified prices, and complete the dashboard management and history UI.

**Architecture:** A Dell-only selection recipe is persisted per product, reapplied by the guarded browser fetcher, and checked against the resulting selected options before price comparison. Existing products without recipes keep the current default-page path. Server-rendered pages expose the structured configuration, individual prices and history, and explicit archive/delete controls.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLAlchemy/Alembic, SQLite, Playwright Chromium, vanilla JS/CSS, pytest.

**Spec:** `docs/superpowers/specs/2026-09-21-pricewatch-configurations-and-dashboard-design.md`

## Global Constraints

- The application remains one container, one administrator, at most ten monitored cards, and a persistent `/data` mount.
- Existing default-configuration and generic-retailer products continue working without a selection recipe.
- No guessed price, option-delta arithmetic, retailer login, cart action, or owner-supplied cookie.
- Mismatched or unavailable selections never overwrite a trusted observation.
- Preserve existing cards and history through migration and deployment; price history is removed only by deliberate permanent deletion.
- Run source checks and live NAS smoke before claiming configured-price success.

## Review Focus

1. Two cards with the same Dell URL but different GPU selections must not share history or latest price (Tasks 3–4).
2. A stale Dell JSON-LD default price must not replace the active configured buy-box price (Task 2).
3. A missing, renamed, or ambiguous option must fail before recording a price (Task 2).
4. A CNY card selection must show CNY in the dashboard summary, with no USD conversion (Task 5).
5. An archived product must remain recoverable and permanently deletable without affecting other cards (Task 6).

---

## File map

- `src/pricewatch/db/models.py`, `alembic/versions/0002_dell_selection.py`: optional per-product recipe and migration.
- `src/pricewatch/domain/products.py`, `src/pricewatch/web/configuration.py`: structured configuration persistence and legacy display rows.
- `src/pricewatch/fetching/dell_options.py`, `src/pricewatch/fetching/types.py`: Dell option catalog, requested-choice application, and typed settled-price evidence.
- `src/pricewatch/fetching/browser.py`, `src/pricewatch/adapters/dell_us.py`: guarded browser integration and configured Dell snapshot extraction.
- `src/pricewatch/services/checks.py`, `src/pricewatch/web/routes_products.py`: recipe-aware checks, preview/confirm, duplicate handling, archived management.
- `src/pricewatch/templates/{dashboard,product_add,product_detail,product_delete,base,login,initialize}.html`, `src/pricewatch/static/{app.css,app.js,mark.svg}`: compact presentation and interactions.
- Unit, integration, and E2E tests under `tests/` own every behavior before its implementation.

### Task 1: Persist recipes and structured configuration

**Interfaces:** Produce `Product.dell_selection: dict[str, str] | None`, `ProductConfiguration.as_record() -> dict[str, object]`, and `configuration_rows(record: dict | None) -> list[tuple[str, str]]`. Later tasks consume these interfaces.

- [ ] **Step 1: Write failing model and rendering tests.** In `tests/unit/test_product_domain.py` assert `ProductConfiguration(cpu="Core Ultra 9", gpu="RTX 5090").as_record()` contains `cpu`, `gpu`, and `summary`. In `tests/unit/test_configuration_rows.py` assert structured records produce labeled rows and a legacy `{"summary": "Core Ultra 9 · RTX 5090"}` produces two nonempty rows. In `tests/unit/test_database.py` assert a `Product(dell_selection={"Graphics Card": "RTX 5090"})` survives commit/reload.

```python
assert ProductConfiguration(cpu="Core Ultra 9", gpu="RTX 5090").as_record()["gpu"] == "RTX 5090"
assert [value for _, value in configuration_rows({"summary": "CPU · GPU"})] == ["CPU", "GPU"]
```

- [ ] **Step 2: Run those tests and confirm they fail** for missing interfaces: `.venv/bin/python -m pytest -q tests/unit/test_product_domain.py tests/unit/test_configuration_rows.py tests/unit/test_database.py`.
- [ ] **Step 3: Implement the model, migration, and helpers.** Add nullable JSON `dell_selection` to `Product`; Alembic revision `0002_dell_selection.py` adds/drops only that column. `as_record()` serializes the existing six named fields and `extras` while retaining the human summary. `configuration_rows()` prefers named fields and falls back to splitting old summaries on ` · `; never renders HTML markup.

```python
def as_record(self) -> dict[str, object]:
    fields = ("cpu", "gpu", "memory", "storage", "display", "os")
    return {**{key: value for key in fields if (value := getattr(self, key))},
            "extras": self.extras, "summary": self.summary()}
```

- [ ] **Step 4: Rerun the focused tests and Alembic upgrade test; commit.** `git commit -m "feat: persist Dell recipes and structured configuration"`.

### Task 2: Apply Dell choices and read the actual configured offer

**Interfaces:** Produce `ConfiguredOffer(selected: dict[str, str], price_minor: int)` in `fetching/types.py`; `read_catalog(page: Page) -> dict[str, list[str]]` and `apply_selection(page: Page, recipe: dict[str, str]) -> ConfiguredOffer` in `fetching/dell_options.py`. `AcquiredPage.configured_offer: ConfiguredOffer | None = None` carries evidence to `DellUsAdapter`, and `BrowserFetcher.fetch(..., dell_selection: dict[str, str] | None = None)` supplies it.

- [ ] **Step 1: Capture a sanitized live DOM fixture, read-only.** From a disposable browser session using the existing Dell guarded proxy, inspect the actual option-group containers, selected indicators, and purchase-price node before/after clicking RTX 5090. Save only public markup and price text to `tests/fixtures/dell/options.html`; no cookies, scripts with tokens, headers, or user data. Record whether Dell updates JSON-LD along with the visible offer. If the page cannot verify an active selection and matching final price, stop rather than inventing selectors.
- [ ] **Step 2: Write failing tests** in `tests/unit/test_dell_options.py` for group discovery, selecting a requested option, missing/ambiguous labels, stale default JSON-LD, and stable USD price. The web fixture must include a default RTX 5070 buy-box and a selected RTX 5090 buy-box so the expected configured price is unambiguous.

```python
assert catalog["Graphics Card"] == ["RTX 5070", "RTX 5090"]
assert configured.selected["Graphics Card"] == "RTX 5090"
assert configured.price_minor == 509999  # fixture's active offer, not its default JSON-LD
```

- [ ] **Step 3: Run tests to red.** `.venv/bin/python -m pytest -q tests/unit/test_dell_options.py tests/unit/test_dell_adapter.py tests/unit/test_browser_fetcher.py`.
- [ ] **Step 4: Implement the catalog, exact matching, click, and settled active-price check** against the verified DOM structure. Require each requested group/label to match exactly one current option. Re-read all selected indicators after clicks. Parse the active purchase price only when USD, nonnegative, and stable across two post-update reads; configured-price extraction does not use stale static metadata. Retain the existing default adapter path and the guarded SOCKS proxy/URL checks.

```python
async def apply_selection(page: Page, recipe: dict[str, str]) -> ConfiguredOffer:
    for group, label in recipe.items():
        option = await find_unique_option(page, group, label)
        await option.click()
        await wait_until_selected(page, group, label)
    return await read_settled_offer(page, recipe)
```

- [ ] **Step 5: Rerun focused tests, type/lint checks, and commit.** `git commit -m "feat: verify configured Dell purchase prices"`.

### Task 3: Choose and preview configurations in the add flow

**Interfaces:** `POST /products/options` loads a short-lived Dell catalog; `POST /products/preview` accepts the chosen recipe; `POST /products/confirm` persists the previewed recipe. Generic and default URL previews remain valid.

- [ ] **Step 1: Write failing integration tests** in `tests/integration/test_product_web.py`: a model URL displays selectable GPU choices; selecting RTX 5090 yields the confirmed RTX 5090 price and recipe; an invalid selection returns 422 without creating a product; an identical recipe links to the existing card while a distinct recipe creates another card.

```python
assert (await admin_client.post("/products/preview", data=chosen)).status_code == 200
assert "RTX 5090" in (await admin_client.get("/products/new")).text
assert stored.dell_selection == {"Graphics Card": "RTX 5090"}
```

- [ ] **Step 2: Run the new tests to red.** `.venv/bin/python -m pytest -q tests/integration/test_product_web.py`.
- [ ] **Step 3: Add the small Dell option chooser** to `product_add.html`, and a short-lived server-side catalog token tied to the authenticated administrator. Preview re-fetches/reapplies choices, validates them, and stores both snapshot and recipe in the 15-minute preview entry. Confirmation creates the product only after a successful preview; duplicate detection uses normalized URL family plus exact recipe, not URL alone.

```python
preview_entry = (admin.id, snapshot, recipe, datetime.now(UTC) + timedelta(minutes=15))
if existing_product_for_recipe(session, snapshot.identity.site, target, recipe):
    return RedirectResponse(f"/products/{existing.id}", status_code=303)
```

- [ ] **Step 4: Rerun preview tests and commit.** `git commit -m "feat: choose Dell configuration during product preview"`.

### Task 4: Check each saved card independently

**Interfaces:** `AcquisitionPipeline.acquire(..., dell_selection=None)` passes the recipe to the browser path; `CheckService.check_product()` supplies `product.dell_selection`. Existing no-recipe calls still work.

- [ ] **Step 1: Write failing integration/state tests** in `tests/integration/test_checks.py` and `tests/unit/test_state_engine.py` for same-URL cards with separate RTX 5070/5090 prices and histories; wrong resulting GPU must leave the prior trusted price unchanged and put that card into attention/failure; unchanged checks remain silent.

```python
assert latest_price(session, card_5070.id) != latest_price(session, card_5090.id)
assert card_5090.status == "needs_attention"
assert latest_price(session, card_5090.id) == trusted_before_mismatch
```

- [ ] **Step 2: Run tests to red.** `.venv/bin/python -m pytest -q tests/integration/test_checks.py tests/unit/test_state_engine.py`.
- [ ] **Step 3: Thread the recipe through scheduler/acquisition/check service.** A browser must be used for a configured Dell check even if the lightweight HTTP fetch returns a default-page snapshot. Save structured configuration with each trusted and pending observation. Compare the fully selected option set and configuration fingerprint before price events. Never fall back to a default-price snapshot after configured browser failure.

```python
if product.dell_selection:
    _, snapshot = await pipeline.acquire(url, adapter, dell_selection=product.dell_selection)
else:
    _, snapshot = await pipeline.acquire(url, adapter)
```

- [ ] **Step 4: Rerun focused tests and commit.** `git commit -m "feat: keep same-URL Dell cards price-isolated"`.

### Task 5: Card selection, readable configurations, and price chart

**Interfaces:** Dashboard card articles expose `data-product-id`, `data-price`, and `data-currency`; each contains a keyboard-operable select button and a separate detail link. `app.js` updates the selected-price summary. `product_detail.html` receives chart points from trusted observations only.

- [ ] **Step 1: Write failing UI tests** in `tests/e2e/test_dashboard.py` and `tests/integration/test_product_web.py` for labeled config rows, a CNY card selected after a USD card, one-point chart visibility, and a two-point chart line with correct chronological ordering.

```python
assert "显卡" in dashboard_html and "RTX 5090" in dashboard_html
assert 'data-currency="CNY"' in dashboard_html
assert 'aria-label="价格走势图"' in detail_html
```

- [ ] **Step 2: Run tests to red.** `.venv/bin/python -m pytest -q tests/e2e/test_dashboard.py tests/integration/test_product_web.py`.
- [ ] **Step 3: Update dashboard/add/detail templates, CSS, and JS.** Replace the global USD minimum stat with “所选商品价格”; each card's button selects without navigation, while “查看详情” is an explicit sibling link. Use `configuration_rows()` for separate labeled lines. Render a compact SVG chart with zero-point empty state, one visible point, or multiple time-ordered points; include readable dates/prices, keep the table and CSV.

```javascript
function selectCard(button) {
  const card = button.closest('[data-product-id]');
  document.querySelector('#selected-price').textContent = card.dataset.price || '—';
  document.querySelector('#selected-currency').textContent = card.dataset.currency || '';
  document.querySelectorAll('.card-select').forEach(node =>
    node.setAttribute('aria-pressed', String(node === button)));
}
```

- [ ] **Step 4: Rerun tests at desktop/mobile widths and commit.** `git commit -m "feat: show per-card prices and real history chart"`.

### Task 6: Visible removal, archive recovery, and browser icon

**Interfaces:** `GET /products/archived` lists archived cards; `POST /products/{id}/restore` returns one to paused monitoring; existing permanent-delete confirmation is available from that list. `/static/mark.svg` is used on every page.

- [ ] **Step 1: Write failing web tests** in `tests/integration/test_product_web.py`: active card has visible remove action; archive removes it from active cards but leaves history and shows it under archived; restore does not trigger an immediate unapproved check; permanent delete requires CSRF and typed confirmation and does not delete a different card; login, initialization, and dashboard link the favicon.

```python
assert "已归档" in (await admin_client.get("/products/archived")).text
assert "mark.svg" in (await admin_client.get("/")).text
assert (await admin_client.post(f"/products/{id}/delete", data={"confirmation": "wrong", "csrf_token": token})).status_code == 400
```

- [ ] **Step 2: Run tests to red.** `.venv/bin/python -m pytest -q tests/integration/test_product_web.py tests/integration/test_web_flows.py`.
- [ ] **Step 3: Add the archive list/restore route, visible buttons, and locally served SVG mark.** Preserve the existing two-step archive then typed permanent-delete path. Update header and auth pages to use the same mark. Ensure archived-card deletion cannot be reached by an accidental card click.

```python
@router.post("/products/{product_id}/restore")
async def restore_product(request: Request, product_id: int, submitted_csrf: str = Form(alias="csrf_token")) -> Response:
    verify_csrf(request, submitted_csrf)
    with request.app.state.session_factory.begin() as session:
        product = session.get(Product, product_id)
        if product is None or product.status != "archived":
            raise HTTPException(409)
        product.status = "paused"
    return RedirectResponse(f"/products/{product_id}", status_code=303)
```

- [ ] **Step 4: Rerun web tests and commit.** `git commit -m "feat: expose archive management and PriceWatch icon"`.

### Task 7: Full verification and careful NAS rollout

**Interfaces:** No new product interface; prove the completed path works against the live public Dell site and existing NAS data remains intact.

- [ ] **Step 1: Run all local checks.** `.venv/bin/ruff check src tests alembic`, `.venv/bin/mypy src/pricewatch`, `.venv/bin/python -m pytest -q`, and `git diff --check`; fix failures using a failing-test/red-green cycle.
- [ ] **Step 2: Build and smoke the container** using the repository Dockerfile; verify a default Dell link and a configured RTX 5090 selection in disposable state. Keep any source-derived DOM fixture public and free of secrets.
- [ ] **Step 3: Push the branch and wait for GitHub CI.** Update the existing PriceWatch PR; do not merge it without the owner's integration choice.
- [ ] **Step 4: Back up the NAS app data before migration.** Confirm backup checksum and preserve existing `Product` and `Observation` counts. Deploy the tested image/code to the existing `pricewatch` app without deleting or replacing `/data`.
- [ ] **Step 5: Verify live service.** Check `/healthz`, existing cards, one same-URL distinct configuration in disposable state, chart/archived/favicon responses, and the two configured Dell USD prices. Report what was verified and what still requires an authenticated browser action, without claiming a Feishu delivery unless actually observed.
