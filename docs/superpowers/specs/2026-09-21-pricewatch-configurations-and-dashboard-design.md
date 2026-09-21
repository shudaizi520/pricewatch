# PriceWatch configurations and dashboard completion

**Status:** Proposed for review  
**Date:** 2026-09-21  
**Scope:** Existing single-admin PriceWatch on TrueNAS; at most ten monitored cards

## Intent and success

The owner wants one card per exact Dell US configuration without needing to find a distinct URL. A card must show its own price, readable hardware specification, and price history. The dashboard must offer an obvious way to remove an unwanted card, a recognizable browser icon, and a summary price that follows the selected card. The app must never show a default configuration's price as another configuration's price. The current China Dell card and existing data remain intact.

Success means the owner can paste the Alienware 18 Area-51 model URL once, select two distinct configurations in PriceWatch, preview two actual Dell USD purchase prices, save two cards, and later check each independently. If a selection or final price cannot be verified, the app reports a failed/attention-needed check and preserves the last trusted price.

## Chosen approach

The model URL identifies a Dell product family; a saved card identifies an exact set of selected Dell options. The add flow reads selectable options from the live Dell page, lets the owner choose a configuration, then uses the existing guarded Chromium acquisition path to apply those choices and read Dell's final visible purchase price. The same choice set is reapplied on scheduled checks. No price is calculated by adding displayed option deltas.

Alternatives considered:

- Finding Dell's prebuilt SKU links automatically is smaller but does not cover arbitrary combinations selected on one model page.
- Taking the base price plus option deltas is unsafe because dependent choices, discounts, and delivery offers can change the final amount.

The implementation remains Dell-US-specific. Generic retailers retain the existing URL-based behavior. No Dell account, cookies supplied by the owner, checkout, or cart automation is needed.

## Add and check flow

1. After a Dell model URL is entered, PriceWatch fetches the page and shows available choices for processor, graphics, memory, storage, display, and operating system when the page exposes them. Groups without choices retain their current selected value. The UI shows only clear option names, not site identifiers or debugging text.
2. The owner selects a combination and requests a preview. A fresh browser visit applies the requested choices by matching the current Dell option group and option label. The app rejects missing or ambiguous matches. It waits for the selected indicators and the active purchase price to settle, then records the full set of selected options and the final price together.
3. The preview displays the selected configuration as labeled rows, the exact USD purchase price, and the Dell product identity. Only explicit confirmation creates the card.
4. The card stores the source URL, a normalized selection recipe, and the last verified configuration. Distinct recipes under the same URL create distinct cards. Re-adding an identical recipe offers the existing card instead of silently duplicating it.
5. Each later check reopens the source URL, reapplies that card's recipe, and validates the resulting selected options and product identity before accepting the price. If Dell's selector behavior changes, an option disappears, or the verified configuration differs, the check fails or enters the existing `needs_attention` flow; it does not replace the trusted observation with the default page price.

The active Dell purchase price is authoritative for a user-selected combination. Static JSON-LD or metadata may describe the default configuration and is corroborating evidence only when it matches the active selection; a stale static value must not overwrite the visible configured price. A configured offer is accepted only when the selected option set is complete, its price is a valid visible USD amount, and the buy-box state is stable. The original default-configuration extraction remains supported.

The selection recipe uses normalized human-readable group and option labels, with site IDs retained as optional hints. It never relies solely on positional CSS selectors or a changing URL. Newly loaded option groups are rediscovered at each check. The exact verified option set is stored so a changed site default cannot silently alter the monitored configuration.

## Data and compatibility

Add an optional JSON field for the Dell selection recipe on `Product` through a versioned database migration. Existing products have no recipe and continue monitoring the page's default configuration. Store structured CPU, GPU, memory, storage, display, OS, and other selected fields in product and observation configuration JSON alongside the existing summary. Old summary-only records remain readable by splitting the summary into display rows; a successful future check populates structured fields without deleting history.

Identity comparison remains per product and per configuration fingerprint. Different cards may share a URL and model/SKU but must have separate observations, historical lows, targets, and notification state. A configuration mismatch must pause ordinary price-change comparison until confirmed. Existing price history remains indefinitely until the owner deliberately deletes it, matching the original retention policy.

## Dashboard and detail UI

- Each card presents CPU, graphics, memory, storage, and display on separate labeled rows, omitting missing fields. The preview and detail page use the same row presentation; long values wrap cleanly.
- Clicking a dashboard card selects it. The summary price shows that card's currency and latest trusted price, with a selected border and accessible pressed/selected state. A separate clear “查看详情” link opens its page. The initial selection is the first available card; a previously selected card can be remembered locally if it still exists. USD and CNY are never compared or converted in this summary.
- The detail page displays a real historical price chart from that card's trusted observations, with readable date and price labels. One record is a visible point labeled “等待更多价格记录”; two or more records form a line. The existing table and CSV export remain. Unchanged checks continue creating at most one daily checkpoint, so the chart truthfully reflects recorded history rather than inventing intra-day points.
- The dashboard and detail page show an obvious “删除/移除” action. The default action archives a card and preserves its history; an “已归档” view lets the owner restore it or proceed to permanent deletion. Permanent deletion requires a separate confirmation and removes that card's history and associated notification logs only. No deletion occurs during upgrade or migration.
- Replace the text glyph with a compact blue PriceWatch SVG mark in the header, login/initialization pages, and browser favicon. The mark is local, not loaded from a third party, and works in light/dark themes.

## Error handling and security

The existing admin session and CSRF protections apply to selection, save, archive, restore, and delete actions. The URL safety checks and Dell guarded proxy remain in force. Only public Dell page options are selected; no cart or account actions are performed. Parsing never renders raw Dell HTML in the app. Option catalogs and preview tokens are short-lived. A stale catalog or ambiguous choice requires a fresh preview. Fetches remain sequential and bounded for ten or fewer products.

## Verification and rollout

Use failing tests first for configuration recipes, same-URL card separation, option mismatch rejection, structured/legacy configuration rendering, dashboard selection across USD and CNY, archive/restore/delete visibility, favicon responses, and zero/one/multiple-point charts. Run the full Python checks and container UI smoke. On the NAS, test the existing default Dell card and one distinct selection against the live page without modifying the owner's saved cards. Take the existing database backup before migration; deploy the updated application without recreating `/data`. Only after the new flow succeeds should the owner add the extra configuration card. GitHub PR and CI must pass before publishing the new image.
