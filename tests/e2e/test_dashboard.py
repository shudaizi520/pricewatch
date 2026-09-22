"""Markup-level accessibility and theme smoke tests without a running browser."""

from pathlib import Path


def test_dashboard_has_semantic_navigation_and_visible_theme_controls():
    template = (Path(__file__).parents[2] / "src/pricewatch/templates/base.html").read_text()
    assert 'aria-label="主导航"' in template
    assert 'name="theme" value="dark"' in template
    assert 'name="theme" value="light"' in template
