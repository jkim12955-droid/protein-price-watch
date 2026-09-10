"""적재: 원본 JSON 을 SQLite 에 넣는다. 같은 조사일을 두 번 넣어도 결과가 같다.

멱등성 원칙: 조사일 단위로 prices 를 지우고 다시 넣는다(A04 방식).
상품·판매점은 기본키로 upsert 하되 처음 본 날짜는 지키고 마지막 본 날짜만 갱신한다.
적재 후 원본 건수와 DB 행 수를 대조한다(V1). 다르면 예외를 던져 파이프라인을 세운다.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import sqlite3
from pathlib import Path

from . import api

DB = api.DATA / "pricewatch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS goods (
  good_id     INTEGER PRIMARY KEY,
  good_name   TEXT NOT NULL,
  maker_code  TEXT,
  unit_div    TEXT,          -- goodUnitDivCode : G / ML / EA …
  base_cnt    REAL,          -- goodBaseCnt     : 기준량 (100g 기준이면 100)
  total_cnt   REAL,          -- goodTotalCnt    : 총량
  total_div   TEXT,          -- goodTotalDivCode
  smlcls_code TEXT,          -- goodSmlclsCode  : 소분류 코드
  detail_mean TEXT,          -- detailMean      : "90g*4개" 같은 보조 설명
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stores (
  entp_id     INTEGER PRIMARY KEY,
  entp_name   TEXT NOT NULL,
  entp_type   TEXT,          -- LM 대형마트 / SM 슈퍼마켓 / DP 백화점 / CS 편의점
  area_code   TEXT,
  area_detail TEXT,
  road_addr   TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
  inspect_day TEXT    NOT NULL,   -- YYYYMMDD
  entp_id     INTEGER NOT NULL,
  good_id     INTEGER NOT NULL,
  price       INTEGER,
  dc_yn       TEXT,               -- 할인 여부 (있을 때만 옴)
  dc_start    TEXT,
  dc_end      TEXT,
  plusone_yn  TEXT,               -- 1+1 여부
  input_dttm  TEXT,
  PRIMARY KEY (inspect_day, entp_id, good_id)
);
CREATE INDEX IF NOT EXISTS ix_prices_good ON prices(good_id, inspect_day);
CREATE TABLE IF NOT EXISTS load_log (
  inspect_day  TEXT NOT NULL,
  loaded_at    TEXT NOT NULL,
  raw_count    INTEGER,
  loaded_count INTEGER,
  dup_in_raw   INTEGER,
  ok           INTEGER
);
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _upsert_goods(con, goods: list[dict], day: str) -> None:
    for g in goods:
        con.execute(
            """INSERT INTO goods (good_id, good_name, maker_code, unit_div, base_cnt, total_cnt,
                                  total_div, smlcls_code, detail_mean, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(good_id) DO UPDATE SET
                 good_name=excluded.good_name, maker_code=excluded.maker_code,
                 unit_div=excluded.unit_div, base_cnt=excluded.base_cnt,
                 total_cnt=excluded.total_cnt, total_div=excluded.total_div,
                 smlcls_code=excluded.smlcls_code, detail_mean=excluded.detail_mean,
                 last_seen=MAX(goods.last_seen, excluded.last_seen)""",
            (int(g["goodId"]), g.get("goodName", ""), g.get("productEntpCode"),
             g.get("goodUnitDivCode"), _num(g.get("goodBaseCnt")), _num(g.get("goodTotalCnt")),
             g.get("goodTotalDivCode"), g.get("goodSmlclsCode"), g.get("detailMean"), day, day),
        )


def _upsert_stores(con, stores: list[dict], day: str) -> None:
    for s in stores:
        con.execute(
            """INSERT INTO stores (entp_id, entp_name, entp_type, area_code, area_detail, road_addr,
                                   first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(entp_id) DO UPDATE SET
                 entp_name=excluded.entp_name, entp_type=excluded.entp_type,
                 area_code=excluded.area_code, area_detail=excluded.area_detail,
                 road_addr=excluded.road_addr,
                 last_seen=MAX(stores.last_seen, excluded.last_seen)""",
            (int(s["entpId"]), s.get("entpName", ""), s.get("entpTypeCode"), s.get("entpAreaCode"),
             s.get("areaDetailCode"), s.get("roadAddrBasic"), day, day),
        )


def load_date(d8: str) -> dict:
    """한 조사일 원본을 적재한다. 원본 건수와 DB 행 수를 대조해 돌려준다."""
    iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
    raw = api.RAW / iso
    goods = json.loads((raw / "goods.json").read_text(encoding="utf8"))
    stores = json.loads((raw / "stores.json").read_text(encoding="utf8"))
    gz = raw / "prices.json.gz"
    if gz.exists():
        with gzip.open(gz, "rt", encoding="utf8") as fh:
            prices = json.load(fh)
    else:
        prices = json.loads((raw / "prices.json").read_text(encoding="utf8"))

    # 원본 안에서의 중복(같은 날·판매점·상품이 두 번)은 마지막 값을 쓰되 개수를 기록한다.
    uniq: dict[tuple, dict] = {}
    for p in prices:
        uniq[(p["goodInspectDay"], int(p["entpId"]), int(p["goodId"]))] = p
    dup_in_raw = len(prices) - len(uniq)

    con = connect()
    with con:
        _upsert_goods(con, goods, d8)
        _upsert_stores(con, stores, d8)
        con.execute("DELETE FROM prices WHERE inspect_day = ?", (d8,))
        con.executemany(
            """INSERT INTO prices (inspect_day, entp_id, good_id, price, dc_yn, dc_start, dc_end,
                                   plusone_yn, input_dttm) VALUES (?,?,?,?,?,?,?,?,?)""",
            [(k[0], k[1], k[2], int(float(p.get("goodPrice") or 0)) or None,
              p.get("goodDcYn"), p.get("goodDcStartDay"), p.get("goodDcEndDay"),
              p.get("plusoneYn"), p.get("inputDttm")) for k, p in uniq.items()],
        )
        loaded = con.execute("SELECT COUNT(*) FROM prices WHERE inspect_day=?", (d8,)).fetchone()[0]
        ok = int(loaded == len(uniq))
        con.execute("INSERT INTO load_log VALUES (?,?,?,?,?,?)",
                    (d8, dt.datetime.now().isoformat(timespec="seconds"), len(prices), loaded, dup_in_raw, ok))
    con.close()
    result = {"inspect_day": d8, "raw_count": len(prices), "unique_in_raw": len(uniq),
              "dup_in_raw": dup_in_raw, "loaded_count": loaded, "ok": bool(ok)}
    if not ok:
        raise RuntimeError(f"적재 대조 실패: 원본 고유 {len(uniq)}건 ≠ DB {loaded}건 ({iso})")
    return result


def run(dates: list[str] | None = None) -> list[dict]:
    if dates is None:
        dates = sorted(p.name.replace("-", "") for p in api.RAW.iterdir()
                       if p.is_dir() and ((p / "prices.json.gz").exists() or (p / "prices.json").exists()))
    out = []
    for d8 in dates:
        r = load_date(d8)
        print(f"[{d8}] 원본 {r['raw_count']}건 (중복 {r['dup_in_raw']}) → DB {r['loaded_count']}행 :: {'대조 일치' if r['ok'] else '불일치'}")
        out.append(r)
    return out
