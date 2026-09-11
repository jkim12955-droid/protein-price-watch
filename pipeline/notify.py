"""슬랙 웹훅으로 한 줄 알림. 웹훅이 없으면 출력만 하고 정상 종료한다."""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from . import api


def run(report_path: str | None = None, text: str | None = None) -> bool:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    latest = api.DATA / "report_latest.json"
    if text is None:
        if report_path and latest.exists():
            s = json.loads(latest.read_text(encoding="utf8"))
            repo = os.environ.get("GITHUB_REPOSITORY")
            link = f"https://github.com/{repo}/blob/main/{report_path}" if repo else report_path
            d = s["survey_day"]
            # "정상" 은 받침이 있어 "이다", "주의"·"실패" 는 받침이 없어 "다" 가 붙는다.
            verdict = {"ok": "정상이다", "warn": "주의다", "fail": "실패다"}.get(s.get("overall_qa"), "아직 없다")
            lines = [f"{int(d[4:6])}월 {int(d[6:])}일 조사분 단백질 가격 리포트가 나왔다.",
                     f"단백질 1g이 가장 싼 셋은 {s['top3']}이다."]
            if s.get("baseline_won"):
                lines.append(f"늘 사는 {s['baseline_name']}은 {s['baseline_won']:,.1f}원이다.")
            if s.get("watch"):
                lines.append("관심 품목은 " + ", ".join(s["watch"][:4]) + "이다.")
            flagged = (s.get("qa_fail") or []) + (s.get("qa_warn") or [])
            lines.append(f"자동 점검 결과는 {verdict}." + (f" {', '.join(flagged)} 항목을 확인해야 한다." if flagged else ""))
            lines.append(link)
            text = "\n".join(lines)
        else:
            text = "이번 주는 새 조사분이 없어 리포트를 만들지 않았다."
    if not url:
        print("SLACK_WEBHOOK_URL 없음. 보낼 내용:\n" + text)
        return False
    body = json.dumps({"text": text}).encode("utf8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
    except Exception as e:  # noqa: BLE001
        # 수집 코드와 같은 문제다. 일부 맥 파이썬은 시스템 인증서를 못 찾는다.
        # CI 에서는 인증서가 정상이라 우회하지 않고 그대로 실패시킨다.
        if not api._is_cert_error(e) or api.ON_CI:
            raise
        print("경고: 인증서 검증 실패. 슬랙 전송을 검증 없이 한 번 더 시도한다. (로컬 파이썬 인증서 문제)", file=sys.stderr)
        with urllib.request.urlopen(req, timeout=20, context=api._context(True)) as r:
            ok = r.status == 200
    print("슬랙 전송", "성공" if ok else f"실패 {r.status}")
    return ok
