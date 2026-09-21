"""Read and apply public Dell option groups without guessing option deltas."""

import re

from bs4 import BeautifulSoup
from bs4.element import Tag
from playwright.async_api import Page

from pricewatch.fetching.types import ConfiguredOffer


def _groups(html: str) -> dict[str, Tag]:
    soup = BeautifulSoup(html, "lxml")
    groups: dict[str, Tag] = {}
    for group in soup.select('[role="group"][aria-label]'):
        if not isinstance(group, Tag):
            continue
        name = str(group.get("aria-label", "")).strip()
        if not name or not group.select(".option-grid-wrapper"):
            continue
        if name in groups:
            raise ValueError(f"重复的戴尔配置分组: {name}")
        groups[name] = group
    return groups


def _options(group: Tag) -> list[tuple[str, bool]]:
    options: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for wrapper in group.select(".option-grid-wrapper"):
        title = wrapper.select_one('[data-test-id="option-title"]')
        button = wrapper.select_one('[role="button"]')
        if not isinstance(title, Tag) or not isinstance(button, Tag):
            continue
        label = title.get_text(" ", strip=True)
        if not label or label in seen:
            raise ValueError(f"重复或空白的戴尔配置选项: {label}")
        seen.add(label)
        indicator = wrapper.select_one(".price.scoprice")
        selected = (
            isinstance(indicator, Tag)
            and indicator.get_text(" ", strip=True).casefold() == "selected"
        )
        options.append((label, selected))
    return options


def catalog_from_html(html: str) -> dict[str, list[str]]:
    return {name: [label for label, _ in _options(group)] for name, group in _groups(html).items()}


def selected_from_html(html: str) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name, group in _groups(html).items():
        choices = [label for label, chosen in _options(group) if chosen]
        if len(choices) > 1:
            raise ValueError(f"戴尔配置分组有多个选中项: {name}")
        if choices:
            selected[name] = choices[0]
    return selected


def configured_price_minor(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    body = soup.body
    if body is None:
        raise ValueError("戴尔页面没有可见价格")
    for hidden in body.select("script, style, template, [hidden]"):
        hidden.decompose()
    visible = body.get_text(" ", strip=True)
    values = re.findall(r"Dell Price\s*\$([\d,]+\.\d{2})(?!\d)", visible, re.I)
    if len(values) != 1:
        raise ValueError("无法唯一确认戴尔当前美元成交价")
    digits = values[0].replace(",", "")
    whole, cents = digits.split(".")
    return int(whole) * 100 + int(cents)


async def read_catalog(page: Page) -> dict[str, list[str]]:
    return catalog_from_html(await page.content())


async def apply_selection(page: Page, recipe: dict[str, str]) -> ConfiguredOffer:
    if not recipe:
        raise ValueError("请选择至少一个戴尔配置")
    for group, label in recipe.items():
        catalog = await read_catalog(page)
        if catalog.get(group, []).count(label) != 1:
            raise ValueError(f"戴尔配置已不存在或不唯一: {group} / {label}")
        selected = selected_from_html(await page.content())
        if selected.get(group) == label:
            continue
        option_group = page.get_by_role("group", name=group, exact=True)
        wrappers = option_group.locator(".option-grid-wrapper")
        matching = []
        for index in range(await wrappers.count()):
            wrapper = wrappers.nth(index)
            title = wrapper.locator('[data-test-id="option-title"]')
            if await title.count() == 1 and (await title.inner_text()).strip() == label:
                matching.append(wrapper)
        if len(matching) != 1:
            raise ValueError(f"戴尔配置定位失败: {group} / {label}")
        await matching[0].locator('[role="button"]').click()
        accept = page.get_by_role("button", name="Accept", exact=True)
        try:
            await accept.wait_for(state="visible", timeout=1500)
        except Exception:
            pass
        else:
            await accept.click()
        for _ in range(30):
            if selected_from_html(await page.content()).get(group) == label:
                break
            await page.wait_for_timeout(500)
        else:
            raise ValueError(f"戴尔没有完成配置切换: {group} / {label}")
    last_price: int | None = None
    for _ in range(30):
        html = await page.content()
        selected = selected_from_html(html)
        if any(selected.get(group) != label for group, label in recipe.items()):
            raise ValueError("戴尔最终选中配置与要求不符")
        try:
            price = configured_price_minor(html)
        except ValueError:
            price = None
        if price is not None and price == last_price:
            return ConfiguredOffer(selected, price)
        last_price = price
        await page.wait_for_timeout(500)
    raise ValueError("戴尔配置价格未稳定显示")
