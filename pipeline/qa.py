"""자동 점검. 임계값을 넘으면 종료 코드 1 로 파이프라인을 세운다.

점검 항목은 각각 독립된 함수다(A15 방식). 결과는 표로 만들어 리포트 끝에 붙이고, JSON 으로도 남긴다.
판정은 A06 방식으로 셋이다. ok / warn(표시만) / fail(정지).
"""
from __future__ import annotations

import datetime as dt
import json
import statistics

from . import api
from .load import connect
from .metrics import good_metrics, survey_dates
from .quantity import grams_per_pack

CFG = api.ROOT / "config"
OUT_JSON = api.DATA / "qa_latest.json"
OUT_DIR = api.ROOT / "docs" / "qa"


def _th():
    return json.loads((CFG / "qa_thresholds.json").read_text(encoding="utf8"))


def _iso(d8: str) -> str:
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"


# 각 점검은 dict(name, status, value, threshold, message) 를 돌려준다.

def check_survey_gap(con, th, today: dt.date):
    days = survey_dates(con)
    if not days:
        return {"name": "조사 공백", "status": "fail", "value": None, "threshold": "-", "message": "적재된 조사일이 하나도 없다"}
    last = dt.datetime.strptime(days[-1], "%Y%m%d").date()
    gap = (today - last).days
    t = th["survey_gap_days"]
    status = "fail" if gap > t["fail"] else "warn" if gap > t["warn"] else "ok"
    msg = f"마지막 조사가 {_iso(days[-1])}이고 {gap}일 지났다. 격주 조사라 14일까지는 정상이다"
    if status != "ok":
        msg += ". 조사가 밀렸거나 수집 대상이 막혔을 수 있다"
    return {"name": "조사 공백", "status": status, "value": gap, "threshold": f"{t['warn']}일 넘으면 주의, {t['fail']}일 넘으면 실패", "message": msg}


def check_rows(con, th):
    counts = con.execute("SELECT inspect_day, COUNT(*) FROM prices GROUP BY 1 ORDER BY 1").fetchall()
    if not counts:
        return {"name": "수집 건수", "status": "fail", "value": 0, "threshold": ">0", "message": "가격 행이 없다"}
    latest_day, latest_n = counts[-1]
    if latest_n == 0:
        return {"name": "수집 건수", "status": "fail", "value": 0, "threshold": ">0", "message": f"{_iso(latest_day)} 수집 0건"}
    if len(counts) == 1:
        return {"name": "수집 건수", "status": "ok", "value": latest_n, "threshold": "-", "message": f"{_iso(latest_day)} {latest_n:,}건. 비교할 이전 조사가 아직 없다"}
    med = statistics.median(c for _, c in counts[:-1])
    ratio = latest_n / med
    t = th["rows_ratio_min"]
    status = "fail" if ratio < t["fail"] else "warn" if ratio < t["warn"] else "ok"
    return {"name": "수집 건수", "status": status, "value": latest_n, "threshold": f"이전 조사 중앙값 {med:,.0f}건의 {t['warn']:.0%} 이상",
            "message": f"{_iso(latest_day)} {latest_n:,}건으로 이전 조사 중앙값의 {ratio:.0%}다"}


def check_tieout(con):
    # 가장 최근 조사일의 마지막 적재 기록을 본다 (적재 순서가 아니라 조사일 기준)
    r = con.execute("SELECT inspect_day, raw_count, loaded_count, dup_in_raw, ok FROM load_log ORDER BY inspect_day DESC, loaded_at DESC LIMIT 1").fetchone()
    if not r:
        return {"name": "원본 대조", "status": "fail", "value": None, "threshold": "일치", "message": "적재 기록이 없다"}
    day, raw, loaded, dup, ok = r
    return {"name": "원본 대조", "status": "ok" if ok else "fail", "value": f"{raw:,} / {loaded:,}", "threshold": "원본 건수와 DB 행 수가 같을 것",
            "message": f"{_iso(day)} 원본 {raw:,}건(중복 {dup}), DB {loaded:,}행"}


def check_duplicates(con):
    r = con.execute("SELECT inspect_day, dup_in_raw FROM load_log ORDER BY inspect_day DESC, loaded_at DESC LIMIT 1").fetchone()
    dup = r[1] if r else 0
    return {"name": "원본 중복", "status": "warn" if dup else "ok", "value": dup, "threshold": "0",
            "message": f"같은 조사일·판매점·상품이 원본에 {dup}번 겹친다" if dup else "겹치는 행이 없다"}


