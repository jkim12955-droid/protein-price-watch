import os, sys, time, urllib.parse, urllib.request

k = urllib.parse.quote(os.environ["PRICE_API_KEY"], safe="")
URL = ("https://apis.data.go.kr/B551919/ProductPriceInfoService/getProductPriceInfoSvc"
       f"?serviceKey={k}&numOfRows=1&pageNo=1&goodInspectDay=20260828&goodId=1000")
tag = sys.argv[1] if len(sys.argv) > 1 else "py"
t = time.time()
try:
    with urllib.request.urlopen(URL, timeout=20) as r:
        b = r.read()
    print(f"{tag}  성공 code={r.status} {time.time()-t:.2f}s {len(b)}B")
except Exception as e:
    print(f"{tag}  실패 {time.time()-t:.2f}s {type(e).__name__}")
