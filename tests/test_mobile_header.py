"""The header on a phone.

The Settings button was placed inside the skin picker, which the mobile stylesheet hides -- so
on the device the app is mostly used on, Settings could not be opened at all.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "dashboard.html").read_text()
CSS = (ROOT / "static" / "css" / "app.css").read_text()


def _skins_block() -> str:
    start = HTML.index('<div class="skins">')
    return HTML[start:HTML.index("</div>", start)]


def test_the_stylesheet_does_hide_the_skin_picker_on_phones():
    assert re.search(r"\.top \.livechip,\.top \.skins\{display:none\}", CSS)   # the premise below


def test_the_settings_button_is_not_inside_something_phones_hide():
    assert "openSettings()" in HTML
    assert "openSettings()" not in _skins_block()


def test_the_phone_header_gives_the_nav_its_own_row():
    phone = CSS[CSS.index("@media (max-width:640px){\n    .top{"):]
    phone = phone[:phone.index("\n  }") + 4]
    assert "flex-wrap:wrap" in phone and re.search(r"\.top \.navlist\{[^}]*width:100%", phone)
