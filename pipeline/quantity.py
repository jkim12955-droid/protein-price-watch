"""상품 한 묶음이 몇 그램(또는 mL)인지 구한다. 100g당 가격의 분모다.

참가격 상품 정보는 세 가지 형태로 온다.
  1) 단위가 G 나 ML 이고 총량이 숫자로 온다            → 그대로 쓴다
  2) 개수 단위(EA 등)지만 detailMean 에 무게 설명이 있다 → 설명을 읽는다
  3) 마리·단·망 단위에 설명이 없다                     → 상품명의 "(300~500g)" 범위로 중간값을 쓴다
셋 다 안 되면 None 을 돌려주고, 그 상품은 순위에서 빠진다. 빠진 개수는 품질 점검이 센다.
"""
from __future__ import annotations

import re

_NUM = r"(\d+(?:\.\d+)?)"

# 설명이 비어 있는 개수 단위 상품에 쓰는 개당 무게. 같은 판매처의 다른 상품 설명에서 가져온 값이다.
# 계란: CJ 1등급 깨끗한 계란(10개)=520g, 청정원 유정란(15개)=780g → 개당 52g (대란 기준).
KNOWN_UNIT_GRAMS = {
    "계란": 52.0,
    "달걀": 52.0,
    "유정란": 52.0,
    "목초란": 52.0,
}


def _kg_to_g(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in ("kg", "l"):
        return value * 1000.0
    return value


def parse_detail(detail: str | None) -> float | None:
    """detailMean 문자열에서 총 그램을 뽑는다. 못 읽으면 None."""
    if not detail:
        return None
    d = detail.replace(" ", "")

    # "120g*5개입", "1.5g*100개입", "10.9g*220개입"
    m = re.search(rf"{_NUM}(g|kg|ml|l)\*{_NUM}개", d, re.I)
    if m:
        return _kg_to_g(float(m.group(1)), m.group(2)) * float(m.group(3))

    # "780g(52g*15개)" 처럼 총량이 앞에 오는 경우: 총량을 우선
    m = re.match(rf"^{_NUM}(g|kg|ml|l)", d, re.I)
    if m:
        return _kg_to_g(float(m.group(1)), m.group(2))

    # "대란(52g~59g)", "대란(52g ~ 60g)" : 개당 범위. 개수는 호출부에서 곱한다 → per_unit 로 표시
    m = re.search(rf"{_NUM}g~{_NUM}g", d, re.I)
    if m:
        return -((float(m.group(1)) + float(m.group(2))) / 2.0)  # 음수 = 개당 무게 신호

    # "배추 1개당 1500g~2000g", "1망당 1500g"
    m = re.search(rf"당{_NUM}(g|kg)~{_NUM}(g|kg)", d, re.I)
    if m:
        return (_kg_to_g(float(m.group(1)), m.group(2)) + _kg_to_g(float(m.group(3)), m.group(4))) / 2.0
    m = re.search(rf"당{_NUM}(g|kg)", d, re.I)
    if m:
        return _kg_to_g(float(m.group(1)), m.group(2))
    return None


def parse_name_range(name: str) -> float | None:
    """상품명 괄호 안의 "(300~500g)", "(1.5~2kg)", "(250g)" 를 읽는다."""
    m = re.search(rf"{_NUM}\s*~\s*{_NUM}\s*(g|kg|ml|l)\b", name, re.I)
    if m:
        u = m.group(3)
        return (_kg_to_g(float(m.group(1)), u) + _kg_to_g(float(m.group(2)), u)) / 2.0
    m = re.search(rf"{_NUM}\s*(g|kg|ml|l)\b", name, re.I)
    if m:
        return _kg_to_g(float(m.group(1)), m.group(2))
    return None


def grams_per_pack(good: dict) -> tuple[float | None, str]:
    """(총 그램, 근거) 를 돌려준다. good 은 goods 테이블 한 행(dict)이다.

    근거 값: total_cnt / detail_pack / detail_per_unit / name_range / none
    """
    unit = (good.get("unit_div") or good.get("goodUnitDivCode") or "").upper()
    total = good.get("total_cnt") if "total_cnt" in good else good.get("goodTotalCnt")
    total_div = (good.get("total_div") or good.get("goodTotalDivCode") or "").upper()
    detail = good.get("detail_mean") or good.get("detailMean")
    name = good.get("good_name") or good.get("goodName") or ""

    try:
        total_f = float(total) if total not in (None, "") else None
    except (TypeError, ValueError):
        total_f = None

    # 1) 무게·부피 단위 그대로
    if total_div in ("G", "ML") and total_f:
        return total_f, "total_cnt"
    if unit in ("G", "ML") and total_f and total_div in ("", None):
        return total_f, "total_cnt"

    # 2) 설명에서
    parsed = parse_detail(detail)
    if parsed is not None:
        if parsed < 0:  # 개당 무게 × 개수
            if total_f:
                return -parsed * total_f, "detail_per_unit"
            return None, "none"
        return parsed, "detail_pack"

    # 3) 알려진 개당 무게 (설명이 비어 있을 때만)
    if total_f and unit in ("EA", "PK"):
        for key, per in KNOWN_UNIT_GRAMS.items():
            if key in name:
                return per * total_f, "known_unit"

    # 4) 이름의 범위
    rng = parse_name_range(name)
    if rng is not None:
        # "(100g)" 같은 단일값이 개수 단위 상품에 붙어 있으면 개당으로 본다
        if total_f and total_f > 1 and unit in ("EA", "PK", "MR", "DA", "MA"):
            return rng * total_f, "name_range_per_unit"
        return rng, "name_range"
    return None, "none"


def per_100g_price(price: int | float, grams: float) -> float:
    return float(price) / float(grams) * 100.0
