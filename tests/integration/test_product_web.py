import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from pricewatch.db.models import Observation, Product
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
    updated = await admin_client.post(
        f"/products/{product_id}/settings",
        data={
            "target_price": "2800.00",
            "notify_mode": "target_or_change",
            "check_interval_hours": "6",
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
    outcome = checker.accept_snapshot(product_id, observation("RTX 5080", 280000))
    assert outcome.product.status == "active"
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
