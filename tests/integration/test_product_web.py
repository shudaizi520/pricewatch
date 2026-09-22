import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from pricewatch.db.models import Observation, Product, Setting
from pricewatch.domain.products import Money, ProductConfiguration, ProductIdentity, ProductSnapshot
from pricewatch.fetching.http import AcquisitionError


def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
async def admin_client(client):
    page = await client.get("/initialize")
    response = await client.post(
        "/initialize",
        data={
            "username": "owner",
            "password": "VeryStrongSecret123!",
            "csrf_token": csrf(page.text),
        },
    )
    assert response.status_code == 303
    return client


@pytest.mark.anyio
async def test_dashboard_needs_login(client):
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.anyio
async def test_dashboard_uses_content_versioned_card_styles(admin_client):
    page = BeautifulSoup((await admin_client.get("/")).text, "lxml")
    stylesheet = page.select_one('link[href^="/static/cards.css"]')
    assert stylesheet is not None
    href = stylesheet["href"]
    css = Path(__file__).parents[2] / "src" / "pricewatch" / "static" / "cards.css"
    assert href == f"/static/cards.css?v={sha256(css.read_bytes()).hexdigest()[:12]}"


@pytest.mark.anyio
async def test_dashboard_uses_content_versioned_interaction_script(admin_client):
    page = BeautifulSoup((await admin_client.get("/")).text, "lxml")
    script = page.select_one('script[src^="/static/app.js"]')
    assert script is not None
    source = Path(__file__).parents[2] / "src" / "pricewatch" / "static" / "app.js"
    assert script["src"] == f"/static/app.js?v={sha256(source.read_bytes()).hexdigest()[:12]}"


