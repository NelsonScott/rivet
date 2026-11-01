#!/usr/bin/env python3
"""rivet-doctor unit tests: no Claude call, no real command with side effects.

    python3 backend/tests/test_doctor.py
"""
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="rivet-doctor-test-")
os.environ["RIVET_STATE_DIR"] = TMP
sys.path.insert(0, os.path.join(HERE, ".."))
import rivet_doctor as D  # noqa: E402


def events(fn, *a, **kw):
    buf = io.StringIO()
    em = D.Emitter(stream=buf)
    rc = fn(em, *a, **kw)
    evs = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    return rc, evs


class NormalizeTest(unittest.TestCase):
    def test_denylisted_fix_is_disarmed(self):
        d = D.normalize({"fix": {"available": True, "commands": ["sudo dnf5 -y upgrade kernel", "sudo reboot"]}})
        self.assertFalse(d["fix"]["available"])
        self.assertEqual(d["fix"]["blocked"], ["sudo reboot"])
        self.assertIn("refused", d["fix"]["explanation"])

    def test_denylist_shapes(self):
        bad = ["reboot", "sudo systemctl reboot", "systemctl --no-block poweroff", "shutdown -r now",
               "rm -rf /", "rm -rf /boot", "sudo rm -rf /usr", "dd if=/dev/zero of=/dev/sda", "mkfs.ext4 /dev/sda1",
               "echo hi > /dev/sda", ":(){ :|:& };:", "sudo dnf5 -y upgrade kernel; reboot"]
        ok = ["sudo dnf5 -y --refresh upgrade kernel", "sudo akmods --kernels 7.2.6-200.fc44.x86_64",
              "rm -rf /var/cache/akmods/nvidia/*.failed.log", "sudo rm -f /var/cache/dnf5daemon-server/updates-*/repodata/repomd.xml",
              "modinfo -k 7.2.6-200.fc44.x86_64 nvidia", "sudo systemctl restart akmods", "grep reboot /var/log/x"]
        for c in bad:
            self.assertTrue(D.DENYLIST.search(c), c)
        for c in ok:
            self.assertFalse(D.DENYLIST.search(c), c)

    def test_empty_commands_means_no_fix(self):
        d = D.normalize({"fix": {"available": True, "commands": []}})
        self.assertFalse(d["fix"]["available"])

    def test_defaults_are_ui_safe(self):
        d = D.normalize({})
        self.assertEqual(d["reboot"]["safe"], "unknown")
        self.assertEqual(d["severity"], "attention")
        self.assertEqual(d["after_fix"]["verify_commands"], [])
        self.assertIsInstance(d["evidence"], list)


class SplitRootTest(unittest.TestCase):
    def test_sudo_prefix_is_stripped_and_marks_root(self):
        self.assertEqual(D.split_root("sudo dnf5 -y upgrade x", True), ("dnf5 -y upgrade x", True))
        self.assertEqual(D.split_root("sudo -n true", False), ("true", True))
        self.assertEqual(D.split_root("sudo -E -H depmod -a 7.2.9", False), ("depmod -a 7.2.9", True))

    def test_root_tools_are_root_only_when_needed(self):
        self.assertEqual(D.split_root("dnf5 -y upgrade x", True), ("dnf5 -y upgrade x", True))
        self.assertEqual(D.split_root("depmod -a 7.2.9", True), ("depmod -a 7.2.9", True))
        self.assertEqual(D.split_root("dnf5 -y upgrade x", False), ("dnf5 -y upgrade x", False))
        self.assertEqual(D.split_root("rpm -q kernel", True), ("rpm -q kernel", False))

    def test_batch_script_marks_steps_and_stops_at_the_first_failure(self):
        script = D.batch_script([(0, "echo one"), (1, "false"), (2, "echo never")])
        import subprocess
        cp = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(cp.stdout.split("\n")[:5],
                         [f"{D.STEP_MARK} 0", "one", f"{D.STEP_MARK} 1", f"{D.FAIL_MARK} 1 1", ""])
        self.assertNotIn("never", cp.stdout)


