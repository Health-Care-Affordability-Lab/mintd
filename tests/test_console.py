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


# ---------- slice 36: update_status + update_progress_desc -----------------


def test_reporter_update_status_updates_label():
    """Pattern C: update_status mutates the active status spinner's label."""
    reporter = Reporter(no_color=True)
    with reporter.status("A"):
        assert reporter._status_base == "A"
        reporter.update_status("B")
        assert reporter._status_base == "B"


def test_reporter_update_status_noop_in_json_mode():
    reporter = Reporter(json_mode=True)
    # status() returns a nullcontext; update_status must not blow up
    with reporter.status("A"):
        reporter.update_status("B")  # silent no-op
    assert reporter._active_status is None


def test_reporter_update_status_noop_when_no_active_status():
    reporter = Reporter(no_color=True)
    reporter.update_status("X")  # no active status; must be silent no-op
    assert reporter._active_status is None


def test_reporter_update_progress_desc_updates_description():
    """Pattern D: update_progress_desc changes the active Progress task's desc."""
    reporter = Reporter(no_color=True)
    with reporter.progress(100, desc="A") as adv:
        assert reporter._active_progress is not None
        assert reporter._progress_task_id is not None
        reporter.update_progress_desc("B")
        # Rich Progress tasks are accessible via the underlying Progress
        task = reporter._active_progress.tasks[0]
        assert task.description == "B"
        adv(10)


def test_reporter_update_progress_desc_noop_when_no_active_progress():
    reporter = Reporter(no_color=True)
    reporter.update_progress_desc("X")  # no active progress; silent no-op


def test_reporter_update_progress_desc_noop_in_json_mode():
    reporter = Reporter(json_mode=True)
    with reporter.progress(100, desc="A") as adv:
        reporter.update_progress_desc("B")  # silent no-op
        adv(10)


def test_reporter_status_resets_active_status_on_exit():
    """Regression: `with reporter.status(...)` must reset
    ``_active_status`` to None on exit. Otherwise a subsequent
    ``reporter.progress(...)`` block sees the stale reference,
    treats the already-stopped spinner as 'still active', and
    re-opens it on progress exit — leaving the old label stuck
    during downstream subprocess phases.

    Manifested in `mintd data clone` as the spinner showing
    'Cloning <name> repository...' during dvc checkout / trailing
    dvc pull even though the git-clone phase had long finished."""
    reporter = Reporter(no_color=True)
    with reporter.status("Cloning foo repository..."):
        assert reporter._active_status is not None
    # Critical: status closed → reference must be cleared.
    assert reporter._active_status is None
    assert reporter._status_base == ""
    # And progress opened after the status closes does NOT think a
    # status was active (so it won't re-open one on exit).
    with reporter.progress(100, desc="pulling") as adv:
        adv(10)
    assert reporter._active_status is None