@pytest.mark.anyio
async def test_card_keeps_key_configuration_visible_and_folds_other_options(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        session.add(
            Product(
                source_site="dell-us",
                requested_url="https://www.dell.com/en-us/shop/x",
                name="Alienware 18",
                configuration={
                    "cpu": "Ultra 9",
                    "gpu": "RTX 5090",
                    "extras": {
                        "Keyboard": "CherryMX",
                        "Wireless": "Killer Wi-Fi 7",
                        "Operating System Languages": "English, French",
                        "Documentation": "No Documentation",
                    },
                },
            )
        )
    page = BeautifulSoup((await admin_client.get("/")).text, "lxml")
    card = page.select_one(".product-card")
    primary = card.select_one(".card-select")
    folded = card.select_one("details.secondary-config")
    assert "Ultra 9" in primary.get_text(" ", strip=True)
    assert "RTX 5090" in primary.get_text(" ", strip=True)
    assert "CherryMX" in primary.get_text(" ", strip=True)
    assert "Killer Wi-Fi 7" not in primary.get_text(" ", strip=True)
    assert folded is not None and not folded.has_attr("open")
    assert "Killer Wi-Fi 7" in folded.get_text(" ", strip=True)
    assert "English, French" in folded.get_text(" ", strip=True)
    assert "No Documentation" in folded.get_text(" ", strip=True)


@pytest.mark.anyio
async def test_expanded_extra_labels_wrap_without_overlapping_values(admin_client):
    from playwright.async_api import async_playwright

    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        session.add(
            Product(
                source_site="dell-us",
                requested_url="https://www.dell.com/en-us/shop/x",
                name="Alienware 18 Area-51 Gaming Laptop",
                configuration={
                    "cpu": "Intel Core Ultra 9 processor 290HX Plus",
                    "gpu": "NVIDIA GeForce RTX 5090",
                    "extras": {
                        "Keyboard": "CherryMX per-key AlienFX RGB keyboard",
                        "Operating System Languages": "English, French, Spanish",
                        "Documentation": "Regular - No Documentation",
                    },
                },
            )
        )
    markup = (await admin_client.get("/")).text
    css = Path(__file__).parents[2] / "src" / "pricewatch" / "static" / "app.css"
    card_css = css.with_name("cards.css")
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as error:
            if "error while loading shared libraries" in str(error):
                pytest.skip("Local Chromium system libraries are unavailable")
            raise
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.set_content(markup, wait_until="domcontentloaded")
            await page.add_style_tag(path=str(css))
            if card_css.exists():
                await page.add_style_tag(path=str(card_css))
            await page.locator(".product-grid").evaluate("element => element.dataset.view = 'list'")
            await page.locator(".secondary-config").evaluate("element => element.open = true")
            label = page.locator(".secondary-config dt", has_text="Operating System Languages")
            assert await label.evaluate("element => element.scrollWidth <= element.clientWidth")
            title = page.locator(".card-select h3")
            primary = page.locator(".card-select .config-rows")
            price = page.locator(".card-select .price-block")
            assert await title.evaluate("element => element.scrollWidth <= element.clientWidth")
            assert await primary.evaluate("element => element.scrollWidth <= element.clientWidth")
            title_box = await title.bounding_box()
            primary_box = await primary.bounding_box()
            price_box = await price.bounding_box()
            assert title_box["x"] + title_box["width"] <= primary_box["x"]
            assert primary_box["x"] + primary_box["width"] <= price_box["x"]
        finally:
            await browser.close()


@pytest.mark.anyio
async def test_status_shows_safe_http_failure_detail_after_check(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(
            source_site="www.dell.com",
            requested_url="https://www.dell.com/zh-cn/shop/spd/example",
            name="Alienware 18",
            status="active",
        )
        session.add(product)
        session.flush()
        product_id = product.id

    class DeniedPipeline:
        async def acquire(self, _url, _adapter):
            raise AcquisitionError("普通请求和浏览器都被网站拒绝 (HTTP 403)", status_code=403)

    app.state.check_service.pipeline = DeniedPipeline()
    await app.state.check_service.check_product(product_id, "manual")
    response = await admin_client.get("/status")
    assert response.status_code == 200
    assert "HTTP 403" in response.text
    assert "北京时间" in response.text
    assert "UTC" not in response.text


@pytest.mark.anyio
async def test_status_shows_beijing_time_for_utc_check(admin_client):
    from pricewatch.db.models import CheckRun

    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(product)
        session.flush()
        session.add(
            CheckRun(
                product_id=product.id,
                trigger="scheduled",
                outcome="ok",
                created_at=datetime(2026, 9, 21, 14, 9, tzinfo=UTC),
            )
        )
    response = await admin_client.get("/status")
    assert "2026-09-21 22:09" in response.text
    assert "北京时间" in response.text


@pytest.mark.anyio
async def test_card_manual_refresh_works_without_auto_schedule(admin_client, monkeypatch):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(
            source_site="dell-us", requested_url="https://www.dell.com/x", status="paused"
        )
        session.add(product)
        session.flush()
        product_id = product.id
    queued = []
    monkeypatch.setattr(
        app.state.scheduler,
        "request_check",
        lambda product_id, trigger: queued.append((product_id, trigger)),
    )
    page = await admin_client.get("/")
    assert 'aria-label="刷新全部价格"' not in page.text
    assert f'action="/products/{product_id}/check"' in page.text
    removed = await admin_client.post("/refresh-all", data={"csrf_token": csrf(page.text)})
    assert removed.status_code == 404
    response = await admin_client.post(
        f"/products/{product_id}/check", data={"csrf_token": csrf(page.text)}
    )
    assert response.status_code == 303
    assert queued == [(product_id, "manual")]


@pytest.mark.anyio
async def test_preview_does_not_create_product_until_confirm(admin_client, monkeypatch):
    app = admin_client._transport.app
    from pricewatch.fetching.safety import validate_public_url
    from pricewatch.web import routes_products

    monkeypatch.setattr(
        routes_products,
        "validate_public_url",
        lambda address: validate_public_url(address, lambda host: ["8.8.8.8"]),
    )

    class FakePipeline:
        async def acquire(self, url, adapter):
            return None, ProductSnapshot(
                ProductIdentity("dell-us", "aa18250_reg_01"),
                str(url),
                "Alienware 18 Area-51",
                ProductConfiguration(gpu="RTX 5090"),
                Money("USD", 299999),
                availability="in_stock",
            )

    app.state.check_service.pipeline = FakePipeline()
    page = await admin_client.get("/products/new")
    preview = await admin_client.post(
        "/products/preview",
        data={
            "url": "https://www.dell.com/en-us/shop/spd/alienware/aa18250_reg_01",
            "csrf_token": csrf(page.text),
        },
    )
    assert preview.status_code == 200
    assert "Alienware 18 Area-51" in preview.text
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count(Product.id))) == 0
    token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)
    confirmed = await admin_client.post(
        "/products/confirm",
        data={
            "preview_token": token,
            "csrf_token": csrf(preview.text),
        },
    )
    assert confirmed.status_code == 303
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count(Product.id))) == 1
        saved = session.scalar(select(Product))
        assert saved.status == "paused"
        assert saved.next_check_at is None


