"""수집: 새 조사일자를 찾고, 그 날의 가격 전체를 원본 그대로 저장한다.

- 참가격 조사는 격주 금요일에만 이뤄지는 것으로 관측됐다(2026-07~09).
  그래서 매일 돌리되 "새 조사일자가 생겼는가"만 확인하고, 없으면 아무 일도 하지 않는다.
- 원본은 data/raw/YYYY-MM-DD/ 아래 JSON 으로 남긴다. 가공하지 않는다.
- 같은 날짜를 두 번 수집해도 파일이 통째로 다시 쓰일 뿐 섞이지 않는다.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import sys
from pathlib import Path

from . import api

STATE = api.DATA / "state.json"
PROBE_WINDOW_DAYS = 21          # 마지막으로 아는 조사일 이후 이만큼만 훑는다
PROBE_GOOD_ID = 1000            # 판매점 400곳 이상이 취급하는 상품. 조사 유무 확인용


def _load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf8"))
    return {"known_dates": []}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf8")


def _iso(d8: str) -> str:
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"


def reachable() -> bool:
    """참가격 서버가 이 IP 에서 오는 연결을 받는지 한 번 물어본다.

    받지 않으면 그 실행에서는 무엇을 해도 안 되므로, 606회를 시도하기 전에 먼저 확인한다.
    """
    try:
        api.has_survey("20260828", PROBE_GOOD_ID)
        return True
    except api.Unreachable:
        return False


def discover_new_dates(today: dt.date | None = None) -> list[str]:
    """마지막으로 아는 조사일 다음 날부터 오늘까지 하루씩 물어 새 조사일을 찾는다."""
    today = today or dt.date.today()
    state = _load_state()
    known = sorted(state["known_dates"])
    if known:
        start = dt.datetime.strptime(known[-1], "%Y%m%d").date() + dt.timedelta(days=1)
    else:
        start = today - dt.timedelta(days=PROBE_WINDOW_DAYS)
    start = max(start, today - dt.timedelta(days=PROBE_WINDOW_DAYS))
    found: list[str] = []
    d = start
    while d <= today:
        d8 = d.strftime("%Y%m%d")
        if api.has_survey(d8, PROBE_GOOD_ID):
            found.append(d8)
        d += dt.timedelta(days=1)
    return found


def collect_date(d8: str, *, force: bool = False) -> dict:
    """한 조사일의 상품·판매점·가격 전체를 받아 저장한다. 요약을 돌려준다."""
    out = api.RAW / _iso(d8)
    prices_path = out / "prices.json.gz"
    plain = out / "prices.json"
    if plain.exists() and not prices_path.exists():
        # 압축을 넣기 전에 받아둔 조사일이 있으면 여기서 옮긴다.
        # API 를 606회 다시 부르는 대신 파일만 바꾼다.
        rows = json.loads(plain.read_text(encoding="utf8"))
        with gzip.open(prices_path, "wt", encoding="utf8") as fh:
            json.dump(rows, fh, ensure_ascii=False)
        plain.unlink()
        print(f"[{d8}] 비압축 원본을 압축본으로 옮겼다 ({len(rows):,}건)", file=sys.stderr)
    if prices_path.exists() and not force:
        meta = json.loads((out / "meta.json").read_text(encoding="utf8"))
        return {**meta, "skipped": True}

    out.mkdir(parents=True, exist_ok=True)
    calls_before = dict(api.CALLS)

    goods = api.fetch_goods()
    stores = api.fetch_stores()
    prices: list[dict] = []
    failed: list[str] = []
    for i, g in enumerate(goods, 1):
        gid = g["goodId"]
        try:
            prices.extend(api.fetch_prices_for_good(d8, gid))
        except Exception as e:  # 한 상품 실패로 전체를 버리지 않는다. 기록하고 계속.
            failed.append(api.mask(f"{gid}: {type(e).__name__}: {e}")[:200])
        if i % 100 == 0:
            print(f"  … {i}/{len(goods)} 상품, 가격 {len(prices)}건", file=sys.stderr)

    (out / "goods.json").write_text(json.dumps(goods, ensure_ascii=False), encoding="utf8")
    (out / "stores.json").write_text(json.dumps(stores, ensure_ascii=False), encoding="utf8")
    with gzip.open(prices_path, "wt", encoding="utf8") as fh:  # 15MB → 약 1MB. 저장소에 커밋하기 위해 압축
        json.dump(prices, fh, ensure_ascii=False)

    meta = {
        "inspect_day": d8,
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "n_goods": len(goods),
        "n_stores": len(stores),
        "n_prices": len(prices),
        "n_goods_failed": len(failed),
        "failed": failed[:50],
        "api_calls_price": api.CALLS["price"] - calls_before["price"],
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf8")

    state = _load_state()
    if d8 not in state["known_dates"]:
        state["known_dates"].append(d8)
        state["known_dates"].sort()
    state["last_run"] = meta["fetched_at"]
    _save_state(state)
    return {**meta, "skipped": False}


def run(*, dates: list[str] | None = None, force: bool = False) -> list[dict]:
    """CLI 진입점. 날짜를 주면 그 날짜만, 안 주면 새 조사일을 찾아 수집한다."""
    if dates is None:
        if not reachable():
            # 이 실행이 받은 IP 에서는 참가격이 응답하지 않는다. 고장이 아니라 확률이다.
            # 여기서 실패로 처리하면 멀쩡한 날에도 실패 알림이 울린다.
            # 대신 아무것도 안 하고 물러난다. 며칠씩 계속 못 받으면 조사 공백 점검이 잡아낸다.
            print("참가격이 이 실행의 IP 에서 응답하지 않는다. 수집을 건너뛴다. "
                  "(며칠 이어지면 점검의 조사 공백 항목이 잡는다)")
            return []
        dates = discover_new_dates()
        if not dates:
            state = _load_state()
            # 새 조사일이 없던 날에도 state.json 이 바뀌면 매일 봇 커밋이 하나씩 나간다.
            # 격주로 오는 진짜 기록이 그 잡음에 묻히므로, 실행 시각은 추적하지 않는 파일에 적는다.
            (api.DATA / "_last_run.txt").write_text(
                dt.datetime.now().isoformat(timespec="seconds"), encoding="utf8")
            _save_state(state)
            print("새 조사일자 없음. 수집할 것이 없다.")
            return []
        print(f"새 조사일자 발견: {', '.join(_iso(d) for d in dates)}")
    results = []
    for d8 in dates:
        print(f"[{_iso(d8)}] 수집 시작")
        r = collect_date(d8, force=force)
        tag = "이미 있음, 건너뜀" if r.get("skipped") else f"가격 {r['n_prices']}건 / 상품 {r['n_goods']} / 판매점 {r['n_stores']} / 호출 {r.get('api_calls_price', '-')}회"
        print(f"[{_iso(d8)}] {tag}")
        results.append(r)
    return results
