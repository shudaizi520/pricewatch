import json
from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from pricewatch.fetching.dell_options import (
    active_price_minor,
    apply_selection,
    catalog_from_html,
    configured_price_minor,
    selected_from_html,
    settled_offer,
)
from pricewatch.fetching.types import ConfiguredOffer

FIXTURE = Path(__file__).parents[1] / "fixtures" / "dell" / "options.html"


def test_catalog_uses_dell_group_and_option_titles_not_price_deltas():
    catalog = catalog_from_html(FIXTURE.read_text())
    assert catalog["Graphics Card"] == [
        "NVIDIA® GeForce RTX™ 5070 8 GB GDDR7",
        "NVIDIA® GeForce RTX™ 5090 24 GB GDDR7",
    ]
    assert catalog["Power Supply"] == [
        "280W 7.4mm AC Adapter",
        "360W Small Form Factor Adapter",
    ]


def test_selected_options_are_bound_to_groups():
    selected = selected_from_html(FIXTURE.read_text())
    assert selected == {
        "Graphics Card": "NVIDIA® GeForce RTX™ 5070 8 GB GDDR7",
        "Power Supply": "280W 7.4mm AC Adapter",
    }


def test_configured_price_uses_visible_purchase_block_not_static_jsonld():
    html = FIXTURE.read_text().replace("<span>$3,999.99</span>", "<span>$5,099.99</span>")
    assert configured_price_minor(html) == 509999


def test_active_browser_text_accepts_dell_whitespace_and_same_price_repeated():
    assert active_price_minor("Dell Price\n$ 5,099.99\nDell Price $5,099.99") == 509999


@pytest.mark.parametrize("price", ["$4,000", "EUR 4,000.00", "-$1.00"])
def test_configured_price_rejects_missing_or_non_usd_offer(price):
    html = FIXTURE.read_text().replace("<span>$3,999.99</span>", f"<span>{price}</span>")
    with pytest.raises(ValueError):
        configured_price_minor(html)


def test_duplicate_group_is_rejected():
    html = FIXTURE.read_text()
    first_group = html.split('<div class="accordion-box">', 1)[1].split(
        '<div class="accordion-box">', 1
    )[0]
    duplicated = html.replace(
        "</section>", '<div class="accordion-box">' + first_group + "</section>"
    )
    with pytest.raises(ValueError):
        catalog_from_html(duplicated)


def test_duplicate_unselected_option_label_is_deduplicated_in_catalog():
    soup = BeautifulSoup(FIXTURE.read_text(), "lxml")
    group = soup.select_one('[aria-label="Graphics Card"]')
    group.append(deepcopy(group.select(".option-grid-wrapper")[1]))
    assert len(catalog_from_html(str(soup))["Graphics Card"]) == 2


@pytest.mark.anyio
async def test_changed_option_waits_past_stale_default_price():
    soup = BeautifulSoup(FIXTURE.read_text(), "lxml")
    gpu = soup.select_one('[aria-label="Graphics Card"]')
    buttons = gpu.select(".option-grid-wrapper")
    buttons[0].select_one(".price.scoprice").string = ""
    buttons[1].select_one(".price.scoprice").string = "Selected"
    selected_html = str(soup)

    class SlowQuotePage:
        def __init__(self):
            self.prices = [399999, 399999, 399999, 509999, 509999, 509999]
            self.reads = 0

        async def content(self):
            return selected_html

        async def evaluate(self, _script):
            price = self.prices[min(self.reads, len(self.prices) - 1)]
            self.reads += 1
            return f"Dell Price ${price / 100:,.2f}"

        async def wait_for_timeout(self, _milliseconds):
            return None

    page = SlowQuotePage()
    quote = await settled_offer(
        page,
        {"Graphics Card": "NVIDIA® GeForce RTX™ 5090 24 GB GDDR7"},
        baseline_price=399999,
        changed=True,
    )
    assert quote.price_minor == 509999
    assert page.reads >= 6


@pytest.mark.anyio
async def test_changed_option_rejects_price_that_never_moves():
    class StaleQuotePage:
        async def content(self):
            return FIXTURE.read_text()

        async def evaluate(self, _script):
            return "Dell Price $3,999.99"

        async def wait_for_timeout(self, _milliseconds):
            return None

    with pytest.raises(ValueError, match="价格未更新"):
        await settled_offer(
            StaleQuotePage(),
            {"Graphics Card": "NVIDIA® GeForce RTX™ 5070 8 GB GDDR7"},
            baseline_price=399999,
            changed=True,
        )