@pytest.mark.anyio
async def test_same_url_can_create_distinct_selected_configurations(admin_client, monkeypatch):
    app = admin_client._transport.app
    from pricewatch.fetching.safety import validate_public_url
    from pricewatch.web import routes_products

    monkeypatch.setattr(
        routes_products,
        "validate_public_url",
        lambda address: validate_public_url(address, lambda host: ["8.8.8.8"]),
    )
    address = "https://www.dell.com/en-us/shop/cty/spd/alienware18area51aa18250"

    class FakeBrowser:
        async def fetch(self, _url):
            from pricewatch.fetching.types import AcquiredPage

            return AcquiredPage(
                address,
                address,
                200,
                (Path(__file__).parents[1] / "fixtures" / "dell" / "options.html").read_bytes(),
                {},
                "browser",
                datetime.now(UTC),
            )

    class FakePipeline:
        browser = FakeBrowser()

        async def acquire(self, url, _adapter, dell_selection=None):
            gpu = (dell_selection or {}).get("Graphics Card", "RTX 5070")
            price = 509999 if "5090" in gpu else 399999
            return None, ProductSnapshot(
                ProductIdentity("dell-us", "aa18250_reg_01"),
                str(url),
                "Alienware 18 Area-51",
                ProductConfiguration(cpu="Core Ultra 9", gpu=gpu),
                Money("USD", price),
            )

    app.state.check_service.pipeline = FakePipeline()
    page = await admin_client.get("/products/new")
    options = await admin_client.post(
        "/products/options", data={"url": address, "csrf_token": csrf(page.text)}
    )
    assert options.status_code == 200
    assert "5090" in options.text
    token = re.search(r'name="catalog_token" value="([^"]+)"', options.text).group(1)
    for gpu, expected in [
        ("NVIDIA® GeForce RTX™ 5070 8 GB GDDR7", 399999),
        ("NVIDIA® GeForce RTX™ 5090 24 GB GDDR7", 509999),
    ]:
        preview = await admin_client.post(
            "/products/preview",
            data={
                "url": address,
                "catalog_token": token,
                "option__Graphics Card": gpu,
                "csrf_token": csrf(options.text),
            },
        )
        assert preview.status_code == 200
        assert f"${expected / 100:.2f}" in preview.text
        preview_token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)
        confirmed = await admin_client.post(
            "/products/confirm",
            data={"preview_token": preview_token, "csrf_token": csrf(preview.text)},
        )
        assert confirmed.status_code == 303
    with app.state.session_factory() as session:
        cards = session.scalars(select(Product).order_by(Product.id)).all()
        assert len(cards) == 2
        assert "5070" in cards[0].dell_selection["Graphics Card"]
        assert "5090" in cards[1].dell_selection["Graphics Card"]


