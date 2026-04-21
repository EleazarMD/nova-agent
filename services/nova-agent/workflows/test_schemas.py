"""Unit tests for nova.notifyUser / nova.askUser input validators.

Runs without a Hatchet connection — stubs hatchet_sdk before import.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types


def _load_worker():
    # Stub hatchet_sdk so Hatchet() doesn't need a server or token.
    class _FakeHatchet:
        def __init__(self, *a, **k): pass
        def workflow(self, *a, **k):
            class _W:
                name = k.get("name", "fake")
                def task(self, *a, **k):
                    return lambda fn: fn
            return _W()
        def worker(self, *a, **k):
            class _Wk:
                def start(self): pass
            return _Wk()

    stub = types.ModuleType("hatchet_sdk")
    stub.Hatchet = _FakeHatchet
    stub.Context = object
    sys.modules["hatchet_sdk"] = stub

    os.environ.setdefault("HATCHET_CLIENT_TOKEN", "stub")

    spec = importlib.util.spec_from_file_location(
        "nova_worker_under_test",
        os.path.join(os.path.dirname(__file__), "worker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_notify_input_rejects_bad_priority():
    m = _load_worker()
    try:
        m.NotifyUserInput(user_id="u", title="t", body="b", priority="medium")
    except Exception:
        return
    raise AssertionError("should reject priority=medium")


def test_notify_input_accepts_valid():
    m = _load_worker()
    inp = m.NotifyUserInput(
        user_id="eleazar", title="New email", body="from boss",
        priority="high", metadata={"email_id": "abc"},
    )
    assert inp.priority == "high"
    assert inp.metadata["email_id"] == "abc"


def test_ask_input_defaults():
    m = _load_worker()
    inp = m.AskUserInput(user_id="u", question="Reschedule?")
    assert inp.expiry_minutes == 10.0
    assert inp.priority == "high"
    assert inp.options == []
    assert inp.context == ""


def test_ask_input_expiry_bounds():
    m = _load_worker()
    try:
        m.AskUserInput(user_id="u", question="q", expiry_minutes=0.1)
    except Exception:
        pass
    else:
        raise AssertionError("should reject expiry < 0.5min")
    try:
        m.AskUserInput(user_id="u", question="q", expiry_minutes=120)
    except Exception:
        pass
    else:
        raise AssertionError("should reject expiry > 60min")


def test_ask_input_options_list():
    m = _load_worker()
    inp = m.AskUserInput(
        user_id="u", question="Pick:", options=["now", "later", "never"],
        expiry_minutes=5,
    )
    assert inp.options == ["now", "later", "never"]


if __name__ == "__main__":
    import traceback
    tests = [(n, f) for n, f in globals().items()
             if n.startswith("test_") and callable(f)]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception:
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"{passed}/{len(tests)} passed")
    sys.exit(0 if passed == len(tests) else 1)
