"""가독성 요구가 실제 공개 사이트에서 지켜지는지 — 색 규약·대비·모바일.

CEO는 아이폰으로 본다. 그러니 "코드에 CSS를 넣었다"가 아니라 "좁은 화면에서
표가 페이지를 밀어내지 않고, 다크모드에서도 색이 읽힌다"가 인수 기준이다.
스크린샷을 테스트에 넣을 수 없으므로 그 두 가지를 만드는 구조(가로 스크롤
래퍼, 대비비, 미디어쿼리)를 직접 검사한다.
"""
from __future__ import annotations

import re

from market_intel import site as site_mod
from market_intel.reporting.model import FactRow

from tests.publish.conftest import make_report, write_report


# --- CSS 파싱 도우미 --------------------------------------------------------

def _var(css: str, name: str, *, scope: str | None = None) -> str:
    """`--name`의 마지막 정의값. `scope`를 주면 그 블록 안에서만 찾는다."""
    text = css
    if scope is not None:
        start = css.index(scope)
        depth, i = 0, start
        while i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        text = css[start:i]
    found = re.findall(rf"{re.escape(name)}\s*:\s*(#[0-9a-fA-F]{{3,8}})", text)
    assert found, f"{name} not defined in {'the whole stylesheet' if scope is None else scope}"
    return found[-1]


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(hex_colour: str) -> float:
    def chan(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(v) for v in _rgb(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _css(docs_root, conn, reports_root) -> str:
    write_report(reports_root, make_report("morning", "2026-08-01"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    return (docs_root / "style.css").read_text(encoding="utf-8")


# --- ③ 국내 증시 관례: 상승 = 빨강, 하락 = 파랑 ----------------------------

def test_up_is_red_and_down_is_blue(reports_root, docs_root, conn):
    """국내 관례. 미국식 초록/빨강이 아니다 — CEO가 한국 시장을 함께 본다."""
    css = _css(docs_root, conn, reports_root)
    up_r, up_g, up_b = _rgb(_var(css, "--up", scope=":root"))
    dn_r, dn_g, dn_b = _rgb(_var(css, "--down", scope=":root"))
    assert up_r > up_g and up_r > up_b, f"상승색이 빨강이 아니다: {up_r},{up_g},{up_b}"
    assert dn_b > dn_r and dn_b > dn_g, f"하락색이 파랑이 아니다: {dn_r},{dn_g},{dn_b}"
    assert ".up" in css and ".down" in css


def test_colour_contrast_holds_in_light_and_dark(reports_root, docs_root, conn):
    """다크모드에서 색 대비가 유지되어야 한다(WCAG AA 본문 기준 4.5:1)."""
    css = _css(docs_root, conn, reports_root)
    dark_scope = "@media (prefers-color-scheme: dark)"
    assert dark_scope in css, "다크모드 대응이 없다"

    light = {n: _var(css, n, scope=":root") for n in ("--up", "--down", "--bg")}
    dark = {n: _var(css, n, scope=dark_scope) for n in ("--up", "--down", "--bg")}
    assert dark["--bg"] != light["--bg"], "다크모드가 배경을 바꾸지 않는다"

    for theme, palette in (("light", light), ("dark", dark)):
        for name in ("--up", "--down"):
            ratio = _contrast(palette[name], palette["--bg"])
            assert ratio >= 4.5, f"{theme} {name} 대비 {ratio:.2f}:1"


def test_report_tables_scroll_on_a_narrow_screen(reports_root, docs_root, conn):
    """아이폰(375px)에서 표가 페이지를 밀어내면 안 된다 — 기존 `.scroll` 관행."""
    css = _css(docs_root, conn, reports_root)
    assert re.search(r"\.scroll\s*\{[^}]*overflow-x\s*:\s*auto", css), css

    page = (docs_root / "reports" / "morning" / "2026-08-01.html").read_text(encoding="utf-8")
    tables = page.count("<table>")
    wrapped = page.count('<div class="scroll"><table>')
    assert tables > 0 and wrapped == tables, f"{tables}개 표 중 {wrapped}개만 감쌌다"


def test_hero_cards_wrap_instead_of_overflowing(reports_root, docs_root, conn):
    css = _css(docs_root, conn, reports_root)
    hero = re.search(r"\.hero\s*\{([^}]*)\}", css)
    assert hero, "히어로 카드 스타일이 없다"
    assert "flex-wrap" in hero.group(1) and "wrap" in hero.group(1), hero.group(1)


def test_sparkline_colour_comes_from_the_direction_class(reports_root, docs_root, conn):
    """스파크라인은 currentColor로 그리고, 색은 .up/.down 클래스가 정한다 —
    인라인 색 지정이 다크모드를 빠져나가지 못하게."""
    css = _css(docs_root, conn, reports_root)
    assert ".spark" in css and "currentColor" in css


# --- 새 마크업도 이스케이프를 지난다 ----------------------------------------

def test_new_markup_still_escapes_hostile_input(reports_root, docs_root, conn):
    """색·화살표·스파크라인을 붙이면서 site.py가 자체 조립으로 우회하면
    render_html의 escape + URL 허용목록을 뚫는다."""
    evil = '<img src=x onerror="alert(1)">'
    rep = make_report("morning", "2026-08-01", facts=[
        FactRow(label=evil, value=evil, comparison=evil,
                source_url="javascript:alert(1)", data_status="partial",
                known_at="2026-07-31T22:00:00+00:00", subject="X", metric="price_close",
                delta_pct=1.5, series=[1.0, 2.0, 3.0]),
    ])
    write_report(reports_root, rep)
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    for path in sorted(docs_root.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        # `onerror=` 자체는 이스케이프된 본문에도 글자로 남는다. 문제는 그것이
        # **살아 있는 태그**가 되는 것이므로 태그 시작을 본다.
        assert "<img" not in text, f"raw tag injected into {path}"
        assert 'href="javascript:' not in text.lower(), path
        assert "<script" not in text.lower(), path
    page = (docs_root / "reports" / "morning" / "2026-08-01.html").read_text(encoding="utf-8")
    assert "&lt;img" in page
    assert "<svg" in page, "정상적인 시계열은 여전히 그려져야 한다"