def check_coverage(con, th, day):
    total = con.execute("SELECT COUNT(*) FROM goods WHERE smlcls_code LIKE '0301%' OR smlcls_code LIKE '0302%'").fetchone()[0]
    with_price = con.execute("""SELECT COUNT(DISTINCT p.good_id) FROM prices p JOIN goods g USING(good_id)
                                WHERE p.inspect_day=? AND (g.smlcls_code LIKE '0301%' OR g.smlcls_code LIKE '0302%')""", (day,)).fetchone()[0]
    ratio = with_price / total if total else 0
    t = th["goods_coverage_min"]["warn"]
    return {"name": "상품 커버리지", "status": "ok" if ratio >= t else "warn", "value": f"{with_price}/{total}", "threshold": f"≥{t:.0%}",
            "message": f"식품 {total}개 중 {with_price}개에 가격이 있다 ({ratio:.0%})"}


def check_match_rate(con, th):
    from .metrics import protein_categories
    codes = sorted(protein_categories())
    if not codes:
        return {"name": "매칭률", "status": "fail", "value": None, "threshold": "-", "message": "순위 대상 소분류가 config에 없다"}
    q = ",".join("?" * len(codes))
    r = con.execute(f"""SELECT SUM(CASE WHEN m.food_cd IS NOT NULL THEN 1 ELSE 0 END),
                               SUM(CASE WHEN m.method LIKE 'excluded%' THEN 1 ELSE 0 END), COUNT(*)
                        FROM matches m JOIN goods g USING(good_id)
                        WHERE g.smlcls_code IN ({q})""", codes).fetchone()
    matched, excluded, total = (r[0] or 0), (r[1] or 0), (r[2] or 0)
    if total == 0:
        return {"name": "매칭률", "status": "fail", "value": None, "threshold": "-", "message": "매칭을 아직 돌리지 않았다 (python pw.py match)"}
    eligible = total - excluded
    ratio = matched / eligible if eligible else 0
    t = th["match_rate_min"]
    status = "fail" if ratio < t["fail"] else "warn" if ratio < t["warn"] else "ok"
    return {"name": "매칭률", "status": status, "value": f"{matched}/{eligible}", "threshold": f"{t['warn']:.0%} 밑이면 주의, {t['fail']:.0%} 밑이면 실패",
            "message": f"단백질 순위 대상 상품 {eligible}개(사람이 뺀 {excluded}개 제외) 중 {matched}개에 영양 항목이 붙었다 ({ratio:.0%})"}


def check_price_jump(con, th, day, prev):
    if not prev:
        return {"name": "가격 급변", "status": "ok", "value": 0, "threshold": "-", "message": "비교할 이전 조사가 없다"}
    cur = {r[0]: r[1] for r in con.execute("SELECT good_id, price FROM prices WHERE inspect_day=? AND price>0", (day,)).fetchall() and
           [(gid, statistics.median(v)) for gid, v in _group(con, day).items()]}
    pre = {gid: statistics.median(v) for gid, v in _group(con, prev).items()}
    t = th["price_jump_ratio"]
    jumps = []
    for gid, c in cur.items():
        p = pre.get(gid)
        if p and (c / p > t["up"] or c / p < t["down"]):
            jumps.append((gid, p, c, c / p))
    share = len(jumps) / len(cur) if cur else 0
    names = {r[0]: r[1] for r in con.execute("SELECT good_id, good_name FROM goods")}
    ex = ", ".join(f"{names.get(g, g)} {p:,.0f}원에서 {c:,.0f}원({r:.1f}배)" for g, p, c, r in sorted(jumps, key=lambda x: -abs(x[3] - 1))[:5])
    return {"name": "가격 급변", "status": "warn" if share > t["warn_share"] else "ok", "value": len(jumps), "threshold": f"상품의 {t['warn_share']:.0%} 미만",
            "message": f"{_iso(prev)} 조사보다 중앙값이 {t['up']}배 넘게 오르거나 {t['down']}배 밑으로 떨어진 상품이 {len(jumps)}개({share:.1%})다" + (f". 예를 들어 {ex}" if ex else "")}


def _group(con, day):
    g = {}
    for gid, price in con.execute("SELECT good_id, price FROM prices WHERE inspect_day=? AND price>0", (day,)):
        g.setdefault(gid, []).append(price)
    return g


