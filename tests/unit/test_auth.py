import logging
import re

import pytest

from pricewatch.services.auth import hash_password, verify_password


def csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


async def create_admin(
    client,
    username: str = "owner",
    password: str = "a-strong-password",
    confirmation: str | None = None,
):
    page = await client.get("/initialize")
    return await client.post(
        "/initialize",
        data={
            "username": username,
            "password": password,
            "confirm_password": confirmation if confirmation is not None else password,
            "csrf_token": csrf_token(page.text),
        },
        follow_redirects=False,
    )


def test_password_hash_verifies_only_correct_password():
    encoded = hash_password("a-strong-password")

    assert verify_password("a-strong-password", encoded) is True
    assert verify_password("wrong-password", encoded) is False
    assert "a-strong-password" not in encoded


@pytest.mark.anyio
async def test_registration_closes_after_first_admin(client):
    response = await create_admin(client)

    assert response.status_code == 303
    assert (await client.get("/initialize")).status_code == 404


@pytest.mark.anyio
async def test_initialize_requires_matching_confirmation(client):
    response = await create_admin(client, confirmation="different-strong-password")

    assert response.status_code == 422
    assert "两次密码不一致" in response.text
    assert 'value="owner"' in response.text
    assert "a-strong-password" not in response.text
    assert "different-strong-password" not in response.text
    assert (await client.get("/initialize")).status_code == 200


@pytest.mark.anyio
async def test_initialize_requires_confirmation_field(client):
    page = await client.get("/initialize")
    response = await client.post(
        "/initialize",
        data={
            "username": "owner",
            "password": "a-strong-password",
            "csrf_token": csrf_token(page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert (await client.get("/initialize")).status_code == 200


@pytest.mark.anyio
async def test_initialize_form_shows_confirmation_input(client):
    response = await client.get("/initialize")

    assert response.status_code == 200
    assert "确认密码" in response.text
    assert 'name="confirm_password"' in response.text


@pytest.mark.anyio
async def test_post_without_csrf_is_rejected(client):
    await create_admin(client)

    assert (await client.post("/logout")).status_code == 403


@pytest.mark.anyio
async def test_login_is_rate_limited_after_five_failures(client):
    await create_admin(client)
    login_page = await client.get("/login")
    token = csrf_token(login_page.text)

    for _ in range(5):
        response = await client.post(
            "/login",
            data={"username": "owner", "password": "wrong", "csrf_token": token},
        )
        assert response.status_code == 401

    response = await client.post(
        "/login",
        data={"username": "owner", "password": "wrong", "csrf_token": token},
    )
    assert response.status_code == 429


@pytest.mark.anyio
async def test_logs_never_contain_credentials(caplog, client):
    secret = "feishu-secret-not-for-logs"
    caplog.set_level(logging.DEBUG)
    page = await client.get("/login")

    await client.post(
        "/login",
        data={"username": "owner", "password": secret, "csrf_token": csrf_token(page.text)},
    )

    assert secret not in caplog.text
