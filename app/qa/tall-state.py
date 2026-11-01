#!/usr/bin/env python3
"""Write a TALL state.json (sample-state.json with the dnf group cloned until it has
~130 rows) to $1. app/qa-render.sh uses it: the WebKitGTK 2.54 non-DMA-BUF paint bug only
shows once the page is several screens tall (15 rows render fine, 130 do not)."""
import copy, json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
doc = json.load(open(os.path.join(here, "..", "ui", "sample-state.json")))
doc["status"] = "ok"
dnf = next(g for g in doc["groups"] if g["id"] == "dnf")
base = copy.deepcopy(dnf["items"])
n = 1
while len(dnf["items"]) < int(os.environ.get("RIVET_TALL_ROWS", "130")):
    for it in base:
        it = copy.deepcopy(it)
        it["id"] = "%s~%d" % (it["id"], n)
        it["name"] = it["display_name"] = "%s-%d" % (it["name"], n)
        dnf["items"].append(it)
    n += 1
dnf["items"].sort(key=lambda i: i["name"])
doc["summary"]["total"] = sum(len(g["items"]) for g in doc["groups"] if g["id"] != "held")
out = sys.argv[1]
os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump(doc, open(out, "w"), indent=1)
print("tall state: %d rows -> %s" % (doc["summary"]["total"], out))