def check_units(con, th):
    t = th["egg_grams_per_unit"]
    bad = []
    nograms = []
    for r in con.execute("SELECT good_id, good_name, smlcls_code, unit_div, base_cnt, total_cnt, total_div, detail_mean FROM goods WHERE smlcls_code LIKE '0301%' OR smlcls_code LIKE '0302%'"):
        g = dict(zip(["good_id", "good_name", "smlcls_code", "unit_div", "base_cnt", "total_cnt", "total_div", "detail_mean"], r))
        grams, basis = grams_per_pack(g)
        if grams is None:
            nograms.append(g["good_name"])
        elif g["smlcls_code"] == "030101001" and g["total_cnt"]:
            per = grams / float(g["total_cnt"])
            if per < t["min"] or per > t["max"]:
                bad.append(f"{g['good_name']} 개당 {per:.0f}g")
    status = "warn" if (bad or nograms) else "ok"
    msg = []
    if bad: msg.append("계란 개당 무게 이상: " + "; ".join(bad))
    if nograms: msg.append(f"무게로 못 바꾼 상품 {len(nograms)}개(" + ", ".join(nograms[:5]) + ")")
    return {"name": "단위 상식 검사", "status": status, "value": len(bad) + len(nograms), "threshold": f"계란 한 알 {t['min']}~{t['max']}g, 환산 실패 0개",
            "message": ". ".join(msg) if msg else "모든 식품의 무게를 바꿨고 계란 한 알 무게도 범위 안이다"}


def check_baseline():
    b = json.loads((CFG / "baseline.json").read_text(encoding="utf8"))
    ok = b.get("price_krw") and b.get("grams")
    return {"name": "기준선 입력", "status": "ok" if ok else "warn", "value": f"{b.get('price_krw'):,}원, {b.get('grams')}g" if ok else "미입력",
            "threshold": "가격과 용량 둘 다", "message": f"{b['name']}" + ("" if ok else ": config/baseline.json에 가격과 용량을 넣어야 비교가 나온다")}


def check_api_budget(th):
    n = api.CALLS["price"]
    return {"name": "API 호출", "status": "warn" if n > th["api_calls_warn"] else "ok", "value": n, "threshold": f"{th['api_calls_warn']}회 미만", "message": f"이번 실행에서 가격 API {n}회, 영양 API {api.CALLS['nutrient']}회를 불렀다(하루 한도는 각각 2,000회, 10,000회)"}


def run(*, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    th = _th()
    con = connect()
    days = survey_dates(con)
    day = days[-1] if days else None
    prev = days[-2] if len(days) > 1 else None
    checks = [check_survey_gap(con, th, today), check_rows(con, th), check_tieout(con), check_duplicates(con)]
    if day:
        checks += [check_coverage(con, th, day), check_match_rate(con, th), check_price_jump(con, th, day, prev), check_units(con, th)]
    checks += [check_baseline(), check_api_budget(th)]
    con.close()

    worst = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    result = {"run_at": dt.datetime.now().isoformat(timespec="seconds"), "survey_day": day, "previous_day": prev, "overall": worst, "checks": checks}
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 실행일이 아니라 기준으로 삼은 조사일로 이름을 짓는다.
    # 매일 돌아도 같은 조사분이면 같은 파일에 덮어쓰므로, 격주 조사에 맞춰 한 해 26장만 쌓인다.
    # 실행일로 지으면 새 데이터가 없는 날에도 파일이 하나씩 생겨 1년이면 365장이 된다.
    day = result.get("survey_day")
    stem = f"{day[:4]}-{day[4:6]}-{day[6:]}" if day else today.isoformat()
    (OUT_DIR / f"{stem}_qa.md").write_text(as_markdown(result), encoding="utf8")
    return result


def as_markdown(result: dict) -> str:
    icon = {"ok": "정상", "warn": "주의", "fail": "실패"}
    # "정상" 은 받침이 있어 "이다", "주의"·"실패" 는 받침이 없어 "다" 가 붙는다.
    verdict = {"ok": "정상이다", "warn": "주의다", "fail": "실패다"}[result["overall"]]
    lines = [f"종합 판정은 {verdict}. {_iso(result['survey_day']) if result['survey_day'] else '-'} 조사분을 기준으로 {result['run_at'].replace('T', ' ')[:16]}에 점검했다.", "",
             "| 항목 | 판정 | 값 | 기준 | 설명 |", "|---|---|---|---|---|"]
    for c in result["checks"]:
        lines.append(f"| {c['name']} | {icon[c['status']]} | {c['value']} | {c['threshold']} | {c['message']} |")
    return "\n".join(lines)
