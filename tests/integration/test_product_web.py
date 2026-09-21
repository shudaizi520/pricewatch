import re

import pytest
from sqlalchemy import func, select

from pricewatch.db.models import Observation, Product
from pricewatch.domain.products import Money, ProductConfiguration, ProductIdentity, ProductSnapshot


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
    assert "美元最低价" in dashboard
    assert "最低 $1999.00" not in dashboard


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
