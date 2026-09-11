"""주간 리포트. 새 조사분이 들어왔고 아직 리포트가 없을 때만 만든다.

만드는 것: reports/YYYY-Www.md (ISO 주차). 순위표, 관심 품목, 기준선 비교, 할인, 점검표, 한계.
GitHub Actions 에서는 report_path 와 summary_line 을 출력 변수로 넘긴다.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import os

from . import api, qa
from .load import connect
from .metrics import good_metrics, rankable, not_ranked_but_cheap, survey_dates

CFG = api.ROOT / "config"
REPORTS = api.ROOT / "reports"
LATEST = api.DATA / "report_latest.json"

WATCH = [  # 따로 지켜보는 여덟 품목 (워크북 A-3)
    ("계란", ["030101001"]), ("참치캔", ["030202004"]), ("두부", ["030201008"]), ("훈제오리", ["030203010"]),
    ("슬라이스 치즈", ["030203007"]), ("프로틴 드링크", ["030206024"]), ("쇠고기 불고기", ["030101002"]), ("돼지고기(삼겹살·목살)", ["030101004"]),
]


def _iso(d8): return f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
def _won(v): return f"{v:,.0f}원" if v is not None else "-"
def _josa(word: str, pair: str) -> str:
    """받침 유무로 조사를 고른다. pair 는 '은는', '이가' 처럼 받침 있을 때 쓰는 것을 앞에 둔다."""
    w = re.sub(r"(\([^)]*\))+$", "", word).strip()
    ch = w[-1] if w else ""
    has = "가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28 != 0
    return pair[0] if has else pair[1]


def _kdate(d8, year=True):
    d8 = d8.replace("-", "")
    y, m, d = int(d8[:4]), int(d8[4:6]), int(d8[6:])
    return f"{y}년 {m}월 {d}일" if year else f"{m}월 {d}일"


def _reported_days() -> set[str]:
    if not REPORTS.exists():
        return set()
    out = set()
    for p in REPORTS.glob("*.md"):
        head = p.read_text(encoding="utf8")[:400]
        for tok in head.split():
            if tok.startswith("조사일:"):
                out.add(tok.split(":")[1].replace("-", ""))
    return out


def build(day: str, prev: str | None) -> tuple[str, dict]:
    th = json.loads((CFG / "qa_thresholds.json").read_text(encoding="utf8"))
    base = json.loads((CFG / "baseline.json").read_text(encoding="utf8"))
    con = connect()
    rows = good_metrics(con, day)
    prev_rows = {r["good_id"]: r for r in good_metrics(con, prev)} if prev else {}
    con.close()
    ranked = rankable(rows, th["protein_min_per_100g"])
    qa_res = json.loads(qa.OUT_JSON.read_text(encoding="utf8")) if qa.OUT_JSON.exists() else None

    L = []
    L.append("# 단백질 가격 비교 리포트")
    # 이 주석 줄은 GitHub 화면에 보이지 않는다. 같은 조사분 리포트를 두 번 만들지 않으려고 _reported_days 가 읽는다.
    L.append(f"<!-- 조사일:{_iso(day)} -->")
    L.append("")
    today = dt.date.today()
    L.append(f"{_kdate(day)} 조사분" + (f"을 {_kdate(prev, year=False)} 조사와 비교했다." if prev else "이다.") +
             f" {today.month}월 {today.day}일에 만들었다.")
    L.append("")

    def _pct(c):
        # "+0%" 가 줄마다 붙으면 어수선하다. 반올림해 0 이면 그냥 0% 로 쓴다.
        if c is None:
            return "-"
        return "0%" if round(c) == 0 else f"{c:+.0f}%"

    def _dc(v):
        # 할인 비율이 반올림해 0% 면 할인이 없는 것과 같게 "-" 로 쓴다.
        return f"{v:.0%}" if v and round(v * 100) > 0 else "-"

    def _chg(r):
        pv = prev_rows.get(r["good_id"])
        return (r["won_per_g"] / pv["won_per_g"] - 1) * 100 if pv and pv.get("won_per_g") else None

    base_won = (base["price_krw"] / base["grams"] * 100 / base["protein_per_100g"]
                if base.get("price_krw") and base.get("grams") else None)
    checks = (qa_res or {}).get("checks", [])

    # 지켜보는 품목마다 가장 싼 상품. 요약과 표가 함께 쓴다.
    watch, watch_summary = [], []
    for label, codes in WATCH:
        cands = [r for r in ranked if r["smlcls_code"] in codes]
        best = cands[0] if cands else None
        watch.append((label, best))
        if best:
            watch_summary.append(f"{label} {best['won_per_g']:,.0f}원")

    # 요약. 읽는 사람이 궁금한 "이번 주에 뭘 사면 되나" 를 먼저 답한다.
    L.append("## 이번 주 요약")
    L.append("")
    if ranked:
        top = ranked[0]
        s = f"단백질이 가장 싼 상품은 {top['good_name']}이고 1g당 {top['won_per_g']:,.1f}원이다."
        if (top["dc_share"] or 0) >= 0.3:
            s += f" 판매점 {top['dc_share']:.0%}에서 할인 중이다."
        L.append(s)
        L.append("")
    if base_won:
        cheaper = [r for r in ranked if r["won_per_g"] < base_won]
        s = f"늘 사는 {base['name']}{_josa(base['name'], '은는')} {base_won:,.1f}원이고 이보다 싼 상품이 {len(cheaper)}개다."
        wl = [label for label, best in watch if best and best["won_per_g"] < base_won]
        if wl:
            s += f" 지켜보는 품목 가운데 {', '.join(wl)}{_josa(wl[-1], '이가')} 더 싸다."
        if base.get("price_checked_on"):
            s += f" 기준 가격은 {_kdate(base['price_checked_on'], year=False)}에 직접 확인했다."
        L.append(s)
    else:
        L.append(f"{base['name']} 가격이 아직 없어 비교하지 못했다. config/baseline.json을 채우면 여기에 나온다.")
    L.append("")
    moved = [(r, _chg(r)) for r in ranked[:20] if _chg(r) is not None and abs(_chg(r)) >= 10]
    if moved:
        r, c = max(moved, key=lambda x: abs(x[1]))
        L.append(f"지난 조사와 비교하면 {r['good_name']}의 변화가 가장 크다. 단백질 1g당 가격이 {abs(c):.0f}% {'내렸다' if c < 0 else '올랐다'}.")
        L.append("")
    if checks:
        flagged = [c for c in checks if c["status"] != "ok"]
        qa_link = f"docs/qa/{_iso(day)}_qa.md"
        if flagged:
            names = ", ".join(c["name"] for c in flagged)
            L.append(f"자동 점검 {len(checks)}가지 중 {len(checks) - len(flagged)}가지는 정상이고 {names}{_josa(flagged[-1]['name'], '은는')} 확인이 필요하다. 전체 점검표는 {qa_link}에 있다.")
        else:
            L.append(f"자동 점검 {len(checks)}가지를 모두 통과했다. 전체 점검표는 {qa_link}에 있다.")
        L.append("")

    # 순위: 판단에 필요한 칸만 남긴다
    L.append("## 순위")
    L.append("")
    L.append(f"식품 {len(rows)}개 중 순위에 오른 {len(ranked)}개에서 위의 10개다.")
    L.append("")
    L.append("| 순위 | 상품 | 단백질 1g당 | 지난 조사 대비 | 할인하는 판매점 |")
    L.append("|---|---|---|---|---|")
    for i, r in enumerate(ranked[:10], 1):
        c = _chg(r)
        dc = r["dc_share"] or 0
        L.append(f"| {i} | {r['good_name']} | {r['won_per_g']:,.1f}원 | {_pct(c)} | {_dc(dc)} |")
    L.append("")

    L.append("## 지켜보는 품목")
    L.append("")
    L.append("| 품목 | 가장 싼 상품 | 단백질 1g당 | 지난 조사 대비 |")
    L.append("|---|---|---|---|")
    for label, best in watch:
        if not best:
            L.append(f"| {label} | 순위에 오른 상품 없음 | - | - |")
            continue
        c = _chg(best)
        L.append(f"| {label} | {best['good_name']} | {best['won_per_g']:,.1f}원 | {_pct(c)} |")
    L.append("")

    # 참고: 매주 같은 가정과 긴 목록은 파일로 빼고 링크만 건다
    from .metrics import protein_categories
    pcats = protein_categories()
    unmatched_p = [r for r in rows if r["method"] == "unmatched" and r["smlcls_code"] in pcats]
    L.append("## 참고")
    L.append("")
    L.append(f"가격은 판매점 가격의 중앙값이고 단백질은 식약처 영양성분의 100g당 값이다. "
             f"영양 정보를 못 붙였거나 단백질이 100g당 {th['protein_min_per_100g']:.0f}g 미만이거나 뼈 무게가 섞인 식품은 순위에서 뺐다.")
    L.append("")
    if unmatched_p:
        few = ", ".join(re.sub(r"(\([^)]*\))+$", "", r["good_name"]).strip() for r in unmatched_p[:3])
        L.append(f"순위 대상인데 영양 정보를 못 붙여 빠진 상품이 {len(unmatched_p)}개다({few}{' 등' if len(unmatched_p) > 3 else ''}). "
                 "자동으로 이은 상품은 틀릴 수 있다. 빠진 상품 전체와 사람이 정한 가정은 docs/match_report.md와 README에 있다.")
    else:
        L.append("자동으로 이은 상품은 틀릴 수 있다. 사람이 정한 가정은 docs/match_report.md와 README에 있다.")
    L.append("")
    L.append("출처: 한국소비자원 참가격(공공데이터포털 15158701, 공공저작물 제1유형), 식품의약품안전처 식품영양성분DB(15127578).")

    top3 = ", ".join(f"{r['good_name']} {r['won_per_g']:,.0f}원" for r in ranked[:3])
    summary = {"survey_day": day, "top3": top3, "n_ranked": len(ranked), "n_food": len(rows), "watch": watch_summary,
               "overall_qa": (qa_res or {}).get("overall"),
               "baseline_name": base["name"],
               "baseline_won": (round(base["price_krw"] / base["grams"] * 100 / base["protein_per_100g"], 1)
                                if base.get("price_krw") and base.get("grams") else None),
               "qa_warn": [c["name"] for c in (qa_res or {}).get("checks", []) if c["status"] == "warn"],
               "qa_fail": [c["name"] for c in (qa_res or {}).get("checks", []) if c["status"] == "fail"]}
    return "\n".join(L), summary


def run(*, force: bool = False, monday_only: bool = True, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    con = connect(); days = survey_dates(con); con.close()
    out = {"report_path": "", "summary_line": "", "made": False}
    if not days:
        out["summary_line"] = "적재된 조사분이 없다"
        _emit(out); return out
    day, prev = days[-1], (days[-2] if len(days) > 1 else None)
    if day in _reported_days() and not force:
        out["summary_line"] = f"{_iso(day)} 조사분 리포트는 이미 있다. 이번 주 새 조사 없음"
        _emit(out); return out
    if monday_only and today.weekday() != 0 and not force:
        out["summary_line"] = f"{_iso(day)} 조사분이 들어왔다. 리포트는 월요일에 만든다"
        _emit(out); return out
    text, summary = build(day, prev)
    if not summary.get("n_ranked"):
        # 매칭이 하나도 없으면 순위표가 빈 리포트가 만들어진다.
        # 그걸 써 버리면 멀쩡하던 지난 리포트를 덮어쓴다. 쓰지 않고 세운다.
        out["summary_line"] = f"{_iso(day)} 조사분에 순위에 올릴 상품이 없다. match 단계를 먼저 돌려야 한다. 기존 리포트는 건드리지 않는다"
        _emit(out); return out
    REPORTS.mkdir(parents=True, exist_ok=True)
    # 실행한 날이 아니라 조사한 날의 주차로 이름을 짓는다.
    # 같은 조사분을 다시 만들어도 같은 파일에 덮어쓰도록(멱등) 하려는 것이다.
    y, w, _ = dt.date(int(day[:4]), int(day[4:6]), int(day[6:])).isocalendar()
    path = REPORTS / f"{y}-W{w:02d}.md"
    path.write_text(text, encoding="utf8")
    LATEST.write_text(json.dumps({**summary, "path": str(path.relative_to(api.ROOT))}, ensure_ascii=False, indent=1), encoding="utf8")
    out.update({"report_path": str(path.relative_to(api.ROOT)), "made": True,
                "summary_line": f"{_iso(day)} 조사분. 1위 {summary['top3']}. 순위 {summary['n_ranked']}개"})
    _emit(out); return out


def _emit(out: dict) -> None:
    print(out["summary_line"])
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf8") as fh:
            fh.write(f"report_path={out['report_path']}\nsummary_line={out['summary_line']}\n")