class LastRunTest(unittest.TestCase):
    def test_keeps_last_run_and_trims_progress(self):
        p = Path(TMP) / "apply.log"
        lines = [json.dumps({"step": "download", "status": "start", "msg": "old run"}),
                 json.dumps({"step": "done", "status": "ok", "msg": "old done"}),
                 json.dumps({"step": "download", "status": "start", "msg": "Preparing 12"})]
        lines += [json.dumps({"step": "akmod", "status": "progress", "msg": f"Building ({i}s)"}) for i in range(60)]
        lines += [json.dumps({"step": "akmod", "status": "fail", "msg": "akmods did not finish"}),
                  json.dumps({"step": "done", "status": "fail", "msg": "Finished with errors"})]
        p.write_text("\n".join(lines) + "\n")
        out = D.last_apply_run(p)
        self.assertNotIn("old run", out)
        self.assertIn("Preparing 12", out)
        self.assertIn("progress lines omitted", out)
        self.assertIn("akmods did not finish", out)
        self.assertIn("Finished with errors", out)
        self.assertLess(out.count("Building"), 25)


class RunFixTest(unittest.TestCase):
    def diag(self, **fix):
        base = {"available": True, "title": "Echo twice", "explanation": "", "needs_root": False,
                "risk": "low", "reversible": "", "commands": ["echo one", "echo two"]}
        base.update(fix)
        d = {"fix": base, "after_fix": {"verify_commands": ["test -d /"], "expect": "root exists"}}
        p = Path(TMP) / "fix.json"
        p.write_text(json.dumps(d))
        return p

    def test_runs_commands_then_verifies(self):
        rc, evs = events(D.run_fix, self.diag())
        self.assertEqual(rc, 0)
        steps = [(e["step"], e["status"]) for e in evs]
        self.assertIn(("fix", "ok"), steps)
        self.assertIn(("verify", "ok"), steps)
        self.assertEqual(evs[-1]["status"], "ok")
        self.assertEqual(evs[-1]["results"], {"ran": 2, "planned": 2})
        self.assertTrue(any(e.get("log") == "one" for e in evs))

    def test_stops_at_first_failure(self):
        rc, evs = events(D.run_fix, self.diag(commands=["echo one", "false", "echo never"]))
        self.assertEqual(rc, 1)
        self.assertEqual(evs[-1]["status"], "fail")
        self.assertIn("Step 2 failed", evs[-1]["summary"])
        self.assertEqual(evs[-1]["results"]["failed_step"], 2)
        self.assertFalse(any(e.get("log") == "never" for e in evs))

    def test_verify_failure_is_reported(self):
        d = json.loads(self.diag().read_text())
        d["after_fix"]["verify_commands"] = ["test -d /nonexistent-dir"]
        p = Path(TMP) / "fix2.json"
        p.write_text(json.dumps(d))
        rc, evs = events(D.run_fix, p)
        self.assertEqual(rc, 1)
        self.assertIn("check failed", evs[-1]["summary"])

    def test_partial_verification_is_fixed_with_a_note(self):
        # 2026-10-04: module installed, only `test ! -e <stale .failed.log>` failed
        d = json.loads(self.diag().read_text())
        d["after_fix"]["verify_commands"] = ["test -d /", "test -d /nonexistent-dir", "true"]
        p = Path(TMP) / "fix3.json"
        p.write_text(json.dumps(d))
        rc, evs = events(D.run_fix, p)
        self.assertEqual(rc, 0)
        done = evs[-1]
        self.assertEqual(done["status"], "warn")
        self.assertIn("Fixed, with a note", done["summary"])
        self.assertIn("2 of 3 checks passed", done["cause"])
        self.assertEqual(done["results"]["verify_failed"], ["test -d /nonexistent-dir"])
        self.assertTrue(any(e["step"] == "verify" and e["status"] == "warn" for e in evs))
        # every check ran, the first failure did not stop the rest
        self.assertEqual(sum(1 for e in evs if e["step"] == "verify" and e["status"] == "progress"), 3)

    def test_refuses_denylisted_and_unavailable(self):
        rc, evs = events(D.run_fix, self.diag(commands=["sudo reboot"]))
        self.assertEqual(rc, 1)
        self.assertIn("Refusing", evs[-1]["summary"])
        rc, evs = events(D.run_fix, self.diag(available=False))
        self.assertEqual(rc, 1)
        self.assertIn("no fix", evs[-1]["summary"])



