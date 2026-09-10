"""주간 리포트. 새 조사분이 들어왔고 아직 리포트가 없을 때만 만든다.

만드는 것: reports/YYYY-Www.md (ISO 주차). 순위표, 관심 품목, 기준선 비교, 할인, 점검표, 한계.
GitHub Actions 에서는 report_path 와 summary_line 을 출력 변수로 넘긴다.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from . import api, qa
from .load import connect
from .metrics import good_metrics, rankable, survey_dates

CFG = api.ROOT / "config"
REPORTS = api.ROOT / "reports"
LATEST = api.DATA / "report_latest.json"

WATCH = [  # 따로 지켜보는 여덟 품목 (워크북 A-3)
    ("계란", ["030101001"]), ("참치캔", ["030202004"]), ("두부", ["030201008"]), ("훈제오리", ["030203010"]),
    ("슬라이스 치즈", ["030203007"]), ("프로틴 드링크", ["030206024"]), ("쇠고기 불고기", ["030101002"]), ("돼지고기 삼겹살", ["030101004"]),
]


def _iso(d8): return f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
def _won(v): return f"{v:,.0f}원" if v is not None else "-"


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
    L.append(f"# 단백질 가격 비교 리포트")
    L.append(f"조사일:{_iso(day)} 작성:{dt.date.today().isoformat()}" + (f" 비교:{_iso(prev)}" if prev else ""))
    L.append("")
    L.append(f"한국소비자원 참가격 {_iso(day)} 조사분에 식약처 식품영양성분을 붙여, 단백질 1g을 얻는 데 드는 돈이 적은 순서로 줄을 세웠다. "
             f"가격은 판매점 {max((r['n_stores'] for r in rows), default=0)}곳까지의 중앙값이고, 단백질은 100g당 값이다. "
             f"식품 {len(rows)}개 중 {len(ranked)}개가 순위에 올랐다. 나머지는 영양 항목을 못 붙였거나 단백질이 100g당 {th['protein_min_per_100g']:.0f}g 미만이거나 뼈 포함 무게라 뺐다.")
    L.append("")

    # 1. 순위
    L.append("## 1. 단백질 1g당 가격 순위")
    L.append("")
    L.append("| 순위 | 상품 | 분류 | 100g당 가격 | 단백질 g/100g | 단백질 1g당 | 판매점 | 할인 중 | 매칭 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    method_ko = {"manual": "사람", "exact": "자동·정확", "partial": "자동·부분", "fallback": "분류 기본"}
    for i, r in enumerate(ranked[:20], 1):
        chg = ""
        p = prev_rows.get(r["good_id"])
        if p and p.get("won_per_g"):
            d = (r["won_per_g"] / p["won_per_g"] - 1) * 100
            chg = f" ({d:+.0f}%)"
        L.append(f"| {i} | {r['good_name']} | {r['label']} | {_won(r['price_per_100g'])} | {r['protein']:.1f} | **{r['won_per_g']:,.1f}원**{chg} | {r['n_stores']} | {r['dc_share']:.0%} | {method_ko.get(r['method'], r['method'])} |")
    L.append("")
    L.append("괄호 안은 이전 조사 대비 단백질 1g당 가격 변화다. 판매점 수가 적은 상품은 중앙값이 흔들릴 수 있다.")
    L.append("")

    # 2. 기준선
    L.append("## 2. 내가 늘 사는 것과 비교")
    L.append("")
    if base.get("price_krw") and base.get("grams"):
        b100 = base["price_krw"] / base["grams"] * 100
        bwon = b100 / base["protein_per_100g"]
        cheaper = [r for r in ranked if r["won_per_g"] < bwon]
        L.append(f"{base['name']}은 100g에 {_won(b100)}, 단백질 100g당 {base['protein_per_100g']}g 이라 단백질 1g당 {bwon:,.1f}원이다. "
                 f"이보다 싼 상품이 {len(cheaper)}개다.")
        if cheaper:
            L.append("")
            for r in cheaper[:8]:
                L.append(f"- {r['good_name']}: {r['won_per_g']:,.1f}원 ({100 - r['won_per_g']/bwon*100:.0f}% 저렴)")
    else:
        L.append(f"{base['name']} 가격과 용량이 아직 입력되지 않았다. config/baseline.json 을 채우면 이 절에 비교가 나온다.")
    L.append("")

    # 3. 관심 품목
    L.append("## 3. 따로 지켜보는 여덟 품목")
    L.append("")
    L.append("| 품목 | 이번 조사 최저(단백질 1g당) | 상품 | 이전 조사 | 변화 |")
    L.append("|---|---|---|---|---|")
    watch_summary = []
    for label, codes in WATCH:
        cands = [r for r in ranked if r["smlcls_code"] in codes]
        if not cands:
            L.append(f"| {label} | - | (순위에 오른 상품 없음) | - | - |")
            continue
        best = cands[0]
        pv = prev_rows.get(best["good_id"], {}).get("won_per_g")
        chg = f"{(best['won_per_g']/pv-1)*100:+.0f}%" if pv else "-"
        L.append(f"| {label} | {best['won_per_g']:,.1f}원 | {best['good_name']} | {f'{pv:,.1f}원' if pv else '-'} | {chg} |")
        watch_summary.append(f"{label} {best['won_per_g']:,.0f}원")
    L.append("")

    # 4. 할인
    disc = [r for r in ranked if (r["dc_share"] or 0) >= 0.3][:8]
    L.append("## 4. 지금 할인 중인 것")
    L.append("")
    if disc:
        for r in disc:
            L.append(f"- {r['good_name']}: 판매점 {r['dc_share']:.0%}에서 할인, 단백질 1g당 {r['won_per_g']:,.1f}원 (최저 {_won(r['min_price'])} {r['min_store']})")
    else:
        L.append("순위에 오른 상품 중 판매점 30% 이상에서 할인 중인 것은 없다.")
    L.append("")

    # 5. 점검
    L.append("## 5. 자동 점검 결과")
    L.append("")
    L.append(qa.as_markdown(qa_res) if qa_res else "점검 결과 파일이 없다 (python pw.py qa).")
    L.append("")

    # 6. 한계
    unmatched = [r for r in rows if r["method"] == "unmatched"]
    excluded = [r for r in rows if r["method"].startswith("excluded")]
    approx = [r for r in rows if r["method"] == "manual" and r.get("match_note") and "근사" in r["match_note"]]
    L.append("## 6. 못 하는 것과 조심할 것")
    L.append("")
    L.append(f"영양 항목을 못 붙인 상품이 {len(unmatched)}개다. 상품명이 브랜드식이라 식약처 DB 이름과 안 맞는 것들이다. 이 상품들은 순위에 없다.")
    L.append(f"일부러 뺀 상품이 {len(excluded)}개다. 조미료와 과자와 음료처럼 단백질로 먹지 않는 분류, 뼈 포함 무게인 통닭, 영양 DB 에 원재료가 없는 오징어와 연어와 조기가 여기 든다.")
    if approx:
        L.append("근사값으로 이은 상품: " + ", ".join(f"{r['good_name']}({r['match_note'].split('.')[0].split('→')[-1].strip()})" for r in approx[:6]) + ".")
    L.append("계란은 설명이 빈 상품에 개당 52g 을 썼다. 쇠고기 불고기는 부위가 특정되지 않아 앞다리 값을 썼다. 자동·부분 매칭은 단어가 60% 이상 겹치는 항목이라 틀릴 수 있다. 의심스러우면 docs/match_report.md 의 검토용 표를 본다.")
    L.append(f"하림 훈제 닭가슴살은 참가격에 없어 가격을 내가 직접 넣는다. 가공식품의 영양은 제조사 표시값이고 원재료는 식품성분표 값이라 기준이 조금 다르다.")
    L.append("")
    L.append("출처: 한국소비자원 참가격(공공데이터포털 15158701, 공공저작물 제1유형), 식품의약품안전처 식품영양성분DB(15127578). 코드와 원본은 저장소에 있다.")

    top3 = ", ".join(f"{r['good_name']} {r['won_per_g']:,.0f}원" for r in ranked[:3])
    summary = {"survey_day": day, "top3": top3, "n_ranked": len(ranked), "n_food": len(rows), "watch": watch_summary,
               "overall_qa": (qa_res or {}).get("overall")}
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
    REPORTS.mkdir(parents=True, exist_ok=True)
    y, w, _ = today.isocalendar()
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
