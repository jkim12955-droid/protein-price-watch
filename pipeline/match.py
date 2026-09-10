"""매칭: 참가격 상품에 식약처 영양 항목을 이어 붙인다. 이 프로젝트에서 가장 손이 많이 가는 단계다.

우선순위
  1. config/manual_match.json 에 사람이 정한 짝이 있으면 그것을 쓴다(신선식품, 예외 처리).
  2. 자동: 상품명에서 용량 괄호를 떼고, 브랜드로 보이는 첫 단어를 뗀 변형들로 영양 DB 를 검색해
     가공식품(P) 그룹에서 이름이 같은 항목을 찾는다. 없으면 단어가 60% 이상 겹치고 분류 규칙에 맞는 항목을 고른다.
  3. 소분류에 기본 항목(fallback)이 정해져 있으면 그것을 쓴다(두부).
  4. 그래도 없으면 '미매칭'으로 남긴다. 미매칭 비율은 품질 지표다.

검색 결과는 data/nutrient_search_cache.json 에 저장해 같은 이름을 두 번 부르지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3

from . import api
from .load import connect

CFG = api.ROOT / "config"
CACHE = api.DATA / "nutrient_search_cache.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS nutrients (
  food_cd   TEXT PRIMARY KEY,
  food_name TEXT, db_grp TEXT, db_class TEXT, cat1 TEXT, cat2 TEXT, ref_nm TEXT,
  kcal REAL, protein REAL, fat REAL, carb REAL,
  serving TEXT, weight TEXT, maker TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS matches (
  good_id    INTEGER PRIMARY KEY,
  food_cd    TEXT,            -- NULL 이면 미매칭 또는 제외
  method     TEXT NOT NULL,   -- manual / exact / partial / fallback / excluded_manual / excluded_category / unmatched
  score      REAL,
  note       TEXT,
  matched_at TEXT NOT NULL
);
"""

STOP = {"오리지널", "오리지날", "클래식", "순한맛", "매운맛", "마일드", "용기", "실속", "기획", "복합기획", "증정", "오리지널맛"}


def _load_json(path):
    return json.loads(path.read_text(encoding="utf8"))


def norm(s: str | None) -> str:
    return re.sub(r"[\s\-·,./()\[\]&+]+", "", s or "").lower()


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", name)).strip()


def tokens(name: str) -> list[str]:
    return [t for t in clean_name(name).split() if t not in STOP and len(t) >= 2]


def variants(name: str) -> list[str]:
    base = clean_name(name)
    toks = base.split()
    out = [base]
    if len(toks) > 1:
        out += [" ".join(toks[1:]), " ".join(toks[:2]), " ".join(toks[-2:]), toks[-1]]
    seen, res = set(), []
    for v in out:
        v = v.strip()
        if len(v) >= 2 and v not in seen:
            seen.add(v); res.append(v)
    return res


