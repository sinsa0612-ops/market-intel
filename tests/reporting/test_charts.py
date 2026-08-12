"""리포트 그림 3종 (CEO 지시 2026-08-12: "시각화가 충분하지 않다").

CEO는 파이차트를 요청했지만 **넣지 않았다.** 사외 고문 2인이 독립적으로 같은
결론을 냈고 근거도 같다 — 이 리포트의 핵심 수치인 순매수는 부호가 있어 조각으로
나눌 수 없고(음수를 각도로 표현할 방법이 없다), 하루치 구성만 보여 주는 그림은
매일 읽는 문서에서 날짜 비교가 안 된다. 대신 세 장을 넣는다:

  breadth  — 상승/하락 종목 수를 0축 위아래로, 지수 등락률을 점으로 겹친다.
             실측 2026-08-03 코스피는 지수 -5.1%인데 오른 종목 455 / 내린 419였다.
  flows    — 투자자 주체별 순매수(+)/순매도(-). 표는 "오늘 누가", 그림은 "며칠째".
  rebased  — 기준일=100. 실측으로 SK하이닉스 -25.5% vs 미 반도체 -4.5%가 한눈에.

이 파일이 지키는 계약 셋:
 1. 부호는 **위치**가 전달한다(색 없이도 읽힌다).
 2. 정보를 담은 그림이므로 `role="img"` + title/desc가 있고, 마크다운에는
    같은 내용이 **문장으로** 실린다(spec §3: 마크다운에 SVG 금지).
 3. 그릴 것이 모자라면 그림이 **아예 없다**(빈 축은 정보가 아니라 잡음이다).
"""
from __future__ import annotations

import re

import pytest

from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod
from market_intel.reporting.model import ChartBlock, ChartSeries, Report


def _breadth_block(n: int = 5) -> ChartBlock:
    dates = [f"2026-08-{d:02d}" for d in range(3, 3 + n)]
    return ChartBlock(
        kind="breadth", title="코스피 시장 폭과 지수", dates=dates, unit="종목",
        series=[ChartSeries(label="오른 종목", values=[455, 788, 675, 490, 554][:n]),
                ChartSeries(label="내린 종목", values=[-419, -96, -197, -380, -322][:n])],
        overlay=[ChartSeries(label="지수 등락률", values=[-5.1, 1.5, 3.8, -4.6, -0.6][:n])],
        note="지수 -5.1%인데 오른 종목 455 / 내린 종목 419였다.")


def test_breadth_bars_straddle_the_zero_axis():
    """⚠️ 이 그림의 존재 이유. 상승은 0축 위, 하락은 아래여야 "지수는 빠졌는데
    오른 종목이 더 많다"가 눈에 보인다. 둘 다 같은 쪽에 그리면 그 구조가 사라진다."""
    svg = render_html_mod.chart_svg(_breadth_block())
    axis = re.search(r'<line class="axis" x1="[\d.]+" y1="([\d.]+)"', svg)
    assert axis, svg[:200]
    zero_y = float(axis.group(1))

    ups = [float(m) for m in re.findall(r'<rect class="s0" x="[\d.]+" y="([\d.]+)"', svg)]
    downs = [float(m) for m in re.findall(r'<rect class="s1" x="[\d.]+" y="([\d.]+)"', svg)]
    assert ups and downs
    assert all(y < zero_y for y in ups), "상승 막대가 0축 위에 있어야 한다"
    # 하락 막대는 0축에서 시작해 아래로 내려간다(y = 0축, height > 0).
    assert all(y >= zero_y - 0.01 for y in downs), "하락 막대가 0축 아래에 있어야 한다"


def test_diverging_scale_is_symmetric():
    """위아래 배율이 다르면 '더 큰 쪽'이 눈으로 뒤집힌다.

    ⚠️ **막대 길이 비로는 이 계약을 잴 수 없다.** 어떤 선형 배율에서도 10:-2는
    길이 5:1로 나오므로, 그 축의 단언은 배율을 비대칭으로 바꿔도 통과한다 —
    실제로 이 테스트의 첫 두 판이 그랬고 변이 주입에서 초록이었다.

    대칭 배율의 진짜 계약은 **0축이 그림의 세로 한가운데 온다**는 것이다.
    비대칭이면 0축이 값 분포를 따라 위아래로 밀린다(+10/-2에서는 아래쪽으로).
    """
    block = ChartBlock(
        kind="flows", title="t", dates=["2026-08-03", "2026-08-04"], unit="조 원",
        series=[ChartSeries(label="a", values=[10.0, -2.0])])
    svg = render_html_mod.chart_svg(block)
    zero_y = float(re.search(r'<line class="axis" x1="[\d.]+" y1="([\d.]+)"', svg).group(1))

    top = render_html_mod.CHART_PAD_T
    bottom = render_html_mod.CHART_H - render_html_mod.CHART_PAD_B
    assert zero_y == pytest.approx((top + bottom) / 2, abs=0.5), (
        "치우친 표본에서도 0축은 한가운데여야 한다(대칭 배율)")

    rects = re.findall(
        r'<rect class="s0" x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"', svg)
    assert len(rects) == 2
    (y_pos, _h_pos), (y_neg, _h_neg) = [(float(a), float(b)) for a, b in rects]
    assert y_pos < zero_y <= y_neg + 0.01