class RunFixRootTest(unittest.TestCase):
    """Root steps: no passwordless sudo on this box since 2026-10-07, so they go through
    pkexec, and consecutive root steps share ONE pkexec call (one face scan per fix)."""

    def setUp(self):
        self.bin = Path(TMP) / "rootbin"
        self.bin.mkdir(exist_ok=True)
        self.calls = Path(TMP) / "root.calls"
        self.calls.write_text("")
        sudo = self.bin / "sudo"
        sudo.write_text("#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n")
        self.mode = Path(TMP) / "pkexec.mode"          # clean_env() drops the environment
        self.mode.write_text("ok")
        pk = self.bin / "pkexec"
        pk.write_text("#!/bin/sh\n"
                      f"printf 'pkexec %s\\n' \"$*\" >> '{self.calls}'\n"
                      f"case \"$(cat '{self.mode}')\" in dismiss) exit 126;; deny) exit 127;; esac\n"
                      "echo 'as-root'\nexec \"$@\"\n")
        for f in (sudo, pk):
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self.old_path}"
        os.environ["RIVET_FAKE_BIN"] = str(self.bin)

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("RIVET_FAKE_BIN", None)

    def diag(self, commands, needs_root=True, verify=("true",)):
        d = {"fix": {"available": True, "title": "Root twice", "explanation": "", "needs_root": needs_root,
                     "risk": "low", "reversible": "", "commands": commands},
             "after_fix": {"verify_commands": list(verify), "expect": "ok"}}
        p = Path(TMP) / "rootfix.json"
        p.write_text(json.dumps(d))
        return p

    def pkexec_calls(self):
        # one call = one "pkexec …" line; the batch script that follows spans several lines
        return [l for l in self.calls.read_text().splitlines() if l.startswith("pkexec ")]

    def test_consecutive_root_steps_share_one_pkexec_call(self):
        rc, evs = events(D.run_fix, self.diag(["sudo echo one", "sudo echo two", "echo three"]))
        self.assertEqual(rc, 0, evs[-1])
        self.assertEqual(len(self.pkexec_calls()), 1, "both root steps behind one prompt")
        self.assertIn("as-root", [e.get("log") for e in evs])
        steps = [e["msg"] for e in evs if e["step"] == "fix" and (e.get("msg") or "").startswith("Step ")]
        self.assertEqual(steps, ["Step 1 of 3: sudo echo one", "Step 2 of 3: sudo echo two", "Step 3 of 3: echo three"])
        logs = [e.get("log") for e in evs]
        self.assertIn("$ echo one", logs, "the sudo prefix is gone from what runs")
        self.assertEqual(logs.count("one"), 1)
        self.assertIn("three", logs)
        self.assertEqual(evs[-1]["results"], {"ran": 3, "planned": 3})
        asked = [e["msg"] for e in evs if (e.get("msg") or "").startswith("Asking for permission")]
        self.assertEqual(len(asked), 1)
        self.assertIn("face or password", asked[0])

    def test_failure_inside_the_batch_names_the_step(self):
        rc, evs = events(D.run_fix, self.diag(["sudo echo one", "sudo false", "sudo echo never"]))
        self.assertEqual(rc, 1)
        self.assertIn("Step 2 failed (exit 1)", evs[-1]["summary"])
        self.assertEqual(evs[-1]["results"], {"ran": 1, "planned": 3, "failed_step": 2})
        self.assertNotIn("never", [e.get("log") for e in evs])

    def test_dismissed_prompt_says_so_and_runs_nothing(self):
        self.mode.write_text("dismiss")
        rc, evs = events(D.run_fix, self.diag(["sudo echo one", "sudo echo two"]))
        self.assertEqual(rc, 1)
        self.assertIn("Step 1 did not run: the permission prompt was dismissed", evs[-1]["summary"])
        self.assertEqual(evs[-1]["results"]["ran"], 0)
        self.assertIn("dismissed", evs[-1]["results"]["refused"])
        self.assertNotIn("one", [e.get("log") for e in evs])

    def test_root_tool_without_sudo_is_root_when_needs_root(self):
        rc, evs = events(D.run_fix, self.diag(["depmod -a 7.2.9-201.fc44.x86_64"], needs_root=True))
        self.assertEqual(len(self.pkexec_calls()), 1)
        self.assertIn("depmod -a 7.2.9-201.fc44.x86_64", self.calls.read_text())

    def test_user_steps_never_touch_pkexec(self):
        rc, evs = events(D.run_fix, self.diag(["echo one", "echo two"], needs_root=False))
        self.assertEqual(rc, 0)
        self.assertEqual(self.pkexec_calls(), [])
        self.assertEqual(evs[-1]["results"], {"ran": 2, "planned": 2})