@pytest.mark.anyio
async def test_keyboard_options_are_visible_and_saved_with_card(admin_client, monkeypatch):
    from pricewatch.fetching.safety import validate_public_url
    from pricewatch.fetching.types import AcquiredPage
    from pricewatch.web import routes_products

    address = "https://www.dell.com/en-us/shop/cty/spd/alienware18area51aa18250"
    keyboard = "English US CherryMX ultra low-profile mechanical keyboard"
    monkeypatch.setattr(
        routes_products,
        "validate_public_url",
        lambda url: validate_public_url(url, lambda _host: ["8.8.8.8"]),
    )
    monkeypatch.setattr(
        routes_products,
        "catalog_from_html",
        lambda _html: {"Keyboard": [keyboard, "English US keyboard"]},
    )

    class Browser:
        async def fetch(self, _url):
            return AcquiredPage(
                address, address, 200, b"<html></html>", {}, "browser", datetime.now(UTC)
            )

    class Pipeline:
        browser = Browser()

        async def acquire(self, url, _adapter, dell_selection=None):
            assert dell_selection == {"Keyboard": keyboard}
            return None, ProductSnapshot(
                ProductIdentity("dell-us", "aa18250_reg_01"),
                str(url),
                "Alienware 18 Area-51",
                ProductConfiguration(extras={"Keyboard": keyboard}),
                Money("USD", 399999),
            )

    app = admin_client._transport.app
    app.state.check_service.pipeline = Pipeline()
    page = await admin_client.get("/products/new")
    options = await admin_client.post(
        "/products/options", data={"url": address, "csrf_token": csrf(page.text)}
    )
    assert 'name="option__Keyboard"' in options.text
    catalog_token = re.search(r'name="catalog_token" value="([^"]+)"', options.text).group(1)
    preview = await admin_client.post(
        "/products/preview",
        data={
            "url": address,
            "catalog_token": catalog_token,
            "option__Keyboard": keyboard,
            "csrf_token": csrf(options.text),
        },
    )
    assert "键盘" in preview.text
    assert keyboard in preview.text
    preview_token = re.search(r'name="preview_token" value="([^"]+)"', preview.text).group(1)
    await admin_client.post(
        "/products/confirm", data={"preview_token": preview_token, "csrf_token": csrf(preview.text)}
    )
    with app.state.session_factory() as session:
        card = session.scalar(select(Product).where(Product.source_site == "dell-us"))
        assert card.dell_selection == {"Keyboard": keyboard}
    dashboard = await admin_client.get("/")
    assert keyboard in dashboard.text


@pytest.mark.anyio
async def test_invalid_selected_configuration_cannot_create_card(admin_client, monkeypatch):
    app = admin_client._transport.app
    from pricewatch.fetching.safety import validate_public_url
    from pricewatch.web import routes_products

    monkeypatch.setattr(
        routes_products,
        "validate_public_url",
        lambda address: validate_public_url(address, lambda host: ["8.8.8.8"]),
    )
    address = "https://www.dell.com/en-us/shop/cty/spd/alienware18area51aa18250"
    app.state.option_catalogs = {
        "known": (
            1,
            address,
            {"Graphics Card": ["RTX 5070"]},
            datetime.now(UTC) + timedelta(minutes=15),
        )
    }
    page = await admin_client.get("/products/new")
    result = await admin_client.post(
        "/products/preview",
        data={
            "url": address,
            "catalog_token": "known",
            "option__Graphics Card": "RTX 5090",
            "csrf_token": csrf(page.text),
        },
    )
    assert result.status_code == 422
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count(Product.id))) == 0


