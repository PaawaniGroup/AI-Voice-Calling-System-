import httpx
import base64

def check_403():
    r = httpx.get("http://127.0.0.1:4040/api/requests/http")
    data = r.json()
    for req in data.get("requests", []):
        r_info = req.get("request", {})
        resp_info = req.get("response", {})
        if "stream" in r_info.get("uri", "") and resp_info.get("status_code") == 403:
            body_url = "http://127.0.0.1:4040" + req.get("uri")
            b_data = httpx.get(body_url).json()
            raw_resp = b_data.get("response", {}).get("raw")
            if raw_resp:
                print("--- 403 RESPONSE HEADERS/BODY ---")
                print(base64.b64decode(raw_resp).decode("latin1", errors="replace"))
            print("--- REQUEST HEADERS ---")
            print(b_data.get("request", {}).get("headers"))
            break

if __name__ == "__main__":
    check_403()
