import json, urllib.request
for eid in ["evt-gas_surge-1794025387-1729", "evt-gas_surge-1794025474-9761"]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8000/events/{eid}", timeout=10) as r:
            d = json.loads(r.read())
        print(eid, d["status"], (d.get("analysis") or {}).get("risk_level"))
    except Exception as e:
        print(eid, "ERR", e)
