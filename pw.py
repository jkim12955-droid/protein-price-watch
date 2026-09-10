#!/usr/bin/env python3
"""단백질 가격 비교기 CLI.

  python pw.py collect [--date YYYYMMDD ...] [--force]   새 조사일 찾아 원본 수집
  python pw.py load    [--date YYYYMMDD ...]             원본을 SQLite 에 멱등 적재
  python pw.py match   [--refresh]                       상품에 영양 항목 잇기 (매칭률이 품질 지표)
  python pw.py qa                                         자동 점검. fail 이면 종료 코드 1
  python pw.py report  [--force] [--any-day]              주간 리포트 (월요일, 새 조사분이 있을 때)
  python pw.py notify  [--report PATH] [--text ...]       슬랙 알림
  python pw.py calls                                      조사일별로 실제 쓴 API 호출 수

단계를 나눈 이유: 어디서 깨졌는지 보이게 하고, 깨진 단계만 다시 돌리기 위해서다(A12).
"""
import argparse
import json
import sys

from pipeline import api, collect, load, match, qa, report, notify


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pw")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect"); c.add_argument("--date", nargs="*"); c.add_argument("--force", action="store_true")
    l = sub.add_parser("load");    l.add_argument("--date", nargs="*")
    m = sub.add_parser("match");   m.add_argument("--refresh", action="store_true", help="기존 매칭을 버리고 다시")
    sub.add_parser("qa")
    r = sub.add_parser("report");  r.add_argument("--force", action="store_true"); r.add_argument("--any-day", action="store_true", help="월요일이 아니어도 만든다")
    n = sub.add_parser("notify");  n.add_argument("--report", default=None); n.add_argument("--text", default=None)
    sub.add_parser("calls")
    a = ap.parse_args(argv)

    if a.cmd == "collect":
        try:
            collect.run(dates=a.date or None, force=a.force)
        except api.Unreachable as e:
            # 수집 도중에 끊긴 경우다. 이미 받아둔 조사분으로 리포트는 만들 수 있으므로
            # 여기서 파이프라인을 세우지 않는다. 받다 만 조사일은 다음 실행이 다시 받는다.
            print(f"수집 중단: {e}", file=sys.stderr)
            print("이미 받아둔 조사분으로 이어서 진행한다.")
    elif a.cmd == "load":
        load.run(dates=a.date or None)
    elif a.cmd == "match":
        r = match.run(refresh=a.refresh)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif a.cmd == "qa":
        res = qa.run()
        print(qa.as_markdown(res))
        if res["overall"] == "fail":
            print("점검 실패. 파이프라인을 세운다.", file=sys.stderr); sys.exit(1)
    elif a.cmd == "report":
        report.run(force=a.force, monday_only=not a.any_day)
    elif a.cmd == "notify":
        notify.run(report_path=a.report, text=a.text)
    elif a.cmd == "calls":
        # 호출 수는 프로세스마다 0 에서 시작하므로 이 자리에서 세면 늘 0 이다.
        # 수집할 때 meta.json 에 적어 둔 실제 값을 읽는다. 하루 2,000회 한도를 볼 때 쓴다.
        for meta in sorted(api.RAW.glob("*/meta.json")):
            m = json.loads(meta.read_text(encoding="utf8"))
            print(f"{m['inspect_day']}  가격 {m.get('api_calls_price', 0):>5}회  "
                  f"상품 {m.get('n_goods', 0)}개  가격 {m.get('n_prices', 0):,}건  "
                  f"실패 {m.get('n_goods_failed', 0)}개")
        return
    print(f"API 호출: 가격 {api.CALLS['price']}회, 영양 {api.CALLS['nutrient']}회", file=sys.stderr)


if __name__ == "__main__":
    main()
