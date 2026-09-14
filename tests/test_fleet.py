"""
Unit tests for the detection and guardrail logic.

Deliberately side-effect free: no launchctl, no docker, no network. The
end-to-end proof that a remedy actually heals a broken resource lives in
selftest.py, which builds disposable resources and runs on a real Mac.
"""
import json
import os
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import watch      # noqa: E402
import medic      # noqa: E402


# --------------------------------------------------------------- http_ok
def _fake_curl(body, code):
    def run(args, **kw):
        return types.SimpleNamespace(
            stdout="%s\n__STATUS__%s" % (body, code), stderr="", returncode=0)
    return run


def test_http_ok_accepts_a_good_response(monkeypatch):
    monkeypatch.setattr(watch.subprocess, "run", _fake_curl("<urlset>x</urlset>", "200"))
    assert watch.http_ok("http://x", expect="<urlset") is True


def test_http_ok_rejects_a_bad_status(monkeypatch):
    monkeypatch.setattr(watch.subprocess, "run", _fake_curl("<urlset>", "429"))
    assert watch.http_ok("http://x") is False


def test_http_ok_rejects_a_parking_page_served_with_200(monkeypatch):
    """The bug this check exists for: HTTP 200 carrying a noindex holding
    page. A status-only monitor calls that healthy."""
    park = "<html><title>Parked Domain name on Hostinger DNS system</title></html>"
    monkeypatch.setattr(watch.subprocess, "run", _fake_curl(park, "200"))
    assert watch.http_ok("http://x", reject="Parked Domain name") is False


def test_http_ok_rejects_when_expected_content_is_absent(monkeypatch):
    monkeypatch.setattr(watch.subprocess, "run", _fake_curl("<html>nope</html>", "200"))
    assert watch.http_ok("http://x", expect="<urlset") is False


def test_http_ok_treats_a_dead_connection_as_failure(monkeypatch):
    monkeypatch.setattr(watch.subprocess, "run", _fake_curl("", "000"))
    assert watch.http_ok("http://x") is False


# ----------------------------------------------------------------- check
def test_disabled_is_its_own_state_not_ok():
    status, detail = watch.check({"name": "j", "disabled": True, "_why": "paused"})
    assert status == "disabled"
    assert "paused" in detail


def test_disabled_never_counts_as_a_problem():
    assert "disabled" in watch.NON_PROBLEM
    assert "ok" in watch.NON_PROBLEM
    assert "failed" not in watch.NON_PROBLEM


def test_a_silent_log_is_stale(tmp_path):
    log = tmp_path / "j.log"
    log.write_text("x\n")
    os.utime(log, (time.time() - 40 * 3600,) * 2)
    status, detail = watch.check({"name": "j", "log": str(log), "max_age_h": 2})
    assert status == "stale"
    assert "silent for" in detail


def test_a_missing_log_is_a_failure(tmp_path):
    status, _ = watch.check({"name": "j", "log": str(tmp_path / "nope.log")})
    assert status == "failed"


def test_stall_matches_any_of_several_patterns(tmp_path):
    """The publisher was watched for one no-op phrase and scored ok through six
    days of a different one. A job has more than one way to do nothing."""
    log = tmp_path / "p.log"
    log.write_text("\n".join(["WordPress : HTTP 429"] * 4))
    job = {"name": "p", "log": str(log), "max_age_h": 99,
           "stall": {"patterns": ["unreviewed drafts", "WordPress : HTTP 4"],
                     "tail_lines": 10, "min_hits": 3, "note": "no output"}}
    status, detail = watch.check(job)
    assert status == "stalled"
    assert "WordPress : HTTP 4" in detail


def test_a_single_legacy_stall_pattern_still_works(tmp_path):
    log = tmp_path / "p.log"
    log.write_text("\n".join(["nothing captured"] * 5))
    job = {"name": "p", "log": str(log), "max_age_h": 99,
           "stall": {"pattern": "nothing captured", "tail_lines": 10, "min_hits": 3}}
    assert watch.check(job)[0] == "stalled"


