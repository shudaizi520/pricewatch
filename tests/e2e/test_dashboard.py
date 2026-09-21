"""Markup-level accessibility and theme smoke tests without a running browser."""

from pathlib import Path


def test_dashboard_has_semantic_navigation_and_visible_theme_controls():
    template = (Path(__file__).parents[2] / "src/pricewatch/templates/base.html").read_text()
    assert 'aria-label="主导航"' in template
    assert 'name="theme" value="dark"' in template
    assert 'name="theme" value="light"' in template


def test_dashboard_has_keyboard_card_selection_and_separate_details():
    templates = Path(__file__).parents[2] / "src/pricewatch/templates"
    dashboard = (templates / "dashboard.html").read_text()
    script = (Path(__file__).parents[2] / "src/pricewatch/static/app.js").read_text()
    assert 'class="card-select"' in dashboard
    assert 'aria-pressed="' in dashboard
    assert "查看详情" in dashboard
    assert "#selected-price" in script