@pytest.mark.anyio
async def test_confirmed_same_price_keyboard_is_valid():
    soup = BeautifulSoup(FIXTURE.read_text(), "lxml")
    group = soup.select_one('[aria-label="Graphics Card"]')
    group["aria-label"] = "Keyboard"
    wrappers = group.select(".option-grid-wrapper")
    wrappers[0].select_one('[data-test-id="option-title"]').string = "Standard keyboard"
    wrappers[1].select_one('[data-test-id="option-title"]').string = "CherryMX keyboard"
    wrappers[0].select_one(".price.scoprice").string = ""
    wrappers[1].select_one(".price.scoprice").string = "Selected"

    class Page:
        async def content(self):
            return str(soup)

        async def evaluate(self, _script):
            return "Dell Price $3,999.99"

        async def wait_for_timeout(self, _milliseconds):
            return None

    offer = await settled_offer(
        Page(),
        {"Keyboard": "CherryMX keyboard"},
        399999,
        changed=True,
        quote_confirmed=True,
    )
    assert offer.price_minor == 399999


@pytest.mark.anyio
@pytest.mark.parametrize("group", ["Keyboard", "Graphics Card"])
@pytest.mark.parametrize("request_belongs_to_click", [True, False])
@pytest.mark.parametrize("option_matches", [True, False])
@pytest.mark.parametrize("option_id_present", [True, False])
async def test_same_price_switch_requires_its_own_dell_quote(
    monkeypatch, group, request_belongs_to_click, option_matches, option_id_present
):
    from pricewatch.fetching import dell_options

    class Request:
        url = "https://www.dell.com/shopapi/unifiedpd/configure/en-us/sku"

        option_value = (
            "OPTION-NEW" if option_matches else "OPTION-OLD"
        ) if option_id_present else ("CherryMX" if option_matches else "Standard")
        post_data = json.dumps({"option": option_value})

    class Response:
        url = "https://www.dell.com/shopapi/unifiedpd/configure/en-us/sku"
        status = 200

        def __init__(self, request):
            self.request = request

        async def finished(self):
            return None

    class Locator:
        def __init__(self, page):
            self.page = page

        def locator(self, _selector):
            return self

        def nth(self, _index):
            return self

        async def count(self):
            return 1

        async def inner_text(self):
            return "CherryMX"

        async def get_attribute(self, name):
            assert name == "data-option-id"
            return "OPTION-NEW" if option_id_present else None

        async def click(self):
            self.page.selected = "CherryMX"
            request = Request()
            if request_belongs_to_click and (callback := self.page.listeners.get("request")):
                callback(request)
            if callback := self.page.listeners.get("response"):
                callback(Response(request))

        async def is_visible(self):
            return False

        def filter(self, **_kwargs):
            return self

        def get_by_role(self, *_args, **_kwargs):
            return self

    class Page:
        selected = "Standard"

        def __init__(self):
            self.listeners = {}

        async def content(self):
            return json.dumps({group: self.selected})

        async def evaluate(self, _script):
            return "Dell Price $3,999.99"

        async def wait_for_timeout(self, _milliseconds):
            return None

        def get_by_role(self, *_args, **_kwargs):
            return Locator(self)

        def on(self, name, callback):
            self.listeners[name] = callback

        def remove_listener(self, name, _callback):
            self.listeners.pop(name, None)

    monkeypatch.setattr(
        dell_options, "read_catalog", lambda _page: _async_value({group: ["CherryMX"]})
    )
    monkeypatch.setattr(dell_options, "selected_from_html", json.loads)
    if request_belongs_to_click and option_matches:
        result = await apply_selection(Page(), {group: "CherryMX"})
        assert result.price_minor == 399999
    else:
        with pytest.raises(ValueError, match="价格未更新"):
            await apply_selection(Page(), {group: "CherryMX"})


async def _async_value(value):
    return value