def test_a_clean_exit_with_an_error_filled_log_is_a_stall(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("\n".join(["ERROR boom"] * 6 + ["fine"]))
    job = {"name": "a", "log": str(log), "max_age_h": 99,
           "error_lines": {"tail_lines": 20, "token": "ERROR", "max": 3}}
    status, detail = watch.check(job)
    assert status == "stalled"
    assert "exits clean" in detail


def test_a_healthy_log_is_ok(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("all good\n")
    assert watch.check({"name": "a", "log": str(log), "max_age_h": 99})[0] == "ok"


# ------------------------------------------------------------ guardrails
def test_attempt_limit_stops_a_retry_storm():
    entry = {"attempts": [time.time()] * 3, "escalated": False}
    allowed, why = medic.may_attempt("j", entry, {})
    assert not allowed and "limit" in why


def test_an_escalated_job_is_left_alone():
    allowed, why = medic.may_attempt("j", {"attempts": [], "escalated": True}, {})
    assert not allowed and "escalated" in why


def test_backoff_holds_off_the_next_attempt():
    one = {"attempts": [time.time()], "escalated": False}
    two = {"attempts": [time.time() - 1500, time.time()], "escalated": False}
    assert not medic.may_attempt("j", one, {})[0]
    assert not medic.may_attempt("j", two, {})[0]


def test_a_cold_job_is_allowed_to_be_healed():
    allowed, _ = medic.may_attempt("j", {"attempts": [], "escalated": False}, {})
    assert allowed


def test_old_attempts_fall_out_of_the_window():
    entry = {"attempts": [time.time() - 30 * 3600] * 5, "escalated": False}
    assert medic.recent_attempts(entry) == []


# --------------------------------------------------------- action safety
def test_the_action_set_is_closed():
    assert set(medic.ACTIONS) == {
        "launchctl_kickstart", "docker_start", "docker_restart",
        "open_app", "run", "escalate"}


def test_no_action_can_run_an_arbitrary_command():
    for forbidden in ("shell", "exec", "eval", "command", "sh"):
        assert forbidden not in medic.ACTIONS


def test_run_refuses_a_script_outside_the_allowed_directories():
    ok, detail = medic.ACTIONS["run"]({"script": "/etc/passwd"})
    assert not ok and "outside the allowed" in detail


def test_run_refuses_a_path_traversal_escape():
    ok, detail = medic.ACTIONS["run"]({"script": "~/.fleet/../../../etc/passwd"})
    assert not ok and "refused" in detail


def test_escalate_never_reports_success():
    ok, detail = medic.ACTIONS["escalate"]({"reason": "needs a QR scan"})
    assert not ok and "needs a QR scan" in detail


# ------------------------------------------------------ shipped example
def test_the_example_registry_is_valid_and_complete():
    reg = json.load(open(os.path.join(ROOT, "registry.example.json")))
    assert reg["jobs"], "the example must contain jobs"
    for job in reg["jobs"]:
        assert job.get("name"), "every job needs a name"
        if job.get("disabled"):
            continue
        remedy = job.get("remedy")
        assert remedy, "%s has no remedy" % job["name"]
        assert remedy["action"] in medic.ACTIONS, \
            "%s uses an action outside the allowed set" % job["name"]
        if remedy["action"] == "escalate":
            assert remedy.get("reason"), "%s escalates with no reason" % job["name"]


def test_every_example_job_is_checkable_without_crashing(monkeypatch):
    monkeypatch.setattr(watch, "docker_bin", lambda: None)
    monkeypatch.setattr(watch, "http_ok", lambda *a, **k: None)
    reg = json.load(open(os.path.join(ROOT, "registry.example.json")))
    for job in reg["jobs"]:
        status, _ = watch.check(job)
        assert status in ("ok", "stale", "failed", "stalled", "unknown", "disabled")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