class Searcher:
    """이름 검색을 캐시한다. 같은 실행 안에서도, 다음 실행에서도 재사용한다."""

    def __init__(self):
        self.cache = _load_json(CACHE) if CACHE.exists() else {}
        self.dirty = False
        self.pending = 0

    def search(self, name: str) -> list[dict]:
        if name in self.cache:
            return self.cache[name]
        _, items = api.nutrient_search(name, rows=100)
        keep = ["FOOD_CD", "FOOD_NM_KR", "DB_GRP_CM", "DB_CLASS_NM", "FOOD_CAT1_NM", "FOOD_CAT2_NM", "FOOD_REF_NM",
                "AMT_NUM1", "AMT_NUM3", "AMT_NUM4", "AMT_NUM6", "NUTRI_AMOUNT_SERVING", "Z10500", "MAKER_NM"]
        slim = [{k: i.get(k) for k in keep} for i in items]
        self.cache[name] = slim
        self.dirty = True
        self.pending += 1
        if self.pending >= 50:      # 중간에 죽어도 호출한 만큼은 남긴다
            self.save()
        return slim

    def save(self):
        if self.dirty:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf8")
            self.pending = 0


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def upsert_nutrient(con: sqlite3.Connection, rec: dict) -> None:
    con.execute(
        """INSERT OR REPLACE INTO nutrients
           (food_cd, food_name, db_grp, db_class, cat1, cat2, ref_nm, kcal, protein, fat, carb, serving, weight, maker, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec["FOOD_CD"], rec.get("FOOD_NM_KR"), rec.get("DB_GRP_CM"), rec.get("DB_CLASS_NM"), rec.get("FOOD_CAT1_NM"),
         rec.get("FOOD_CAT2_NM"), rec.get("FOOD_REF_NM"), _num(rec.get("AMT_NUM1")), _num(rec.get("AMT_NUM3")),
         _num(rec.get("AMT_NUM4")), _num(rec.get("AMT_NUM6")), rec.get("NUTRI_AMOUNT_SERVING"), rec.get("Z10500"),
         rec.get("MAKER_NM"), dt.datetime.now().isoformat(timespec="seconds")),
    )


def category_ok(cand: dict, rule: dict | None, good_name: str, generic_forbid: list[str]) -> bool:
    hay = norm((cand.get("FOOD_NM_KR") or "") + (cand.get("FOOD_CAT1_NM") or "") + (cand.get("FOOD_CAT2_NM") or "") + (cand.get("FOOD_REF_NM") or ""))
    if rule and rule.get("require"):
        if not any(norm(k) in hay for k in rule["require"]):
            return False
    cname = norm(cand.get("FOOD_NM_KR"))
    gname = norm(good_name)
    for w in generic_forbid:
        if norm(w) in cname and norm(w) not in gname:
            return False
    if rule and rule.get("protein_range"):
        lo, hi = rule["protein_range"]
        p = _num(cand.get("AMT_NUM3"))
        if p is None or p < lo or p > hi:
            return False
    return True


def auto_match(good: dict, rule: dict | None, generic_forbid: list[str], searcher: Searcher) -> tuple[dict | None, str, float]:
    """(영양 항목, 방법, 점수). 못 찾으면 (None, 'unmatched', 0)."""
    name = good["good_name"]
    full = norm(clean_name(name))
    toks = tokens(name)
    core = toks[1:] if len(toks) > 1 else toks     # 첫 단어(브랜드로 추정)를 뺀 핵심 단어들
    best, best_score = None, 0.0
    for v in variants(name):
        items = [i for i in searcher.search(v) if i.get("DB_GRP_CM") == "P"]
        for i in items:
            if norm(i["FOOD_NM_KR"]) == full:
                return i, "exact", 1.0
        for i in items:
            if not category_ok(i, rule, name, generic_forbid):
                continue
            n = norm(i["FOOD_NM_KR"])
            hit = sum(1 for t in core if norm(t) in n)
            sc = hit / len(core) if core else 0.0
            # 브랜드 단어까지 맞으면 가산
            if toks and norm(toks[0]) in n:
                sc = min(1.0, sc + 0.15)
            if sc > best_score or (sc == best_score and best and len(i["FOOD_NM_KR"]) < len(best["FOOD_NM_KR"])):
                best, best_score = i, sc
        if best_score >= 0.99:
            break
    # 부분 매칭은 분류 규칙(require)이 있는 소분류에서만, 단어가 75% 이상 겹칠 때만 받는다.
    # 규칙이 없는 분류는 이름이 정확히 같을 때만 잇는다. 사이다가 단백질 음료로 이어지는 일을 막기 위해서다.
    if best and best_score >= 0.75 and rule and rule.get("require"):
        return best, "partial", round(best_score, 2)
    return None, "unmatched", 0.0


def run(*, refresh: bool = False) -> dict:
    manual = {int(i["good_id"]): i for i in _load_json(CFG / "manual_match.json")["items"]}
    cats = _load_json(CFG / "categories.json")
    rules, generic_forbid = cats["rules"], cats.get("_generic_forbid", [])
    r1ref = {r["FOOD_CD"]: r for r in _load_json(CFG / "nutrient_r1_reference.json")}

    con = connect()
    con.executescript(SCHEMA)
    goods = [dict(zip(["good_id", "good_name", "smlcls_code"], r)) for r in
             con.execute("SELECT good_id, good_name, smlcls_code FROM goods WHERE smlcls_code LIKE '0301%' OR smlcls_code LIKE '0302%' ORDER BY smlcls_code, good_name")]
    done = {} if refresh else {r[0]: r[1] for r in con.execute("SELECT good_id, method FROM matches")}

    searcher = Searcher()
    now = dt.datetime.now().isoformat(timespec="seconds")
    stats = {"manual": 0, "exact": 0, "partial": 0, "fallback": 0, "excluded_manual": 0, "excluded_category": 0, "unmatched": 0, "skipped_cached": 0}
    unmatched: list[tuple] = []
    for idx, g in enumerate(goods, 1):
        gid, name, code = g["good_id"], g["good_name"], g["smlcls_code"] or ""
        if gid in done:
            stats["skipped_cached"] += 1
            continue
        rule = rules.get(code)
        food_cd, method, score, note = None, "unmatched", 0.0, None

        if gid in manual:
            m = manual[gid]
            if m.get("exclude"):
                method, note = "excluded_manual", m.get("note")
            else:
                rec = r1ref.get(m["food_cd"])
                if rec is None:
                    raise RuntimeError(f"manual_match 의 food_cd {m['food_cd']} 가 참고표에 없다 (good {gid} {name})")
                upsert_nutrient(con, rec)
                food_cd, method, score, note = m["food_cd"], "manual", 1.0, m.get("note")
        elif rule and rule.get("protein") is False:
            method, note = "excluded_category", f"{rule.get('label')}: 단백질 순위 대상이 아님"
        else:
            rec, method, score = auto_match(g, rule, generic_forbid, searcher)
            if rec is not None:
                upsert_nutrient(con, rec)
                food_cd = rec["FOOD_CD"]
            elif rule and rule.get("fallback_food_cd"):
                fb = rule["fallback_food_cd"]
                fbrec = r1ref.get(fb)
                if fbrec is None:
                    # 참고표에 없으면 이름으로 한 번 찾아본다(두부 등 P 그룹 품목대표)
                    hits = [i for i in searcher.search(rule.get("label", "")) if i.get("FOOD_CD") == fb]
                    fbrec = hits[0] if hits else None
                if fbrec:
                    upsert_nutrient(con, fbrec)
                    food_cd, method, score, note = fb, "fallback", 0.5, f"{rule.get('label')} 소분류 기본 항목"
        if method == "unmatched":
            unmatched.append((gid, code, name))
        stats[method] += 1
        con.execute("INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?)", (gid, food_cd, method, score, note, now))
        con.commit()                      # 상품 하나마다 남긴다. 중단되면 다음 실행이 이어서 한다
        if idx % 50 == 0:
            print(f"  … {idx}/{len(goods)} 상품, 영양 API {api.CALLS['nutrient']}회", flush=True)
    searcher.save()
    if api.NUTRIENT_FAILURES:
        print(f"경고: 재시도 후에도 실패한 검색어 {len(api.NUTRIENT_FAILURES)}개: {api.NUTRIENT_FAILURES[:10]}")

    # 요약표. 매칭률의 분모는 '단백질 순위 대상 소분류'의 상품 중 사람이 제외하지 않은 것이다.
    protein_codes = {k for k, v in rules.items() if v.get("protein") is True}
    rows = con.execute("""
        SELECT g.smlcls_code, COUNT(*) total,
               SUM(CASE WHEN m.food_cd IS NOT NULL THEN 1 ELSE 0 END) matched,
               SUM(CASE WHEN m.method='unmatched' THEN 1 ELSE 0 END) unmatched,
               SUM(CASE WHEN m.method LIKE 'excluded%' THEN 1 ELSE 0 END) excluded
        FROM goods g JOIN matches m USING(good_id)
        WHERE g.smlcls_code LIKE '0301%' OR g.smlcls_code LIKE '0302%'
        GROUP BY 1 ORDER BY 1""").fetchall()
    total = sum(r[1] for r in rows); matched_all = sum(r[2] for r in rows); unm_all = sum(r[3] for r in rows)
    prow = [r for r in rows if r[0] in protein_codes]
    p_total = sum(r[1] for r in prow); p_matched = sum(r[2] for r in prow); p_exc = sum(r[4] for r in prow); p_unm = sum(r[3] for r in prow)
    eligible = p_total - p_exc
    summary = {"total_food": total, "matched_all": matched_all, "unmatched_all": unm_all,
               "protein_target_goods": p_total, "protein_excluded_manual": p_exc, "eligible": eligible,
               "matched": p_matched, "unmatched": p_unm,
               "match_rate_of_eligible": round(p_matched / eligible, 4) if eligible else None,
               "stats": stats, "api_calls_nutrient": api.CALLS["nutrient"]}
    matched, unm, exc = p_matched, p_unm, p_exc

    # 문서
    lines = ["# 매칭 결과", "", f"실행 {now}. 식품 {total}개 전체에 매칭을 시도해 {matched_all}개에 영양 항목이 붙었다.",
             f"단백질 순위 대상 소분류의 상품은 {p_total}개이고 그중 사람이 뺀 {exc}개를 제외한 {eligible}개 기준으로 {matched}개가 붙어 매칭률 {matched/eligible*100:.1f}%, 미매칭 {unm}개." if eligible else "",
             "순위 대상이 아닌 분류(밀가루·과자·음료·조미료 등)는 계산은 하되 매칭률에 넣지 않는다.",
             "", "방법별: " + ", ".join(f"{k} {v}" for k, v in stats.items() if v), "",
             "| 소분류 | 상품 | 매칭 | 미매칭 | 제외 |", "|---|---|---|---|---|"]
    for code, t, mm, u, e in rows:
        tag = "순위대상" if code in protein_codes else ""
        lines.append(f"| {code} {rules.get(code, {}).get('label', '')} {tag} | {t} | {mm} | {u} | {e} |")
    lines += ["", "## 미매칭 상품", ""]
    for gid, code, name in con.execute("""SELECT g.good_id, g.smlcls_code, g.good_name FROM matches m JOIN goods g USING(good_id)
                                          WHERE m.method='unmatched' ORDER BY g.smlcls_code, g.good_name"""):
        lines.append(f"- {gid} {code} {name}")
    lines += ["", "## 자동 매칭 결과 (검토용)", "", "| 상품 | 방법 | 점수 | 영양 항목 | 제조사 | 단백질 g/100g |", "|---|---|---|---|---|---|"]
    for r in con.execute("""SELECT g.good_name, m.method, m.score, n.food_name, n.maker, n.protein
                            FROM matches m JOIN goods g USING(good_id) LEFT JOIN nutrients n USING(food_cd)
                            WHERE m.method IN ('exact','partial','fallback','manual') ORDER BY m.method, g.good_name"""):
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3] or ''} | {r[4] or ''} | {r[5] if r[5] is not None else ''} |")
    (api.ROOT / "docs" / "match_report.md").write_text("\n".join(lines), encoding="utf8")
    con.close()
    return summary
