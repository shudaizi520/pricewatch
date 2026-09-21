import pytest

from pricewatch.fetching.safety import UnsafeUrlError, validate_public_url


def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/x",
        "http://user:pass@example.com/x",
        "file:///etc/passwd",
    ],
)
def test_rejects_unsafe_targets(url: str):
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url, public_resolver)


def test_rejects_dns_with_mixed_private_and_public_answers():
    with pytest.raises(UnsafeUrlError):
        validate_public_url("https://shop.example/item", lambda _: ["93.184.216.34", "10.0.0.1"])


def test_accepts_public_https_target():
    assert (
        str(validate_public_url("https://shop.example/item", public_resolver))
        == "https://shop.example/item"
    )


def test_private_dns_failure_remains_distinguishable_from_malformed_address():
    with pytest.raises(UnsafeUrlError, match="non-public address"):
        validate_public_url("https://shop.example/item", lambda _: ["10.0.0.1"])
