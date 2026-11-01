#!/usr/bin/env python3
"""Stand-in for rivet-doctor in app QA: the REAL backend/rivet_doctor.py in --fake
mode, so no Claude call is made and no command is run. RIVET_DOCTOR_SCENARIO picks
the diagnose story (ok | nofix | nodiag), RIVET_DOCTOR_RUN_SCENARIO the run story
(ok | runfail). Both default to ok."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "..", "backend", "rivet_doctor.py")

args = sys.argv[1:]
if args and args[0] == "health":
    # 2026-11-01: the REAL health checks against a fake world (fake systemctl, scratch run dir):
    # RIVET_HEALTH_SCENARIO=portal-stale -> the document portal is active, its by-app dir gone;
    # anything else -> healthy. The fix restarts the fake unit, which brings the dir back.
    import json, time
    sd = os.environ.get("RIVET_STATE_DIR") or os.path.expanduser("~/.local/state/rivet")
    w = os.path.join(sd, "fake-health")
    if not os.path.isdir(w):
        os.makedirs(os.path.join(w, "run", "doc"))
        stale = os.environ.get("RIVET_HEALTH_SCENARIO") == "portal-stale"
        if not stale:
            os.makedirs(os.path.join(w, "run", "doc", "by-app"))
        json.dump({"xdg-document-portal.service": {"since": time.time() - 3600}}, open(os.path.join(w, "units.json"), "w"))
    env = dict(os.environ, FAKE_WORLD=w, RIVET_RUN_DIR=os.path.join(w, "run"),
               RIVET_SYSTEMCTL=os.path.join(HERE, "..", "..", "backend", "fixtures", "fake_systemctl.py"))
    os.execve(sys.executable, [sys.executable, BACKEND] + args, env)
which = "run" if args and args[0] == "run" else "diagnose"
scenario = os.environ.get("RIVET_DOCTOR_RUN_SCENARIO" if which == "run" else "RIVET_DOCTOR_SCENARIO", "ok")
env = dict(os.environ, RIVET_FAKE_PACE=os.environ.get("RIVET_FAKE_PACE", "0.15"))
os.execve(sys.executable, [sys.executable, BACKEND] + args + ["--fake", scenario], env)