@pytest.mark.anyio
async def test_each_changed_group_must_settle_before_next_click(monkeypatch):
    from pricewatch.fetching import dell_options

    labels = {"Graphics Card": "RTX 5090", "Power Supply": "360W"}
    settled = []

    class Locator:
        def __init__(self, page, group, label):
            self.page, self.group, self.label = page, group, label

        def locator(self, _selector):
            return self

        async def count(self):
            return 1

        async def inner_text(self):
            return self.label

        async def click(self):
            self.page.selected[self.group] = self.label

    class Wrappers:
        def __init__(self, page, group):
            self.page, self.group = page, group

        def locator(self, _selector):
            return self

        async def count(self):
            return 1

        def nth(self, _index):
            return Locator(self.page, self.group, labels[self.group])

    class Accept:
        def filter(self, **_kwargs):
            return self

        def get_by_role(self, *_args, **_kwargs):
            return self

        async def is_visible(self):
            return False

    class Page:
        def __init__(self):
            self.selected = {"Graphics Card": "RTX 5070", "Power Supply": "280W"}
            self.price = 399999

        async def content(self):
            return json.dumps(self.selected)

        async def evaluate(self, _script):
            return f"Dell Price ${self.price / 100:,.2f}"

        async def wait_for_timeout(self, _milliseconds):
            return None

        def get_by_role(self, role, name=None, exact=True):
            return Wrappers(self, name) if role == "group" else Accept()

    async def catalog(_page):
        return {group: [label] for group, label in labels.items()}

    async def quote(page, recipe, baseline_price, changed, quote_responses):
        assert quote_responses == []
        settled.append((dict(recipe), baseline_price, changed))
        page.price = 509999 if len(settled) == 1 else 519999
        return ConfiguredOffer(dict(page.selected), page.price)

    monkeypatch.setattr(dell_options, "read_catalog", catalog)
    monkeypatch.setattr(dell_options, "selected_from_html", json.loads)
    monkeypatch.setattr(dell_options, "settled_offer", quote)
    result = await apply_selection(Page(), labels)
    assert result.price_minor == 519999
    assert settled == [
        ({"Graphics Card": "RTX 5090"}, 399999, True),
        ({"Power Supply": "360W"}, 509999, True),
    ]


@pytest.mark.anyio
async def test_changed_option_clicks_without_a_fixed_fifteen_second_delay(monkeypatch):
    from pricewatch.fetching import dell_options

    class Locator:
        def __init__(self, page):
            self.page = page

        def locator(self, _selector):
            return self

        def nth(self, _index):
            return self

        def filter(self, **_kwargs):
            return self

        def get_by_role(self, *_args, **_kwargs):
            return self

        async def count(self):
            return 1

        async def inner_text(self):
            return "RTX 5090"

        async def is_visible(self):
            return False

        async def click(self):
            assert self.page.elapsed_ms == 0, "actionable option must not wait a fixed 15 seconds"
            self.page.selected = "RTX 5090"

    class Page:
        selected = "RTX 5070"
        elapsed_ms = 0

        async def content(self):
            return json.dumps({"Graphics Card": self.selected})

        async def evaluate(self, _script):
            return "Dell Price $3,999.99"

        async def wait_for_timeout(self, milliseconds):
            self.elapsed_ms += milliseconds

        def get_by_role(self, *_args, **_kwargs):
            return Locator(self)

    async def catalog(_page):
        return {"Graphics Card": ["RTX 5070", "RTX 5090"]}

    async def settled(page, _recipe, _baseline_price, changed, quote_responses):
        assert quote_responses == []
        assert changed
        return ConfiguredOffer({"Graphics Card": page.selected}, 509999)

    monkeypatch.setattr(dell_options, "read_catalog", catalog)
    monkeypatch.setattr(dell_options, "selected_from_html", json.loads)
    monkeypatch.setattr(dell_options, "settled_offer", settled)

    result = await apply_selection(Page(), {"Graphics Card": "RTX 5090"})
    assert result.selected["Graphics Card"] == "RTX 5090"


