import urllib.request, xml.etree.ElementTree as ET
for ip in ["192.168.10.71", "192.168.10.72"]:
    host = ip.split(".")[-1]
    try:
        req = urllib.request.urlopen(f"http://{ip}/data/static.xml", timeout=5)
        root = ET.fromstring(req.read())
        rooms = {r.get("nr"): r for r in root.findall(".//HEATAREA")}
        for c in root.findall(".//HEATCTRL"):
            if c.findtext("INUSE") == "1" and c.findtext("HEATAREA_NR") in rooms:
                r = rooms[c.findtext("HEATAREA_NR")]
                name = r.findtext("HEATAREA_NAME")
                ist = r.findtext("T_ACTUAL")
                soll = r.findtext("T_TARGET")
                ventil = c.findtext("ACTOR_PERCENT")
                print(f".{host} | {name:<15} | Ist: {ist:>4}°C | Soll: {soll:>4}°C | Ventil: {ventil:>3}%")
    except Exception as e:
        print(f".{host} | Fehler beim Abruf: {e}")
