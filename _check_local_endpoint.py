import json
import urllib.request
import urllib.error

url = "http://127.0.0.1:45111/mcp"
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}}}).encode()
req = urllib.request.Request(url, data=body, headers={
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
})
try:
    with urllib.request.urlopen(req, timeout=5) as resp:
        print("status:", resp.status)
        print(resp.read(2000))
except urllib.error.HTTPError as e:
    print("HTTPError:", e.code, e.read(2000))
except Exception as e:
    print("error:", type(e).__name__, e)
