"""조사일 하나에 대해 상품별 지표를 만든다. 리포트와 점검이 같은 숫자를 쓰도록 여기 한 곳에 둔다."""
from __future__ import annotations

import json
import sqlite3
import statistics

from . import api
from .quantity import grams_per_pack

CFG = api.ROOT / "config"


def survey_dates(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT inspect_day FROM prices ORDER BY 1")]


def labels() -> dict[str, str]:
    cats = json.loads((CFG / "categories.json").read_text(encoding="utf8"))["rules"]
    return {k: v.get("label", "") for k, v in cats.items()}


def good_metrics(con: sqlite3.Connection, day: str) -> list[dict]:
    """조사일 day 의 식품 상품별 지표. 매칭이 안 됐거나 제외된 상품도 포함하되 won_per_g 는 None."""
    lab = labels()
    goods = {r[0]: dict(zip(["good_id", "good_name", "smlcls_code", "unit_div", "base_cnt", "total_cnt", "total_div", "detail_mean"], r))
             for r in con.execute("""SELECT good_id, good_name, smlcls_code, unit_div, base_cnt, total_cnt, total_div, detail_mean
                                     FROM goods WHERE smlcls_code LIKE '0301%' OR smlcls_code LIKE '0302%'""")}
    prices: dict[int, list[tuple[int, int, str | None]]] = {}
    for gid, entp, price, dc in con.execute("SELECT good_id, entp_id, price, dc_yn FROM prices WHERE inspect_day=? AND price IS NOT NULL AND price > 0", (day,)):
        prices.setdefault(gid, []).append((price, entp, dc))
    stores = {r[0]: r[1] for r in con.execute("SELECT entp_id, entp_name FROM stores")}
    matches = {r[0]: dict(zip(["food_cd", "method", "score", "note"], r[1:]))
               for r in con.execute("SELECT good_id, food_cd, method, score, note FROM matches")}
    nutrients = {r[0]: dict(zip(["food_name", "protein", "kcal", "maker"], r[1:]))
                 for r in con.execute("SELECT food_cd, food_name, protein, kcal, maker FROM nutrients")}

    out = []
    for gid, g in goods.items():
        ps = prices.get(gid, [])
        m = matches.get(gid, {"food_cd": None, "method": "unmatched", "score": None, "note": None})
        n = nutrients.get(m["food_cd"]) if m["food_cd"] else None
        grams, basis = grams_per_pack(g)
        row = {**g, "label": lab.get(g["smlcls_code"], ""), "n_stores": len(ps),
               "median_price": None, "min_price": None, "min_store": None, "dc_share": None,
               "grams": grams, "grams_basis": basis, "price_per_100g": None,
               "food_cd": m["food_cd"], "food_name": (n or {}).get("food_name"), "protein": (n or {}).get("protein"),
               "kcal": (n or {}).get("kcal"), "method": m["method"], "match_note": m["note"], "won_per_g": None}
        if ps:
            vals = sorted(p for p, _, _ in ps)
            row["median_price"] = statistics.median(vals)
            mn = min(ps, key=lambda x: x[0])
            row["min_price"], row["min_store"] = mn[0], stores.get(mn[1], str(mn[1]))
            row["dc_share"] = round(sum(1 for _, _, dc in ps if dc == "Y") / len(ps), 3)
            if grams:
                row["price_per_100g"] = row["median_price"] / grams * 100.0
                if row["protein"] and row["protein"] > 0 and m["method"] not in ("excluded_manual", "excluded_category", "unmatched"):
                    row["won_per_g"] = row["price_per_100g"] / row["protein"]
        out.append(row)
    return out


def rankable(rows: list[dict], protein_min: float) -> list[dict]:
    return sorted([r for r in rows if r["won_per_g"] is not None and (r["protein"] or 0) >= protein_min], key=lambda r: r["won_per_g"])
