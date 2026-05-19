import logging
from mintd._console import Reporter

def test_reporter_json_suppression(capsys):
    reporter = Reporter(json_mode=True)
    reporter.info("should not show")
    reporter.status("should not show")
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""

def test_reporter_error_format(capsys):
    reporter = Reporter(no_color=True)
    reporter.error("bad thing", hint="try this")
    _, err = capsys.readouterr()
    assert "error: bad thing" in err
    assert "hint: try this" in err

def test_reporter_result_json(capsys):
    reporter = Reporter(json_mode=True)
    reporter.result({"a": 1})
    out, _ = capsys.readouterr()
    assert out == '{"a":1}\n'

def test_reporter_log_bridge(capsys):
    reporter = Reporter()
    reporter.install_log_bridge()
    lg = logging.getLogger("mintd")
    lg.info("log message")
    _, err = capsys.readouterr()
    assert "log message" in err
    reporter.uninstall_log_bridge()


# ---------- slice 26: Reporter.progress widget --------------------------


def test_progress_yields_advance_callable(capsys):
    """Happy path: the context manager yields a callable that accepts
    integer byte counts without error."""
    reporter = Reporter(no_color=True)
    with reporter.progress(100, desc="x") as adv:
        adv(50)
        adv(50)
    # No assertion on output bytes — rich renders to stderr but the
    # transient bar erases on exit; capsys may or may not catch frames.


def test_progress_suppressed_in_json_mode(capsys):
    """``--json`` mode: no rendering, yields a no-op callable."""
    reporter = Reporter(json_mode=True)
    with reporter.progress(100, desc="x") as adv:
        adv(50)
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""


def test_progress_suppressed_when_total_zero(capsys):
    """``total=0`` (empty/all-cached repo): no rendering, yields no-op."""
    reporter = Reporter(no_color=True)
    with reporter.progress(0, desc="x") as adv:
        adv(0)
    _, err = capsys.readouterr()
    assert err == ""


def test_progress_suspends_and_resumes_active_status():
    """Opening progress inside an active status block suspends the status
    for the duration; resumes on exit with the same base text."""
    reporter = Reporter(no_color=True)
    # Open a status (slice 25 spinner machinery)
    with reporter.status("Cloning foo..."):
        before_status = reporter._active_status
        assert before_status is not None
        # Now enter progress — should suspend status
        with reporter.progress(100, desc="pulling") as adv:
            assert reporter._active_status is None
            adv(50)
        # After progress exits, status should be resumed (new Status instance)
        assert reporter._active_status is not None
        assert reporter._status_base == "Cloning foo..."
