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