@pytest.mark.anyio
async def test_theme_and_csv_export(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(
            source_site="dell-us", requested_url="https://www.dell.com/x", name="Alienware"
        )
        session.add(product)
        session.flush()
        product_id = product.id
        session.add(
            Observation(
                product_id=product_id,
                currency="USD",
                price_minor=299999,
                availability="in_stock",
                configuration_fingerprint="a",
            )
        )
    page = await admin_client.get("/")
    changed = await admin_client.post(
        "/theme", data={"theme": "dark", "csrf_token": csrf(page.text)}
    )
    assert changed.status_code == 303
    assert 'data-theme="dark"' in (await admin_client.get("/")).text
    exported = await admin_client.get(f"/products/{product_id}/history.csv")
    assert exported.status_code == 200
    assert "price_minor" in exported.text
    assert "299999" in exported.text


@pytest.mark.anyio
async def test_target_and_pause(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(product)
        session.flush()
        product_id = product.id
    page = await admin_client.get(f"/products/{product_id}")
    assert "未设置时间" in page.text
    assert 'name="check_interval_hours"' not in page.text
    updated = await admin_client.post(
        f"/products/{product_id}/settings",
        data={
            "target_price": "2800.00",
            "notify_mode": "target_or_change",
            "check_interval_hours": "1",
            "csrf_token": csrf(page.text),
        },
    )
    assert updated.status_code == 303
    paused = await admin_client.post(
        f"/products/{product_id}/pause", data={"csrf_token": csrf(page.text)}
    )
    assert paused.status_code == 303
    with app.state.session_factory() as session:
        current = session.get(Product, product_id)
        assert current.target_price_minor == 280000
        assert current.status == "paused"
        assert current.check_interval_hours == 24


@pytest.mark.anyio
async def test_each_card_time_and_auto_switch_control_monitoring_count(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        products = [
            Product(
                source_site="dell-us",
                requested_url=f"https://www.dell.com/{index}",
                status="paused",
            )
            for index in range(3)
        ]
        session.add_all(products)
        session.flush()
        ids = [product.id for product in products]

    async def count() -> int:
        page = BeautifulSoup((await admin_client.get("/")).text, "lxml")
        return int(page.select_one(".stats .stat strong").get_text(strip=True))

    assert await count() == 0
    page = await admin_client.get("/")
    assert "每天 10:00" not in page.text
    assert page.text.count('name="check_time"') == 3
    assert page.text.count('role="switch"') == 3
    for product_id, chosen in zip(ids, ("08:10", "10:20", "17:30"), strict=True):
        saved = await admin_client.post(
            f"/products/{product_id}/check-time",
            data={"check_time": chosen, "csrf_token": csrf((await admin_client.get("/")).text)},
        )
        assert saved.status_code == 303
        switched = await admin_client.post(
            f"/products/{product_id}/pause",
            data={"return_to": "dashboard", "csrf_token": csrf((await admin_client.get("/")).text)},
        )
        assert switched.status_code == 303
    assert await count() == 3
    switched_off = await admin_client.post(
        f"/products/{ids[1]}/pause",
        data={"return_to": "dashboard", "csrf_token": csrf((await admin_client.get("/")).text)},
    )
    assert switched_off.status_code == 303
    assert await count() == 2
    for product_id in (ids[0], ids[2]):
        await admin_client.post(
            f"/products/{product_id}/pause",
            data={"return_to": "dashboard", "csrf_token": csrf((await admin_client.get("/")).text)},
        )
    assert await count() == 0
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count(Product.id)).where(Product.status == "active")) == 0
        assert session.get(Setting, f"product_check_time:{ids[0]}").value_text == "08:10"


@pytest.mark.anyio
async def test_auto_switch_saves_selected_time_when_enabling(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/en-us/shop/example",
            status="paused",
        )
        session.add(product)
        session.flush()
        product_id = product.id

    response = await admin_client.get("/")
    card = BeautifulSoup(response.text, "lxml").select_one(".product-card")
    time_input = card.select_one('input[name="check_time"]')
    auto_switch = card.select_one('button[role="switch"]')
    assert time_input.find_parent("form") == auto_switch.find_parent("form")
    assert not auto_switch.has_attr("disabled")

    switched = await admin_client.post(
        f"/products/{product_id}/pause",
        data={
            "return_to": "dashboard",
            "check_time": "12:47",
            "csrf_token": csrf(response.text),
        },
    )
    assert switched.status_code == 303
    with app.state.session_factory() as session:
        assert session.get(Product, product_id).status == "active"
        assert session.get(Setting, f"product_check_time:{product_id}").value_text == "12:47"


@pytest.mark.anyio
async def test_paused_card_does_not_show_duplicate_auto_closed_badge(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        session.add(
            Product(
                source_site="dell-us",
                requested_url="https://www.dell.com/en-us/shop/example",
                status="paused",
            )
        )
    page = BeautifulSoup((await admin_client.get("/")).text, "lxml")
    card = page.select_one(".product-card")
    assert "自动已关闭" not in card.get_text(" ", strip=True)


@pytest.mark.anyio
async def test_auto_switch_requires_time_and_rejects_duplicate_active_time(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        products = [
            Product(
                source_site="dell-us",
                requested_url=f"https://www.dell.com/{index}",
                status="paused",
            )
            for index in range(2)
        ]
        session.add_all(products)
        session.flush()
        first, second = [product.id for product in products]
    page = await admin_client.get("/")
    missing = await admin_client.post(
        f"/products/{first}/pause",
        data={"return_to": "dashboard", "csrf_token": csrf(page.text)},
    )
    assert missing.status_code == 303
    assert missing.headers["location"].endswith("schedule_error=missing")
    for product_id in (first, second):
        result = await admin_client.post(
            f"/products/{product_id}/check-time",
            data={"check_time": "09:30", "csrf_token": csrf(page.text)},
        )
        assert result.status_code == 303
    await admin_client.post(
        f"/products/{first}/pause",
        data={"return_to": "dashboard", "csrf_token": csrf(page.text)},
    )
    conflict = await admin_client.post(
        f"/products/{second}/pause",
        data={"return_to": "dashboard", "csrf_token": csrf(page.text)},
    )
    assert conflict.status_code == 303
    assert conflict.headers["location"].endswith("schedule_error=conflict")
    with app.state.session_factory() as session:
        assert session.get(Product, first).status == "active"
        assert session.get(Product, second).status == "paused"


@pytest.mark.anyio
async def test_configuration_review_does_not_offer_auto_switch(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(
            source_site="dell-us", requested_url="https://www.dell.com/x", status="needs_attention"
        )
        session.add(product)
        session.flush()
        identifier = product.id
    detail = await admin_client.get(f"/products/{identifier}")
    assert detail.status_code == 200
    assert f'action="/products/{identifier}/pause"' not in detail.text


@pytest.mark.anyio
async def test_confirm_configuration_adopts_new_baseline(admin_client):
    from pricewatch.domain.products import (
        Money,
        ProductConfiguration,
        ProductIdentity,
        ProductSnapshot,
    )

    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(
            source_site="dell-us", requested_url="https://www.dell.com/x", sku="sku-1"
        )
        session.add(product)
        session.flush()
        product_id = product.id

    def observation(gpu, amount):
        return ProductSnapshot(
            ProductIdentity("dell-us", "sku-1"),
            "https://www.dell.com/x",
            "Alienware",
            ProductConfiguration(gpu=gpu),
            Money("USD", amount),
        )

    checker = app.state.check_service
    checker.accept_snapshot(product_id, observation("RTX 5090", 300000))
    checker.accept_snapshot(product_id, observation("RTX 5080", 280000))
    page = await admin_client.get(f"/products/{product_id}")
    response = await admin_client.post(
        f"/products/{product_id}/confirm-configuration", data={"csrf_token": csrf(page.text)}
    )
    assert response.status_code == 303
    with app.state.session_factory() as session:
        stored = session.get(Product, product_id)
        assert stored.configuration["gpu"] == "RTX 5080"
        assert stored.status == "paused"
        assert stored.next_check_at is None
    outcome = checker.accept_snapshot(product_id, observation("RTX 5080", 280000))
    assert outcome.product.status == "paused"
    assert outcome.events == []


@pytest.mark.anyio
async def test_dashboard_lowest_price_uses_entire_history(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(product)
        session.flush()
        for price in [100000, *([200000] * 35)]:
            session.add(
                Observation(
                    product_id=product.id,
                    currency="USD",
                    price_minor=price,
                    configuration_fingerprint="a",
                )
            )
    page = await admin_client.get("/")
    assert "最低 $1000.00" in page.text


@pytest.mark.anyio
async def test_non_usd_prices_are_not_displayed_as_dollars_or_mixed_in_overview(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(source_site="generic", requested_url="https://example.com/laptop")
        session.add(product)
        session.flush()
        product_id = product.id
        session.add(
            Observation(
                product_id=product_id,
                currency="EUR",
                price_minor=199900,
                configuration_fingerprint="a",
            )
        )
    dashboard = (await admin_client.get("/")).text
    detail = (await admin_client.get(f"/products/{product_id}")).text
    assert "EUR 1999.00" in dashboard
    assert "EUR 1999.00" in detail
    assert "$1999.00" not in dashboard + detail
    assert "所选商品价格" in dashboard
    assert "最低 $1999.00" not in dashboard


@pytest.mark.anyio
async def test_dashboard_cards_show_labeled_config_and_selectable_currency(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        for currency, price, gpu in [("USD", 399999, "RTX 5070"), ("CNY", 3741430, "RTX 5090")]:
            item = Product(
                source_site="dell-us",
                requested_url="https://www.dell.com/x",
                name="Alienware 18",
                configuration={"cpu": "Core Ultra 9", "gpu": gpu},
            )
            session.add(item)
            session.flush()
            session.add(
                Observation(
                    product_id=item.id,
                    currency=currency,
                    price_minor=price,
                    configuration_fingerprint="a",
                )
            )
    dashboard = (await admin_client.get("/")).text
    assert "显卡" in dashboard and "RTX 5090" in dashboard
    assert 'data-currency="CNY"' in dashboard
    assert 'data-price="CNY 37414.30"' in dashboard
    assert dashboard.count('class="card-select"') == 2


@pytest.mark.anyio
async def test_detail_price_chart_has_ordered_points(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        item = Product(source_site="dell-us", requested_url="https://www.dell.com/x")
        session.add(item)
        session.flush()
        product_id = item.id
        for day, amount in [(1, 399999), (2, 379999)]:
            session.add(
                Observation(
                    product_id=product_id,
                    currency="USD",
                    price_minor=amount,
                    configuration_fingerprint="a",
                    observed_at=datetime(2026, 9, day, tzinfo=UTC),
                )
            )
    detail = (await admin_client.get(f"/products/{product_id}")).text
    assert 'aria-label="价格走势图"' in detail
    chart = detail.split('aria-label="价格走势图"', 1)[1].split("</svg>", 1)[0]
    assert chart.count('class="chart-point"') == 2
    assert chart.index("2026-09-01") < chart.index("2026-09-02")


@pytest.mark.anyio
async def test_archive_restore_and_permanent_delete_are_explicit(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        item = Product(
            source_site="dell-us", requested_url="https://www.dell.com/x", name="Area-51"
        )
        session.add(item)
        session.flush()
        product_id = item.id
        session.add(Setting(key=f"product_check_time:{product_id}", value_text="08:10"))
        session.add(
            Observation(
                product_id=product_id,
                currency="USD",
                price_minor=399999,
                configuration_fingerprint="a",
            )
        )
    dashboard = await admin_client.get("/")
    assert "移除" in dashboard.text
    archived = await admin_client.post(
        f"/products/{product_id}/archive", data={"csrf_token": csrf(dashboard.text)}
    )
    assert archived.status_code == 303
    listing = await admin_client.get("/products/archived")
    assert "Area-51" in listing.text and "恢复" in listing.text
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count(Observation.id))) == 1
    restored = await admin_client.post(
        f"/products/{product_id}/restore", data={"csrf_token": csrf(listing.text)}
    )
    assert restored.status_code == 303
    with app.state.session_factory() as session:
        assert session.get(Product, product_id).status == "paused"
    detail = await admin_client.get(f"/products/{product_id}")
    await admin_client.post(
        f"/products/{product_id}/archive", data={"csrf_token": csrf(detail.text)}
    )
    deletion = await admin_client.get(f"/products/{product_id}/delete")
    wrong = await admin_client.post(
        f"/products/{product_id}/delete",
        data={
            "confirmation": "wrong",
            "csrf_token": csrf(deletion.text),
        },
    )
    assert wrong.status_code == 400
    deleted = await admin_client.post(
        f"/products/{product_id}/delete",
        data={
            "confirmation": "DELETE",
            "csrf_token": csrf(deletion.text),
        },
    )
    assert deleted.status_code == 303
    with app.state.session_factory() as session:
        assert session.get(Product, product_id) is None
        assert session.get(Setting, f"product_check_time:{product_id}") is None


@pytest.mark.anyio
async def test_all_entry_pages_link_the_browser_icon(admin_client):
    for url in ("/", "/products/new", "/login"):
        page = await admin_client.get(url)
        assert 'rel="icon"' in page.text
        assert "/static/mark.svg" in page.text


@pytest.mark.anyio
async def test_initialization_page_links_browser_icon(client):
    page = await client.get("/initialize")
    assert 'rel="icon"' in page.text
    assert "/static/mark.svg" in page.text


@pytest.mark.anyio
async def test_configuration_review_shows_difference_and_can_split(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(
            source_site="dell-us", requested_url="https://www.dell.com/x", sku="sku-1"
        )
        session.add(product)
        session.flush()
        product_id = product.id

    def observed(gpu):
        return ProductSnapshot(
            ProductIdentity("dell-us", "sku-1"),
            "https://www.dell.com/x",
            "Alienware",
            ProductConfiguration(gpu=gpu),
            Money("USD", 200000),
        )

    checker = app.state.check_service
    checker.accept_snapshot(product_id, observed("RTX 5090"))
    checker.accept_snapshot(product_id, observed("RTX 5080"))
    page = await admin_client.get(f"/products/{product_id}")
    assert "RTX 5090" in page.text
    assert "RTX 5080" in page.text
    assert "作为新商品" in page.text
    response = await admin_client.post(
        f"/products/{product_id}/confirm-configuration",
        data={"action": "separate", "csrf_token": csrf(page.text)},
    )
    assert response.status_code == 303
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count(Product.id))) == 2
        assert session.get(Product, product_id).status == "paused"
        fresh = session.scalar(select(Product).where(Product.id != product_id))
        assert fresh.configuration["gpu"] == "RTX 5080"
        assert fresh.status == "paused"
        assert fresh.next_check_at is None


@pytest.mark.anyio
async def test_selected_dell_card_cannot_split_without_a_new_recipe(admin_client):
    app = admin_client._transport.app
    with app.state.session_factory.begin() as session:
        product = Product(
            source_site="dell-us",
            requested_url="https://www.dell.com/x",
            sku="sku-1",
            dell_selection={"Graphics Card": "RTX 5090"},
        )
        session.add(product)
        session.flush()
        product_id = product.id

    def observed(gpu):
        return ProductSnapshot(
            ProductIdentity("dell-us", "sku-1"),
            "https://www.dell.com/x",
            "Alienware",
            ProductConfiguration(gpu=gpu),
            Money("USD", 200000),
        )

    checker = app.state.check_service
    checker.accept_snapshot(product_id, observed("RTX 5090"))
    checker.accept_snapshot(product_id, observed("RTX 5080"))
    page = await admin_client.get(f"/products/{product_id}")
    assert "作为新商品" not in page.text
    response = await admin_client.post(
        f"/products/{product_id}/confirm-configuration",
        data={"action": "separate", "csrf_token": csrf(page.text)},
    )
    assert response.status_code == 409
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count(Product.id))) == 1
        assert session.get(Product, product_id).dell_selection == {"Graphics Card": "RTX 5090"}
