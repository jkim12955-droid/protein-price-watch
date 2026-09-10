"""공공데이터포털 두 API의 얇은 클라이언트.

- 참가격 생필품 가격 정보 (한국소비자원)  : XML
- 식품영양성분DB (식약처)                  : JSON

표준 라이브러리만 쓴다. GitHub Actions에서 pip 설치 없이 돌리기 위해서다.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"


def load_env() -> None:
    """저장소 루트의 .env 를 읽어 환경변수로 올린다. 이미 있는 값은 덮지 않는다."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env()

PRICE_KEY = os.environ.get("PRICE_API_KEY", "")
NUTRI_KEY = os.environ.get("NUTRIENT_API_KEY", "")
PRICE_BASE = "https://apis.data.go.kr/B551919/ProductPriceInfoService"
NUTRI_BASE = "https://apis.data.go.kr/1471000/FoodNtrCpntDbInfo02"

# 호출 횟수를 세어 둔다. 하루 한도(가격 2,000 / 영양 10,000) 감시용.
CALLS = {"price": 0, "nutrient": 0}
NUTRIENT_FAILURES: list[str] = []   # 재시도 후에도 실패한 검색어


def _context(insecure: bool) -> ssl.SSLContext:
    """검증용 SSL 컨텍스트. certifi 가 있으면 그 인증서 묶음을 쓴다."""
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _is_cert_error(e: BaseException) -> bool:
    """urllib 이 SSL 오류를 URLError 로 감싸서 던지므로 reason 까지 들여다본다."""
    if isinstance(e, ssl.SSLCertVerificationError):
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(e)


SSL_FALLBACK_USED = False

# GitHub Actions 같은 CI 에서는 인증서가 정상이므로 우회를 켜지 않는다.
# 우회가 조용히 켜지면 중간자 공격을 눈치채지 못한다.
ON_CI = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))


class Unreachable(RuntimeError):
    """참가격 서버가 이 IP 에서 오는 연결을 받지 않는 상태.

    실측한 것: apis.data.go.kr 은 GitHub 러너가 받는 애저 IP 대역 일부에서 오는
    TCP 연결을 거절하지 않고 그냥 버린다. 같은 시각에 IP 가 다른 러너는 1.5초 만에 200 을 받는다.
    curl 이든 파이썬이든 똑같이 막히므로 클라이언트 문제가 아니다.
    러너 IP 는 실행마다 바뀌니 이건 고장이 아니라 확률이다. 그래서 따로 이름을 붙여
    호출하는 쪽이 '오늘은 건너뛴다' 로 다룰 수 있게 한다.
    """


def _is_timeout(e: BaseException) -> bool:
    import socket
    reason = getattr(e, "reason", None)
    return isinstance(e, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)) \
        or "timed out" in str(e).lower()


def mask(text) -> str:
    """로그와 예외 메시지에서 인증키를 가린다.

    URL 인코딩된 키는 GitHub 의 시크릿 마스킹에 걸리지 않는다(원본 문자열과 다르다).
    그래서 우리 쪽에서 먼저 지운다.
    """
    return re.sub(r"(serviceKey=|ServiceKey=)[^&\s]+", r"\1***", str(text))


def http_get(url: str, *, timeout: int = 60, retries: int = 3) -> str:
    """GET 한 번. 실패하면 간격을 늘려가며 다시 시도한다.

    인증서 검증에 실패하면(일부 macOS 파이썬은 시스템 인증서를 못 찾는다) 경고를 남기고
    검증 없이 한 번 더 시도한다. PW_INSECURE_SSL=1 이면 처음부터 검증을 끈다.
    GitHub Actions 의 우분투에서는 검증이 정상 동작하므로 이 우회는 로컬에서만 쓰인다.
    """
    global SSL_FALLBACK_USED
    insecure = os.environ.get("PW_INSECURE_SSL") == "1"
    last: Exception | None = None
    attempt = 0
    while attempt < retries:
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=_context(insecure)) as r:
                return r.read().decode("utf8", "replace")
        except Exception as e:  # noqa: BLE001 - 아래에서 종류별로 가른다
            last = e
            if not insecure and _is_cert_error(e) and not ON_CI:
                insecure = True
                if not SSL_FALLBACK_USED:
                    print("경고: 인증서 검증 실패. 이 실행은 검증 없이 계속한다. (로컬 파이썬 인증서 문제)", file=sys.stderr)
                    SSL_FALLBACK_USED = True
                continue  # 재시도 횟수를 소모하지 않고 바로 다시
            attempt += 1
            time.sleep(1.5 * attempt)
    if _is_timeout(last):
        raise Unreachable(f"연결이 닿지 않는다({retries}회 모두 시간 초과): {mask(url)[:160]}")
    raise RuntimeError(f"GET 실패({retries}회): {mask(url)[:160]} :: {mask(last)}")


