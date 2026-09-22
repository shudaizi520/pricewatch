"""Read and apply public Dell option groups without guessing option deltas."""

import re

from bs4 import BeautifulSoup
from bs4.element import Tag
from playwright.async_api import Locator, Page

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
    for wrapper in group.select(".option-grid-wrapper"):
        title = wrapper.select_one('[data-test-id="option-title"]')
        button = wrapper.select_one('[role="button"]')
        if not isinstance(title, Tag) or not isinstance(button, Tag):
            continue
        label = title.get_text(" ", strip=True)
        if not label:
            raise ValueError("戴尔配置选项名称为空")
        indicator = wrapper.select_one(".price.scoprice")
        selected = (
            isinstance(indicator, Tag)
            and indicator.get_text(" ", strip=True).casefold() == "selected"
        )
        options.append((label, selected))
    return options


def catalog_from_html(html: str) -> dict[str, list[str]]:
    return {
        name: list(dict.fromkeys(label for label, _ in _options(group)))
        for name, group in _groups(html).items()
    }


def selected_from_html(html: str) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name, group in _groups(html).items():
        choices = [label for label, chosen in _options(group) if chosen]
        if len(choices) > 1:
            raise ValueError(f"戴尔配置分组有多个选中项: {name}")
        if choices:
            selected[name] = choices[0]
    return selected


def active_price_minor(visible_text: str) -> int:
    values = re.findall(r"Dell Price\s*\$\s*([\d,]+\.\d{2})(?!\d)", visible_text, re.I)
    if len(set(values)) != 1:
        raise ValueError("无法唯一确认戴尔当前美元成交价")
    digits = values[0].replace(",", "")
    whole, cents = digits.split(".")
    return int(whole) * 100 + int(cents)


def configured_price_minor(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    body = soup.body
    if body is None:
        raise ValueError("戴尔页面没有可见价格")
    for hidden in body.select("script, style, template, [hidden]"):
        hidden.decompose()
    return active_price_minor(body.get_text(" ", strip=True))


async def read_catalog(page: Page) -> dict[str, list[str]]:
    return catalog_from_html(await page.content())


async def settled_offer(
    page: Page,
    recipe: dict[str, str],
    baseline_price: int | None,
    changed: bool,
    quote_confirmed: bool = False,
) -> ConfiguredOffer:
    last_price: int | None = None
    stable_reads = 0
    for _ in range(20):
        selected = selected_from_html(await page.content())
        if any(selected.get(group) != label for group, label in recipe.items()):
            raise ValueError("戴尔最终选中配置与要求不符")
        try:
            price = active_price_minor(await page.evaluate("document.body.innerText"))
        except ValueError:
            price = None
        # Dell can mark the new option selected before its asynchronous quote arrives.
        if changed and not quote_confirmed and price == baseline_price:
            price = None
        stable_reads = stable_reads + 1 if price is not None and price == last_price else 1
        if price is not None and stable_reads >= 3:
            return ConfiguredOffer(selected, price)
        last_price = price
        await page.wait_for_timeout(1000)
    if changed:
        raise ValueError("戴尔切换配置后价格未更新; 未保存旧报价")
    raise ValueError("戴尔配置价格未稳定显示")


async def apply_selection(page: Page, recipe: dict[str, str]) -> ConfiguredOffer:
    if not recipe:
        raise ValueError("请选择至少一个戴尔配置")
    last_offer: ConfiguredOffer | None = None
    interaction_waited = False
    for group, label in recipe.items():
        catalog = await read_catalog(page)
        if label not in catalog.get(group, []):
            raise ValueError(f"戴尔配置已不存在或不唯一: {group} / {label}")
        selected = selected_from_html(await page.content())
        if selected.get(group) == label:
            continue
        baseline_price = active_price_minor(await page.evaluate("document.body.innerText"))
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
        if not interaction_waited:
            # Dell can render the default options before its click handlers are ready.
            await page.wait_for_timeout(15000)
            interaction_waited = True
        accept = (
            page.get_by_role("dialog")
            .filter(has_text="Spec changes required")
            .get_by_role("button", name="Accept", exact=True)
        )

        async def choose_option(
            matched: Locator = matching[0],
            dialog: Locator = accept,
            option_group: str = group,
            option_label: str = label,
        ) -> None:
            await matched.locator('[role="button"]').click()
            for _ in range(40):
                if await dialog.is_visible():
                    await dialog.click()
                if selected_from_html(await page.content()).get(option_group) == option_label:
                    return
                await page.wait_for_timeout(500)
            raise ValueError(f"戴尔没有完成配置切换: {option_group} / {option_label}")

        quote_confirmed = False
        if group == "Keyboard":
            async with page.expect_response(
                lambda response: (
                    "/shopapi/unifiedpd/configure/" in response.url and response.status == 200
                ),
                timeout=20000,
            ) as response_info:
                await choose_option()
            quote_response = await response_info.value
            await quote_response.finished()
            quote_confirmed = True
        else:
            await choose_option()
        if quote_confirmed:
            last_offer = await settled_offer(
                page,
                {group: label},
                baseline_price,
                changed=True,
                quote_confirmed=True,
            )
        else:
            last_offer = await settled_offer(page, {group: label}, baseline_price, changed=True)
    if last_offer is None:
        return await settled_offer(page, recipe, baseline_price=None, changed=False)
    if any(last_offer.selected.get(group) != label for group, label in recipe.items()):
        raise ValueError("戴尔最终选中配置与要求不符")
    return last_offer
