import os, urllib.request, xml.etree.ElementTree as ET
# Same setting as the exporter, as "ip:port,ip:port"; the ports are only for its proxy
if not os.getenv("PROXY_PORTS"):
    raise SystemExit("PROXY_PORTS is not set, expected ip:port,ip:port")
for ip in (entry.rsplit(":", 1)[0].strip() for entry in os.environ["PROXY_PORTS"].split(",")):
    host = ip.split(".")[-1]
    try:
        req = urllib.request.urlopen(f"http://{ip}/data/static.xml", timeout=5)
        root = ET.fromstring(req.read())
        rooms = {r.get("nr"): r for r in root.findall(".//HEATAREA")}
        for c in root.findall(".//HEATCTRL"):
            if c.findtext("INUSE") == "1" and c.findtext("HEATAREA_NR") in rooms:
                r = rooms[c.findtext("HEATAREA_NR")]
                name = r.findtext("HEATAREA_NAME")
                actual = r.findtext("T_ACTUAL")
                target = r.findtext("T_TARGET")
                valve = c.findtext("ACTOR_PERCENT")
                print(f".{host} | {name:<15} | Actual: {actual:>4}°C | Target: {target:>4}°C | Valve: {valve:>3}%")
    except Exception as e:
        print(f".{host} | Fetch failed: {e}")
