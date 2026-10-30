import httpx
import json

def check():
    r = httpx.get("http://127.0.0.1:4040/api/requests/http")
    data = r.json()
    print("TOTAL REQUESTS IN NGROK:", len(data.get("requests", [])))
    for req in data.get("requests", []):
        r_info = req.get("request", {})
        resp_info = req.get("response", {})
        print(f"[{req.get('start')}] {r_info.get('method')} {r_info.get('uri')} -> {resp_info.get('status_code')}")
        if "answer" in r_info.get("uri", ""):
            # print response body
            body_url = "http://127.0.0.1:4040" + req.get("uri")
            b_data = httpx.get(body_url).json()
            raw_resp = b_data.get("response", {}).get("raw")
            print("  ANSWER RAW RESPONSE:", raw_resp)

if __name__ == "__main__":
    check()