def test_chart_is_announced_to_screen_readers():
    """스파크라인은 옆 칸 숫자를 거드는 장식이라 aria-hidden이지만, 이 그림들은
    그 자체가 정보다 — 낭독기에서 사라지면 그 자리가 통째로 말을 못 한다."""
    svg = render_html_mod.chart_svg(_breadth_block())
    assert 'role="img"' in svg
    assert "<title" in svg and "<desc" in svg
    assert "aria-hidden" not in svg
    assert "<figcaption>" in svg, "그림과 같은 내용의 문장이 함께 실려야 한다"


def test_no_external_resources_or_script():
    """사이트 정책(spec B8): 외부 라이브러리·CDN·<script> 금지."""
    for block in (_breadth_block(), _rebased_block()):
        svg = render_html_mod.chart_svg(block)
        assert "<script" not in svg.lower()
        assert "http://" not in svg and "https://" not in svg
        assert "url(" not in svg


def test_colour_is_not_inline():
    """인라인 색은 다크모드 미디어쿼리를 그냥 빠져나간다(`sparkline_svg`와 같은 규칙).
    색은 클래스가 정하고 스타일시트가 섞는다."""
    svg = render_html_mod.chart_svg(_breadth_block())
    assert "fill=" not in svg and "stroke=" not in svg, svg[:300]


def _rebased_block() -> ChartBlock:
    return ChartBlock(
        kind="rebased", title="2026-07-14 = 100 기준 상대 추이",
        dates=["2026-08-03", "2026-08-04", "2026-08-05"], unit="=100",
        series=[ChartSeries(label="코스피", values=[100.0, 101.0, 105.0]),
                ChartSeries(label="SK하이닉스", values=[100.0, 96.0, 74.5])],
        note="같은 기간 상대 성적 — 코스피 +5.0% · SK하이닉스 -25.5%.")


def test_rebased_labels_sit_at_the_line_end_not_in_a_legend():
    """범례를 따로 두면 눈이 그림과 범례를 오가야 하고, 흑백에서는 어느 선이
    누구인지 못 가린다. 이름은 선 끝에 직접 붙인다."""
    svg = render_html_mod.chart_svg(_rebased_block())
    assert svg.count('<path class="line') == 2
    assert "코스피" in svg and "SK하이닉스" in svg
    # 계열마다 다른 클래스 -> 스타일시트가 선 패턴을 다르게 줄 수 있다.
    assert 'class="line s0"' in svg and 'class="line s1"' in svg


def test_series_get_distinct_classes_for_black_and_white():
    svg = render_html_mod.chart_svg(_breadth_block())
    assert 'class="s0"' in svg and 'class="s1"' in svg


@pytest.mark.parametrize("block", [
    ChartBlock(kind="breadth", title="t", dates=[], series=[]),
    ChartBlock(kind="breadth", title="t", dates=["2026-08-03"],
               series=[ChartSeries(label="a", values=[None])]),
    ChartBlock(kind="rebased", title="t", dates=["2026-08-03"],
               series=[ChartSeries(label="a", values=[100.0])]),
    ChartBlock(kind="아직없는종류", title="t", dates=["2026-08-03", "2026-08-04"],
               series=[ChartSeries(label="a", values=[1.0, 2.0])]),
])
def test_nothing_to_draw_means_no_chart(block):
    """빈 축이나 점 하나짜리 그림은 정보가 아니라 잡음이다 — `sparkline_svg`와
    같은 관례로 빈 문자열을 낸다."""
    assert render_html_mod.chart_svg(block) == ""


def test_markdown_gets_the_sentence_not_the_svg():
    """마크다운에는 SVG를 넣지 않는다(spec §3). 대신 같은 정보를 문장으로 —
    옵시디언으로만 읽는 사람에게서 사실이 사라지면 안 된다."""
    report = Report(report_type="morning", report_date="2026-08-12",
                    charts=[_breadth_block()])
    md = render_md_mod.render_markdown(report)
    assert "<svg" not in md
    assert "코스피 시장 폭과 지수" in md
    assert "오른 종목 455" in md


def test_html_carries_the_svg_for_the_same_report():
    report = Report(report_type="morning", report_date="2026-08-12",
                    charts=[_breadth_block()])
    html = render_html_mod.render_html(report)
    assert "<svg" in html and 'class="chart chart-breadth"' in html


def test_old_reports_without_charts_still_render():
    """`site build`는 `reports/`의 **모든** JSON에서 사이트를 다시 만든다.
    이 필드가 없던 옛 리포트 하나가 사이트 전체를 무너뜨리면 안 된다."""
    old = Report(report_type="morning", report_date="2026-07-28")
    raw = old.to_json()
    import json as _json
    d = _json.loads(raw)
    d.pop("charts")
    restored = Report.from_json(_json.dumps(d, ensure_ascii=False))
    assert restored.charts == []
    assert "<svg" not in render_md_mod.render_markdown(restored)
    render_html_mod.render_html(restored)  # 예외 없이 그려져야 한다


def test_charts_survive_a_json_round_trip():
    report = Report(report_type="morning", report_date="2026-08-12",
                    charts=[_breadth_block(), _rebased_block()])
    again = Report.from_json(report.to_json())
    assert [c.kind for c in again.charts] == ["breadth", "rebased"]
    assert again.charts[0].overlay[0].values[0] == pytest.approx(-5.1)
    assert again.charts[1].series[1].label == "SK하이닉스"
