import re

import pytest
from sqlalchemy import select

from pricewatch.db.models import Setting


def token(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.mark.anyio
async def test_save_feishu_encrypted_and_status_redacts_secret(client):
    page = await client.get("/initialize")
    await client.post(
        "/initialize",
        data={
            "username": "owner",
            "password": "VeryStrongSecret123!",
            "csrf_token": token(page.text),
        },
    )
    app = client._transport.app
    page = await client.get("/settings")
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/0123456789abcdef0123456789abcdef"
    result = await client.post(
        "/settings", data={"feishu_webhook": webhook, "csrf_token": token(page.text)}
    )
    assert result.status_code == 303
    with app.state.session_factory() as session:
        stored = session.scalar(select(Setting).where(Setting.key == "feishu_webhook"))
        assert webhook not in stored.value_text
    created = await client.post(
        "/backups/create", data={"csrf_token": token(page.text)}
    )
    assert created.status_code == 200
    downloaded = await client.get(created.json()["download_url"])
    assert downloaded.status_code == 200
    assert webhook.encode() not in downloaded.content
    assert webhook not in (await client.get("/settings")).text
    assert webhook not in (await client.get("/status")).text
