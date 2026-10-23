"""Bounded entry recovery for explicitly known US Dell category migrations."""

import re

from httpx import URL

Entry = tuple[str, URL]
_LAPTOP_PATH = re.compile(
    r"/en-us/shop/(laptop-computers|dell-laptops)/spd/([a-z0-9-]+)(?:/([A-Za-z0-9_-]+))?"
)


def dell_check_entries(
    original: URL, canonical: URL, sku: str | None, preferred: str | None = None,
) -> list[Entry]:
    entries = _check_entries(original, canonical, sku)
    # A hint may only reorder the current bounded, revalidated candidate list.
    # A stale, corrupted or off-site setting must never introduce a request.
    return sorted(entries, key=lambda item: str(item[1]) != preferred)


def _check_entries(original: URL, canonical: URL, sku: str | None) -> list[Entry]:
    same_store = (
        original.scheme == canonical.scheme == "https"
        and original.host == canonical.host
        and canonical.host in {"www.dell.com", "dell.com"}
        and original.port == canonical.port
        and not original.userinfo and not canonical.userinfo
        and original.path.startswith("/en-us/shop/")
        and canonical.path.startswith("/en-us/shop/")
    )
    entries: list[Entry] = [("直达入口", canonical)]
    if same_store and sku and original != canonical:
        entries.insert(0, ("原始入口", original))

    first = _LAPTOP_PATH.fullmatch(original.path)
    saved = _LAPTOP_PATH.fullmatch(canonical.path)
    if not (
        same_store and canonical.port in {None, 443}
        and sku and re.fullmatch(r"[A-Za-z0-9_-]+", sku)
        and not original.query and not canonical.query
        and not original.fragment and not canonical.fragment
        and first and saved and first[2] == saved[2]
        and first[3] in {None, sku} and saved[3] in {None, sku}
    ):
        return entries

    # A canonical URL is saved only after complete quote/configuration validation.
    # Reuse its working category without overwriting the user's original URL.
    if saved[1] == "dell-laptops" and first[1] == "laptop-computers":
        family = canonical.copy_with(path=f"/en-us/shop/dell-laptops/spd/{saved[2]}")
        entries = [("已验证分类入口", family), ("直达入口", canonical),
                   ("原始入口", original)]
        entries.append(("原分类直达入口", original.copy_with(
            path=f"/en-us/shop/laptop-computers/spd/{saved[2]}/{sku}",
        )))
    elif first[1] == saved[1] == "laptop-computers":
        family = canonical.copy_with(path=f"/en-us/shop/dell-laptops/spd/{saved[2]}")
        entries += [("官方分类入口", family),
                    ("官方分类直达入口", family.copy_with(path=f"{family.path}/{sku}"))]

    # Never scan arbitrary paths, generate new SKUs, or retry a duplicate entry.
    unique: list[Entry] = []
    for entry in entries:
        if all(entry[1] != prior[1] for prior in unique):
            unique.append(entry)
    return unique[:4]