class FakeClaudeTest(unittest.TestCase):
    """run_claude against a shell script that replays a stream-json transcript."""

    def fake_claude(self, lines):
        script = Path(TMP) / "claude"
        transcript = Path(TMP) / "transcript.jsonl"
        transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        script.write_text(f"#!/bin/sh\ncat {transcript}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        os.environ["RIVET_CLAUDE_BIN"] = str(script)
        D.CLAUDE_CANDIDATES = (str(script),)

    def test_stream_is_narrated_and_structured_output_returned(self):
        diag = {"headline": "Nothing is broken", "what_happened": "x", "severity": "fine",
                "reboot": {"safe": "yes", "why": "y"}, "fix": {"available": False, "title": "", "explanation": "",
                "commands": [], "needs_root": False, "risk": "low", "reversible": ""},
                "after_fix": {"verify_commands": [], "expect": ""}, "confidence": "high", "evidence": ["e"]}
        self.fake_claude([
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "rpm -q kernel", "description": "Which kernels"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "kernel-7.2.5"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "sudo dnf5 makecache"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                                                       "content": "Permission for this tool use was denied."}]}},
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "duration_ms": 1200,
             "total_cost_usd": 0.01, "structured_output": diag},
        ])
        buf = io.StringIO()
        em = D.Emitter(stream=buf)
        out = D.run_claude(em, "prompt", "opus")
        evs = [json.loads(line) for line in buf.getvalue().splitlines()]
        self.assertEqual(out["headline"], "Nothing is broken")
        self.assertEqual(out["_meta"]["checks"], 2)
        self.assertEqual(out["_meta"]["denied"], 1)
        msgs = [e.get("msg") for e in evs]
        logs = [e.get("log") for e in evs]
        self.assertIn("Checking: Which kernels", msgs)
        self.assertIn("$ rpm -q kernel", logs)
        self.assertTrue(any(l and l.startswith("(not allowed, read-only mode: sudo dnf5 makecache") for l in logs))
        self.assertTrue(any(l and "kernel-7.2.5" in l for l in logs))

    def test_investigation_timeout_falls_back_to_evidence_only(self):
        # a claude that never answers with tools, and answers at once without them
        diag = {"headline": "From evidence", "what_happened": "w", "severity": "fine",
                "reboot": {"safe": "yes", "why": "y"}, "fix": {"available": False, "title": "", "explanation": "",
                "commands": [], "needs_root": False, "risk": "low", "reversible": ""},
                "after_fix": {"verify_commands": [], "expect": ""}, "confidence": "high", "evidence": []}
        script = Path(TMP) / "claude2"
        good = Path(TMP) / "good.jsonl"
        good.write_text(json.dumps({"type": "result", "subtype": "success", "structured_output": diag}) + "\n")
        script.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = '--tools' ] && shift && [ \"$1\" = '' ] && cat %s && exit 0; shift; done\n"
                          "echo '{\"type\":\"result\",\"subtype\":\"error_max_turns\",\"is_error\":true,\"result\":\"ran out\"}'\n" % good)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        D.CLAUDE_CANDIDATES = (str(script),)
        real_gather = D.gather_evidence
        D.gather_evidence = lambda p, em=None: "### fake evidence"
        try:
            rc, evs = events(D.diagnose, log_path=Path(TMP) / "nope.log", model="opus")
        finally:
            D.gather_evidence = real_gather
        self.assertEqual(rc, 0)
        self.assertEqual(evs[-1]["diagnosis"]["headline"], "From evidence")
        self.assertIn("fallback", evs[-1]["diagnosis"]["_meta"])
        self.assertTrue(any("evidence alone" in (e.get("msg") or "") for e in evs))

    def test_out_of_usage_walks_the_model_ladder(self):
        """opus exhausted -> the next model answers, tools and all (2026-09-22)."""
        diag = {"headline": "Answered by the next model", "what_happened": "w", "severity": "fine",
                "reboot": {"safe": "yes", "why": "y"}, "fix": {"available": False, "title": "", "explanation": "",
                "commands": [], "needs_root": False, "risk": "low", "reversible": ""},
                "after_fix": {"verify_commands": [], "expect": ""}, "confidence": "high", "evidence": []}
        good = Path(TMP) / "ladder-good.jsonl"
        good.write_text(json.dumps({"type": "result", "subtype": "success", "structured_output": diag}) + "\n")
        script = Path(TMP) / "claude-ladder"
        # opus is out of credits whatever the flags; sonnet answers
        script.write_text(
            "#!/bin/sh\n"
            "for a in \"$@\"; do case \"$a\" in opus) echo '{\"type\":\"result\",\"subtype\":\"error\","
            "\"is_error\":true,\"result\":\"You are out of usage credits.\"}'; exit 0;; esac; done\n"
            f"cat {good}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        D.CLAUDE_CANDIDATES = (str(script),)
        D.FALLBACK_MODELS = ["sonnet"]
        buf = io.StringIO()
        em = D.Emitter(stream=buf)
        out = D.ask(em, "prompt", "opus")
        evs = [json.loads(line) for line in buf.getvalue().splitlines()]
        self.assertEqual(out["headline"], "Answered by the next model")
        self.assertEqual(out["_meta"]["ladder"], ["opus", "sonnet"])
        self.assertTrue(any("Asking sonnet instead" in (e.get("msg") or "") for e in evs))
        self.assertTrue(any("no usage left" in (e.get("log") or "") for e in evs))

    def test_every_model_out_of_usage_says_so_plainly(self):
        script = Path(TMP) / "claude-broke"
        script.write_text("#!/bin/sh\necho '{\"type\":\"result\",\"subtype\":\"error\",\"is_error\":true,"
                          "\"result\":\"You are out of usage credits.\"}'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        D.CLAUDE_CANDIDATES = (str(script),)
        D.FALLBACK_MODELS = ["sonnet"]
        real_gather = D.gather_evidence
        D.gather_evidence = lambda p, em=None: "### fake evidence"
        try:
            rc, evs = events(D.diagnose, log_path=Path(TMP) / "nope.log", model="opus")
        finally:
            D.gather_evidence = real_gather
        self.assertEqual(rc, 1)
        self.assertIn("no model has usage left", evs[-1]["summary"])
        self.assertIn("opus, sonnet", evs[-1]["summary"])
        self.assertIn("nothing was changed", evs[-1]["summary"])

    def test_error_result_raises(self):
        self.fake_claude([{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "too many"}])
        with self.assertRaises(RuntimeError):
            D.run_claude(D.Emitter(stream=io.StringIO()), "p", "opus")

    def test_diagnose_end_to_end_with_fake_claude(self):
        diag = {"headline": "H", "what_happened": "W", "severity": "attention",
                "reboot": {"safe": "no", "why": "y"}, "fix": {"available": True, "title": "T", "explanation": "E",
                "commands": ["echo fix"], "needs_root": False, "risk": "low", "reversible": "r"},
                "after_fix": {"verify_commands": ["true"], "expect": "ok"}, "confidence": "medium", "evidence": []}
        self.fake_claude([{"type": "result", "subtype": "success", "structured_output": diag, "num_turns": 1}])
        log = Path(TMP) / "apply2.log"
        log.write_text(json.dumps({"step": "done", "status": "fail", "msg": "boom"}) + "\n")
        real_gather = D.gather_evidence
        D.gather_evidence = lambda p, em=None: "### fake evidence"
        try:
            rc, evs = events(D.diagnose, log_path=log, model="opus")
        finally:
            D.gather_evidence = real_gather
        self.assertEqual(rc, 0)
        done = evs[-1]
        self.assertEqual(done["status"], "ok")
        self.assertEqual(done["diagnosis"]["headline"], "H")
        self.assertTrue(Path(done["fix_path"]).is_file())
        # (under `unittest discover` an earlier module may own common.STATE_DIR: compare to the doctor's dir)
        self.assertTrue(done["fix_path"].startswith(str(D.DOCTOR_DIR) + os.sep))
        # and the saved fix runs
        rc, evs2 = events(D.run_fix, Path(done["fix_path"]))
        self.assertEqual(rc, 0)


class FakeModeTest(unittest.TestCase):
    def test_fake_diagnose_and_run(self):
        os.environ["RIVET_FAKE_PACE"] = "0"
        rc, evs = events(D.fake_diagnose, "ok")
        self.assertEqual(rc, 0)
        self.assertEqual(evs[-1]["diagnosis"]["fix"]["available"], True)
        rc, evs = events(D.fake_diagnose, "nofix")
        self.assertFalse(evs[-1]["diagnosis"]["fix"]["available"])
        rc, evs = events(D.fake_diagnose, "nodiag")
        self.assertEqual(evs[-1]["status"], "fail")
        rc, evs = events(D.fake_run, "runfail", Path("/dev/null"))
        self.assertEqual(evs[-1]["status"], "fail")
        rc, evs = events(D.fake_run, "ok", Path("/dev/null"))
        self.assertEqual(evs[-1]["status"], "ok")


class SilentChildTest(unittest.TestCase):
    """Deadlines must hold while the child says nothing (a sudo prompt, a stalled claude)."""

    def test_silent_fix_command_is_killed_at_the_deadline(self):
        t0 = time.monotonic()
        rc, evs = events(D.stream_lines, "fix", "echo started; exec sleep 30", timeout=1)
        self.assertEqual(rc, 124)
        self.assertLess(time.monotonic() - t0, 6)
        self.assertTrue(any("started" in (e.get("log") or "") for e in evs))
        self.assertTrue(any("Timed out" in (e.get("msg") or "") for e in evs))

    def test_claude_that_hangs_silently_times_out_and_stderr_is_narrated(self):
        script = Path(TMP) / "claude-hang"
        script.write_text("#!/bin/sh\necho 'warming up' >&2\nexec sleep 30\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        os.environ.pop("RIVET_CLAUDE_BIN", None)
        D.CLAUDE_CANDIDATES = (str(script),)
        buf = io.StringIO()
        t0 = time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            D.run_claude(D.Emitter(stream=buf), "p", "opus", timeout=1)
        self.assertIn("did not finish", str(cm.exception))
        self.assertLess(time.monotonic() - t0, 6)
        evs = [json.loads(line) for line in buf.getvalue().splitlines()]
        self.assertTrue(any((e.get("log") or "") == "warming up" for e in evs))


if __name__ == "__main__":
    unittest.main(verbosity=1)
