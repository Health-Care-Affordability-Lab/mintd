"""Helpers to invoke mintd's bundled dvc."""

import os
import sys


def dvc_cmd() -> list[str]:
    """Return the subprocess argv prefix for invoking mintd's bundled dvc.
    Uses ``sys.executable -m dvc`` so the dvc that runs is the one
    installed in mintd's own Python env (per pyproject), not whatever
    happens to be first on PATH. See SLICE-40."""
    return [sys.executable, "-m", "dvc"]


def dvc_env() -> dict[str, str]:
    """Return the subprocess env for invoking dvc: the parent env plus
    ``DVC_NO_ANALYTICS``.

    dvc ships telemetry on by default. Left alone it writes a persistent
    machine id under ``$HOME`` and POSTs a report from a detached daemon on
    every invocation -- so `mintd init`, which spawns dvc five times, phones
    a third party five times. Under CI the report carries the org name
    (``$GITHUB_SERVER_URL/$(dirname $GITHUB_REPOSITORY)``) and the acting
    account rather than an anonymous id.

    No dataset content is involved, but mintd scaffolds enclave and lab-only
    projects and runs where an unannounced outbound request on project
    creation is a governance question, so opting out is the defensible default.

    It is an unconditional opt-out, and deliberately so. dvc's
    ``analytics.is_enabled`` reads ``enabled = not os.getenv(DVC_NO_ANALYTICS)``
    and only consults ``core.analytics`` when that is still true, so the
    variable wins over any user config and mintd offers no way to switch
    telemetry back on for a mintd-spawned dvc. A ``setdefault`` here would not
    change that — dvc treats *any* non-empty value, including ``"0"``, as
    "disabled" — so the honest statement is that this is not user-overridable
    rather than a knob that looks like one. Contrast ``_dvc_ops._env()``, which
    does use ``setdefault`` for ``AWS_PROFILE``: that one genuinely defers to an
    exported value.

    Every dvc spawn in mintd routes through here -- ``_dvc_ops``,
    ``_init_ops`` and ``_fast_sync_ops``'s version probe -- so the opt-out
    cannot be missed by adding a call site.
    """
    env = dict(os.environ)
    env["DVC_NO_ANALYTICS"] = "1"
    return env