@pytest.mark.anyio
@pytest.mark.parametrize("request_delay_polls", [0, 11])
async def test_inflight_dell_quote_is_not_triggered_twice(monkeypatch, request_delay_polls):
    from pricewatch.fetching import dell_options

    class Locator:
        def __init__(self, page):
            self.page = page

        def locator(self, _selector):
            return self

        def nth(self, _index):
            return self

        def filter(self, **_kwargs):
            return self

        def get_by_role(self, *_args, **_kwargs):
            return self

        async def count(self):
            return 1

        async def inner_text(self):
            return "RTX 5090"

        async def is_visible(self):
            return False

        async def click(self):
            self.page.clicks += 1
            if request_delay_polls == 0 and (callback := self.page.listeners.get("request")):
                callback(
                    type("Request", (), {"url": "https://www.dell.com/shopapi/unifiedpd/configure/en-us/sku"})()
                )

    class Page:
        def __init__(self):
            self.clicks = 0
            self.listeners = {}
            self.waits = 0

        async def content(self):
            return json.dumps({"Graphics Card": "RTX 5070"})

        async def evaluate(self, _script):
            return "Dell Price $3,999.99"

        async def wait_for_timeout(self, _milliseconds):
            self.waits += 1
            if (
                self.waits == request_delay_polls
                and request_delay_polls
                and (callback := self.listeners.get("request"))
            ):
                callback(
                    type("Request", (), {"url": "https://www.dell.com/shopapi/unifiedpd/configure/en-us/sku"})()
                )

        def get_by_role(self, *_args, **_kwargs):
            return Locator(self)

        def on(self, name, callback):
            self.listeners[name] = callback

    async def catalog(_page):
        return {"Graphics Card": ["RTX 5070", "RTX 5090"]}

    monkeypatch.setattr(dell_options, "read_catalog", catalog)
    monkeypatch.setattr(dell_options, "selected_from_html", json.loads)
    page = Page()
    with pytest.raises(ValueError, match="没有完成配置切换"):
        await apply_selection(page, {"Graphics Card": "RTX 5090"})
    assert page.clicks == 1


@pytest.mark.anyio
async def test_late_spec_dependency_dialog_is_accepted(monkeypatch):
    from pricewatch.fetching import dell_options

    class Accept:
        def __init__(self, page):
            self.page = page

        async def wait_for(self, **_kwargs):
            raise TimeoutError()

        async def is_visible(self):
            self.page.polls += 1
            return self.page.clicked and self.page.polls >= 4

        async def click(self):
            self.page.selected = "RTX 5090"

    class Locator:
        def __init__(self, page):
            self.page = page

        def locator(self, _selector):
            return self

        async def count(self):
            return 1

        def nth(self, _index):
            return self

        async def inner_text(self):
            return "RTX 5090"

        async def click(self):
            self.page.clicked = True

    class Dialog:
        def __init__(self, page):
            self.page = page

        def filter(self, **_kwargs):
            return self

        def get_by_role(self, *_args, **_kwargs):
            return Accept(self.page)

    class Page:
        def __init__(self):
            self.selected = "RTX 5070"
            self.clicked = False
            self.polls = 0

        async def content(self):
            return json.dumps({"Graphics Card": self.selected})

        async def evaluate(self, _script):
            return "Dell Price $3,999.99"

        async def wait_for_timeout(self, _milliseconds):
            return None

        def get_by_role(self, role, **_kwargs):
            return Locator(self) if role == "group" else Dialog(self)

    async def catalog(_page):
        return {"Graphics Card": ["RTX 5090"]}

    async def quote(page, _recipe, _baseline, changed, quote_responses):
        assert quote_responses == []
        assert changed
        return ConfiguredOffer({"Graphics Card": page.selected}, 509999)

    monkeypatch.setattr(dell_options, "read_catalog", catalog)
    monkeypatch.setattr(dell_options, "selected_from_html", json.loads)
    monkeypatch.setattr(dell_options, "settled_offer", quote)
    page = Page()
    result = await apply_selection(page, {"Graphics Card": "RTX 5090"})
    assert result.price_minor == 509999
    assert page.polls >= 4


