import pytest
from cryptography.fernet import InvalidToken

from pricewatch.services.crypto import SecretBox


def test_secret_box_round_trips_without_plaintext_leakage():
    secret_box = SecretBox("an-application-secret-with-at-least-32-characters")

    encrypted = secret_box.encrypt("https://open.feishu.cn/open-apis/bot/v2/hook/secret-token")

    assert "secret-token" not in encrypted
    assert secret_box.decrypt(encrypted).endswith("/secret-token")


def test_secret_box_rejects_tokens_from_another_key():
    first = SecretBox("first-application-secret-with-32-characters")
    second = SecretBox("second-application-secret-with-32-characters")

    with pytest.raises(InvalidToken):
        second.decrypt(first.encrypt("sensitive"))
