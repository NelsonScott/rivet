#!/usr/bin/env python3
"""systemctl --user for health checks: units in $FAKE_WORLD/units.json; restart of the
portal re-creates its by-app dir (the real one's FUSE mount)."""
import json, os, sys, time
w = os.environ["FAKE_WORLD"]
units = json.load(open(os.path.join(w, "units.json")))
a = [x for x in sys.argv[1:] if x != "--user"]
open(os.path.join(w, "calls"), "a").write(" ".join(a) + "\n")
if a[0] == "is-active":
    u = units.get(a[1])
    print("active" if u else "inactive"); sys.exit(0 if u else 3)
if a[0] == "show":
    u = units.get(a[1])
    print("ActiveState=" + ("active" if u else "inactive"))
    print("ActiveEnterTimestamp=@%d" % int(u["since"]) if u else "ActiveEnterTimestamp=")
    sys.exit(0)
if a[0] == "restart":
    if os.environ.get("FAKE_RESTART") == "fail":
        print("Job for %s failed." % a[1], file=sys.stderr); sys.exit(1)
    units[a[1]] = {"since": time.time()}
    json.dump(units, open(os.path.join(w, "units.json"), "w"))
    if a[1] == "xdg-document-portal.service":
        os.makedirs(os.path.join(w, "run", "doc", "by-app"), exist_ok=True)
    sys.exit(0)
sys.exit("fake systemctl refuses " + " ".join(sys.argv[1:]))