@pytest.mark.anyio
async def test_current_dell_selection_modal_button_is_accepted(monkeypatch):
    from pricewatch.fetching import dell_options

    class ModernAccept:
        def __init__(self, page):
            self.page = page

        async def is_visible(self):
            return self.page.clicked and not self.page.accepted

        async def click(self):
            self.page.accepted = True
            self.page.selected = "RTX 5090"

    class NeverVisible:
        def filter(self, **_kwargs):
            return self

        def get_by_role(self, *_args, **_kwargs):
            return self

        async def is_visible(self):
            return False

    class Option:
        def __init__(self, page):
            self.page = page

        def locator(self, _selector):
            return self

        def nth(self, _index):
            return self

        async def count(self):
            return 1

        async def inner_text(self):
            return "RTX 5090"

        async def get_attribute(self, _name):
            return "GPU-5090"

        async def click(self):
            self.page.clicked = True

    class Page:
        def __init__(self):
            self.selected = "RTX 5080"
            self.clicked = False
            self.accepted = False

        async def content(self):
            return json.dumps({"Graphics Card": self.selected})

        async def evaluate(self, _script):
            return "Dell Price $5,699.99"

        async def wait_for_timeout(self, _milliseconds):
            return None

        def locator(self, selector):
            assert selector == "#selection-modal-change-btn"
            return ModernAccept(self)

        def get_by_role(self, role, **_kwargs):
            return Option(self) if role == "group" else NeverVisible()

    async def catalog(_page):
        return {"Graphics Card": ["RTX 5080", "RTX 5090"]}

    async def quote(page, _recipe, _baseline, changed, quote_responses):
        assert changed
        return ConfiguredOffer({"Graphics Card": page.selected}, 719999)

    monkeypatch.setattr(dell_options, "read_catalog", catalog)
    monkeypatch.setattr(dell_options, "selected_from_html", json.loads)
    monkeypatch.setattr(dell_options, "settled_offer", quote)

    page = Page()
    result = await apply_selection(page, {"Graphics Card": "RTX 5090"})

    assert page.accepted is True
    assert result.selected == {"Graphics Card": "RTX 5090"}


@pytest.mark.anyio
async def test_option_click_retries_once_when_first_click_emits_no_request(monkeypatch):
    from pricewatch.fetching import dell_options

    class Request:
        url = "https://www.dell.com/shopapi/unifiedpd/configure/en-us/sku"
        post_data = json.dumps({"optionId": "GPU-5090"})

    class Response:
        url = Request.url
        status = 200

        def __init__(self, request):
            self.request = request

        async def finished(self):
            return None

    class HiddenModal:
        def filter(self, **_kwargs):
            return self

        def get_by_role(self, *_args, **_kwargs):
            return self

        async def is_visible(self):
            return False

    class Option:
        def __init__(self, page):
            self.page = page

        def locator(self, _selector):
            return self

        def nth(self, _index):
            return self

        async def count(self):
            return 1

        async def inner_text(self):
            return "RTX 5090"

        async def get_attribute(self, _name):
            return "GPU-5090"

        async def click(self):
            self.page.clicks += 1
            if self.page.clicks == 2:
                request = Request()
                self.page.listeners["request"](request)
                self.page.selected = "RTX 5090"
                self.page.listeners["response"](Response(request))

    class Page:
        def __init__(self):
            self.selected = "RTX 5080"
            self.clicks = 0
            self.listeners = {}

        async def content(self):
            return json.dumps({"Graphics Card": self.selected})

        async def evaluate(self, _script):
            return "Dell Price $5,699.99"

        async def wait_for_timeout(self, _milliseconds):
            return None

        def locator(self, selector):
            assert selector == "#selection-modal-change-btn"
            return HiddenModal()

        def get_by_role(self, role, **_kwargs):
            return Option(self) if role == "group" else HiddenModal()

        def on(self, name, callback):
            self.listeners[name] = callback

        def remove_listener(self, name, _callback):
            self.listeners.pop(name, None)

    async def catalog(_page):
        return {"Graphics Card": ["RTX 5080", "RTX 5090"]}

    async def quote(page, _recipe, _baseline, changed, quote_responses):
        assert changed
        assert len(quote_responses) == 1
        return ConfiguredOffer({"Graphics Card": page.selected}, 719999)

    monkeypatch.setattr(dell_options, "read_catalog", catalog)
    monkeypatch.setattr(dell_options, "selected_from_html", json.loads)
    monkeypatch.setattr(dell_options, "settled_offer", quote)

    page = Page()
    result = await apply_selection(page, {"Graphics Card": "RTX 5090"})

    assert page.clicks == 2
    assert result.selected == {"Graphics Card": "RTX 5090"}
