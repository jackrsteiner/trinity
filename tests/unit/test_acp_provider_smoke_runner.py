import importlib.util
from pathlib import Path
from types import SimpleNamespace


RUNNER = Path(__file__).parents[1] / "interop" / "run_provider_smoke.py"
SPEC = importlib.util.spec_from_file_location("acp_provider_smoke", RUNNER)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_expected_capabilities_are_json_object(monkeypatch):
    monkeypatch.setenv(
        "TRINITY_ACP_EXPECT_CAPABILITIES",
        '{"negotiated": true, "session_load": false}',
    )

    assert MODULE._expected_capabilities() == {
        "negotiated": True,
        "session_load": False,
    }


def test_permission_probe_selects_rejection_option():
    options = [
        SimpleNamespace(kind="allow_once", option_id="allow"),
        SimpleNamespace(kind="reject_once", option_id="reject"),
    ]

    assert MODULE._reject_permission("session", object(), options) == "reject"
