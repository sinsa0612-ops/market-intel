"""`krx top-kospi` CLI — 수급 표본 목록을 **손으로 적지 않기 위한** 명령.

`universe.KOSPI_FLOW_SAMPLE`은 코스피 시가총액 상위 N종목이다. 이 목록을 손으로
관리하면 순위가 바뀌어도 아무도 모르고, 어느 시점 기준인지도 사라진다. 이
명령은 KRX 전종목 시세(`MKTCAP`)에서 현재 순위를 뽑아 **붙여넣을 수 있는 파이썬
블록 그대로** 찍는다 — 갱신하면 git diff에 남는다.

자동으로 파일을 고치지 않는 이유: 관측 대상이 바뀌는 것은 사람이 보고 넘겨야
하는 변경이다. 조용히 바뀌면 어제 리포트와 오늘 리포트가 다른 종목군을 재고도
같은 이름으로 불린다.

우선주는 뺀다 — 기준은 **종목코드 끝자리**(보통주는 `0`). 이름에서 `우`를 찾는
방식은 `CJ4우(전환)`·`아모레퍼시픽홀딩스3우C`를 놓친다(2026-08-03 실측).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta

from .http_client import SafeHttp

KRX_STOCK_DAILY = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"
DEFAULT_TOP_N = 20
# 기준일을 며칠까지 거슬러 시도할지. 주말·공휴일에 돌리면 그날 데이터가 없다.
MAX_LOOKBACK_DAYS = 7


def register(sub) -> None:
    p = sub.add_parser("krx")
    krx_sub = p.add_subparsers(dest="krx_command", required=True)
    p_top = krx_sub.add_parser("top-kospi")
    p_top.add_argument("--n", type=int, default=DEFAULT_TOP_N)
    p_top.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 최근 거래일)")
    p_top.add_argument("--include-preferred", action="store_true", dest="include_preferred",
                       help="우선주도 포함(기본은 제외)")


def is_common_stock(isu_cd: str) -> bool:
    """보통주인가. 코스피 종목코드는 보통주가 `0`으로 끝나고 우선주는
    `5`/`7`/`9`/`K`/`L` 등으로 끝난다(2026-08-03 실측: 943종목 중 833개가 `0`,
    나머지 110개는 전부 우선주류)."""
    return bool(isu_cd) and isu_cd.endswith("0")


def _fetch(client, api_key: str, bas_dd: str) -> list[dict]:
    resp = client.get(KRX_STOCK_DAILY, params={"basDd": bas_dd},
                      headers={"AUTH_KEY": api_key})
    if resp.status_code != 200:
        raise RuntimeError(f"KRX http {resp.status_code}")
    return (json.loads(resp.text) or {}).get("OutBlock_1") or []


def _mktcap(row: dict) -> int:
    try:
        return int(row.get("MKTCAP") or 0)
    except (TypeError, ValueError):
        return 0


def rank_kospi(rows: list[dict], n: int, *, include_preferred: bool) -> tuple[list[dict], float]:
    """-> (상위 n종목, 코스피 전체 시가총액 대비 비중 %).

    비중의 분모는 **우선주를 포함한 코스피 전체**다. 우선주를 뺀 채 분모까지
    보통주로 좁히면 커버리지가 실제보다 커 보인다 — 우리가 못 보는 부분을
    작게 보이게 만드는 계산이다."""
    kospi = [r for r in rows if r.get("MKT_NM") == "KOSPI"]
    total = sum(_mktcap(r) for r in kospi)
    pool = kospi if include_preferred else [r for r in kospi if is_common_stock(r.get("ISU_CD", ""))]
    pool = sorted(pool, key=_mktcap, reverse=True)[:n]
    coverage = (sum(_mktcap(r) for r in pool) / total * 100) if total else 0.0
    return pool, coverage


def run(args, settings) -> int:
    if not settings.krx_api_key:
        print("krx: MI_KRX_API_KEY가 비어 있음", file=sys.stderr)
        return 1

    client = SafeHttp("krx", settings)
    wanted = args.date.replace("-", "") if args.date else None

    rows: list[dict] = []
    used = ""
    day = datetime.strptime(wanted, "%Y%m%d").date() if wanted else date.today()
    for _ in range(MAX_LOOKBACK_DAYS):
        used = day.strftime("%Y%m%d")
        try:
            rows = _fetch(client, settings.krx_api_key, used)
        except Exception as exc:  # noqa: BLE001
            print(f"krx: 조회 실패 {used} — {exc.__class__.__name__}: {exc}", file=sys.stderr)
            return 1
        if rows:
            break
        # 주말·공휴일이면 빈 응답이 온다. 하루씩 거슬러 올라가 본다.
        day -= timedelta(days=1)
        if wanted:
            break  # 날짜를 명시했으면 그날만 본다 — 조용히 다른 날을 쓰면 안 된다
    if not rows:
        print(f"krx: {used} 기준 데이터 없음(휴장일이거나 아직 미공시)", file=sys.stderr)
        return 1

    top, coverage = rank_kospi(rows, args.n, include_preferred=args.include_preferred)
    kospi_n = sum(1 for r in rows if r.get("MKT_NM") == "KOSPI")
    common_n = sum(1 for r in rows
                   if r.get("MKT_NM") == "KOSPI" and is_common_stock(r.get("ISU_CD", "")))
    pool_desc = f"코스피 {kospi_n}개" if args.include_preferred else f"코스피 보통주 {common_n}개"

    iso = f"{used[:4]}-{used[4:6]}-{used[6:]}"
    print(f"# 기준일 {iso} · {pool_desc} 중 상위 {len(top)} · 시총 비중 {coverage:.1f}%")
    print("KOSPI_FLOW_SAMPLE: tuple[tuple[str, str], ...] = (")
    for r in top:
        print(f'    ("{r["ISU_CD"]}.KS", "{r["ISU_NM"]}"),')
    print(")")
    print(f'KOSPI_FLOW_SAMPLE_AS_OF = "{iso}"')
    print(f"KOSPI_FLOW_SAMPLE_COVERAGE_PCT = {coverage:.1f}")
    return 0


def dispatch(args, settings):
    """`cli.py`의 확장 규약 — 내 서브커맨드가 아니면 `None`을 돌려 다음 모듈에
    넘긴다."""
    if getattr(args, "command", None) != "krx":
        return None
    if args.krx_command == "top-kospi":
        return run(args, settings)
    return 2
