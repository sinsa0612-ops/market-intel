"""백필 append 프리미티브 (spec 백필 §4 S5).

**`upsert_fact`를 쓰지 않는다.** 라이브 경로의 "직전 판과 값이 같으면 생략"
규칙은 백필에서 정확히 틀린 규칙이다: 소스가 준 vintage는 값이 같아도 서로
다른 판(그때 알 수 있었던 사실)이고, 생략하면 그 시점의 진실이 원장에서
사라진다(spec 반증 3 — 가격 겹침 256건이 전부 이 경로였다).

멱등성 키는 **`(fact_id, known_at, value_num, value_text, unit, data_status)`**
— 완전히 같은 행이 이미 있으면 no-op이므로 두 번 돌려도 중복이 안 생긴다.
"""
from __future__ import annotations

import json
import sqlite3

from .. import db as db_mod

# spec S6: 실측 최대치가 관측 1건당 8판이다. 여기 걸리면 소스가 예상과 다르게
# 동작한다는 뜻이므로 조용히 자르지 않고 중단하고 보고한다 — 있었던 판을
# 임의로 버리면 그 시점의 진실이 사라진다.
MAX_REVISIONS_PER_FACT = 50


def append_vintage(
    conn: sqlite3.Connection,
    fact_id: str,
    snapshot_id: str | None,
    known_at: str,
    fc,
    correction_reason: str | None = None,
) -> bool:
    """`fact_id`의 계보에 vintage 한 판을 append한다. 새 행을 넣었으면 True,
    같은 판이 이미 있어 아무것도 하지 않았으면 False.

    `correction_reason`은 spec S4의 판별자(`'backfill:<source>'`)다. 이것이
    없으면 파생 FCF(`reconstructed` + `correction_reason IS NULL`)와 백필을
    SQL로 구분할 수 없다 — `data_status`만으로는 안 된다(spec 반증 1).
    """
    # 문자열로 사전식 비교되는 값이므로 반드시 정규화한다. `+09:00`이 그대로
    # 들어가면 차단선이 조용히 깨진다(db.py `iso_utc` 주석).
    known_at = db_mod.iso_utc(known_at)
    unit = fc.unit or ""
    value_text = fc.value_text or ""

    duplicate = conn.execute(
        """SELECT revision_no FROM fact_revisions
           WHERE fact_id=? AND known_at=? AND value_num IS ?
             AND IFNULL(value_text,'') IS ? AND IFNULL(unit,'') IS ?
             AND data_status IS ?
           LIMIT 1""",
        (fact_id, known_at, fc.value_num, value_text, unit, fc.data_status),
    ).fetchone()
    if duplicate is not None:
        return False

    stats = conn.execute(
        "SELECT COUNT(*) c, MAX(revision_no) m FROM fact_revisions WHERE fact_id=?",
        (fact_id,),
    ).fetchone()
    if stats["c"] >= MAX_REVISIONS_PER_FACT:
        raise RuntimeError(
            f"backfill 폭주 방지: fact_id={fact_id!r}의 revision이 이미 "
            f"{stats['c']}판이다(상한 {MAX_REVISIONS_PER_FACT}). 소스가 예상과 "
            f"다르게 동작하고 있다 — 판을 버리지 않고 중단한다."
        )

    # revision_no는 append 순번(PRIMARY KEY)일 뿐 시간축이 아니다(spec S1).
    revision_no = (stats["m"] or 0) + 1
    # supersedes = 이 known_at 시점에 직전까지 유효했던 판. S1의 정렬 기준
    # (known_at, revision_no)을 그대로 쓴다.
    previous = conn.execute(
        "SELECT revision_no FROM fact_revisions WHERE fact_id=? AND known_at<=? "
        "ORDER BY known_at DESC, revision_no DESC LIMIT 1",
        (fact_id, known_at),
    ).fetchone()

    conn.execute(
        """INSERT INTO fact_revisions
           (fact_id, revision_no, snapshot_id, observed_at, event_at, known_at, market, country,
            subject, category, metric, value_num, value_text, unit, comparison_basis, publisher,
            safe_source_url, data_status, correction_reason, supersedes_revision, session_label, extra_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fact_id, revision_no, snapshot_id, db_mod.iso_utc(), fc.event_at, known_at,
            fc.market, fc.country, fc.subject, fc.category, fc.metric, fc.value_num,
            fc.value_text, fc.unit, fc.comparison_basis, fc.publisher,
            getattr(fc, "safe_source_url", "") or None, fc.data_status,
            correction_reason, previous["revision_no"] if previous else None,
            fc.session_label,
            json.dumps(fc.extra, ensure_ascii=False) if fc.extra else None,
        ),
    )
    return True