# ---------------------------------------------------------------- 참가격 (XML)

def _price_url(op: str, **params) -> str:
    q = {"serviceKey": PRICE_KEY, **params}
    return f"{PRICE_BASE}/{op}?" + urllib.parse.urlencode(q)


def parse_price_xml(text: str) -> tuple[str | None, str | None, list[dict]]:
    """<response><result>…레코드…</result><resultCode/><resultMsg/></response> 를 푼다."""
    root = ET.fromstring(text)
    code = root.findtext("resultCode")
    msg = root.findtext("resultMsg")
    result = root.find("result")
    recs: list[dict] = []
    if result is not None:
        for child in result:
            recs.append({c.tag: (c.text or "").strip() for c in child})
    return code, msg, recs


def price_get(op: str, **params) -> list[dict]:
    """한 오퍼레이션을 페이지 끝까지 읽어 레코드 목록으로 돌려준다."""
    page = 1
    rows = int(params.pop("numOfRows", 1000))
    out: list[dict] = []
    while True:
        CALLS["price"] += 1
        text = http_get(_price_url(op, numOfRows=rows, pageNo=page, **params))
        code, msg, recs = parse_price_xml(text)
        if code not in (None, "00"):
            raise RuntimeError(f"참가격 API 오류 [{code}] {msg} (op={op}, params={params})")
        out.extend(recs)
        if len(recs) < rows:
            break
        page += 1
    return out


def fetch_goods() -> list[dict]:
    return price_get("getProductInfoSvc.do")


def fetch_stores() -> list[dict]:
    return price_get("getStoreInfoSvc.do")


def fetch_prices_for_good(inspect_day: str, good_id: str | int) -> list[dict]:
    return price_get("getProductPriceInfoSvc", goodInspectDay=inspect_day, goodId=str(good_id))


def has_survey(inspect_day: str, probe_good_id: str | int = 1000) -> bool:
    """그 날짜에 조사 데이터가 있는지 1건만 물어 확인한다."""
    CALLS["price"] += 1
    # 조사일이 있는지만 묻는 요청이라 짧게 끊는다.
    # 서버가 응답할 IP 라면 1~2초에 오고, 막힌 IP 라면 아무리 기다려도 오지 않는다.
    text = http_get(_price_url("getProductPriceInfoSvc", numOfRows=1, pageNo=1,
                               goodInspectDay=inspect_day, goodId=str(probe_good_id)),
                    timeout=20, retries=2)
    code, _, recs = parse_price_xml(text)
    return code in (None, "00") and len(recs) > 0


# ---------------------------------------------------------------- 영양성분 (JSON)

def nutrient_get(**params) -> tuple[int, list[dict]]:
    """식품영양성분DB 조회. (totalCount, items) 를 돌려준다.

    서버가 가끔 resultCode 01 'System Error!!' 를 돌려준다. 그 경우 간격을 두고 세 번까지 다시 묻고,
    그래도 안 되면 빈 결과를 돌려준다. 한 이름의 실패가 전체 실행을 멈추지 않게 하기 위해서다.
    """
    q = {"serviceKey": NUTRI_KEY, "type": "json", "pageNo": 1, "numOfRows": 100, **params}
    url = f"{NUTRI_BASE}/getFoodNtrCpntDbInq02?" + urllib.parse.urlencode(q)
    last = None
    for attempt in range(3):
        CALLS["nutrient"] += 1
        try:
            text = http_get(url)
            data = json.loads(text)
        except (RuntimeError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2.0 * (attempt + 1))
            continue
        header = data.get("header", {}) or {}
        code = header.get("resultCode")
        if code in (None, "00"):
            body = data.get("body", {}) or {}
            return int(body.get("totalCount", 0) or 0), list(body.get("items", []) or [])
        if code == "03":  # NODATA_ERROR: 결과 없음은 정상
            return 0, []
        last = RuntimeError(f"영양 API 오류 {header}")
        time.sleep(2.0 * (attempt + 1))
    print(f"경고: 영양 API 조회 실패, 빈 결과로 진행 ({params.get('FOOD_NM_KR')!r}): {mask(last)}", file=sys.stderr)
    NUTRIENT_FAILURES.append(str(params.get("FOOD_NM_KR")))
    return 0, []


def nutrient_search(name: str, rows: int = 100, page: int = 1) -> tuple[int, list[dict]]:
    return nutrient_get(FOOD_NM_KR=name, numOfRows=rows, pageNo=page)
