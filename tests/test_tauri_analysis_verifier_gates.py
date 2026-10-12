"""ISS-141 verdict regressions: synthetic boundaries, real entry/finalization.

No Tauri launch, Swift compilation, real process signals or model requests.
The driver/HTTP/cleanup return values below are fault-injection inputs, not
claims that a native GUI or a real model interaction has been verified.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_tauri_analysis_verifier import vta


@pytest.mark.parametrize("request_product", [False, True])
@pytest.mark.parametrize("cleanup_kind", ["clean", "alive", "error", "port", "foreign"])
def test_entry_product_unknown_and_cleanup_verdict(
    tmp_path: Path, monkeypatch, request_product: bool, cleanup_kind: str,
) -> None:
    app = tmp_path / "Fathom.app"
    (app / "Contents").mkdir(parents=True)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    resume = tmp_path / "stale-resume"
    resume.write_text("preexisting marker, no interaction evidence", encoding="utf-8")
    state = {}
    helper = str(app / vta.HELPER_RELATIVE_PATH)
    identity = {
        "bundle_id": "audit.only", "executable": "fathom-desktop",
        "fingerprint_sha256": "a" * 64,
        "source": {"commit": "a" * 40, "input_paths": ["fathom"]},
    }

    class BoundaryDriver:
        def __init__(self, *args, **kwargs):
            pass

        def build(self):
            pass

        def close(self):
            pass

        def run(self, argv, **kwargs):
            command = argv[0]
            if command == "--help":
                return {"subcommands": ["launch", "identify", "measure", "screenshot"]}
            if command == "launch":
                runtime = Path(argv[argv.index("--runtime-dir") + 1])
                state["scan"] = argv[argv.index("--scan-root") + 1]
                (runtime / "helper-instance.json").write_text(json.dumps({
                    "pid": 424242, "port": int(argv[argv.index("--port") + 1]),
                    "nonce": "synthetic-instance",
                }), encoding="utf-8")
                return {"pid": 424241, "bundle_id": "audit.only",
                        "executable": str(app / "Contents/MacOS/fathom-desktop"),
                        "activated": False}
            if command == "identify":
                return {"windows": [{"window_id": 17}]}
            if command == "measure":
                return {"chrome_height": 28}
            if command == "screenshot":
                return {"path": argv[argv.index("--output") + 1],
                        "width": 980, "height": 640}
            raise AssertionError(command)

    def boundary_http(url, **kwargs):
        if url.endswith("/api/config"):
            return 200, {"scan_root": state["scan"]}
        if url.endswith("/health"):
            return (200, {"ok": True}) if cleanup_kind == "port" else (0, {})
        raise AssertionError(f"stale resume must not fabricate product observations: {url}")

    cleanup = {
        "reclaimed": [], "signalled": [],
        "still_alive": [{"pid": 424242, "role": "helper"}] if cleanup_kind == "alive" else [],
        "errors": [{"pid": 424242, "error": "injected refusal"}] if cleanup_kind == "error" else [],
        "skipped_identity": [{"pid": 999999, "reason": "foreign"}] if cleanup_kind == "foreign" else [],
    }
    monkeypatch.setattr(vta, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(vta, "load_manifest", lambda path: {})
    monkeypatch.setattr(vta, "verify_manifest_identity", lambda *a, **kw: identity)
    monkeypatch.setattr(vta, "Driver", BoundaryDriver)
    monkeypatch.setattr(vta.tempfile, "mkdtemp", lambda **kw: str(sandbox))
    monkeypatch.setattr(vta, "wait_for_health", lambda *a, **kw: True)
    monkeypatch.setattr(vta, "assert_port_owned", lambda *a, **kw: None)
    monkeypatch.setattr(vta, "ps_identity", lambda pid: {
        "comm": "fathom-helper", "path": helper, "args": helper, "zombie": False,
    })
    monkeypatch.setattr(vta, "http_json", boundary_http)
    monkeypatch.setattr(vta, "reclaim_owned", lambda *a, **kw: cleanup)
    monkeypatch.setattr(vta, "calibrate_window_size", lambda driver, **kw: {
        "content": (kw["target_w"], kw["target_h"]),
        "frame": (kw["target_w"], kw["target_h"] + 28), "iterations": 1,
    })
    argv = ["run", "--app", str(app), "--build-ready", str(tmp_path / "manifest"),
            "--output", str(tmp_path / "out"), "--product-timeout", "1"]
    if request_product:
        argv += ["--resume-file", str(resume), "--send-sample", str(tmp_path / "absent-sample")]
    rc = vta.run_verification(vta.build_parser().parse_args(argv))
    result = json.loads((tmp_path / "out/result.json").read_text(encoding="utf-8"))
    failures = cleanup_kind in {"alive", "error", "port"}
    assert rc == (1 if failures else 0)
    assert result["verdict"] == ("FAIL" if failures else "PASS_WITH_NOT_VERIFIED")
    if request_product:
        stage = json.loads((tmp_path / "out/product-stage.json").read_text(encoding="utf-8"))
        assert stage["shots"] == stage["api_observations"] == []
        assert any("product-send-revoke-not-verified" in item for item in result["not_verified"])
    else:
        assert any("product-stage-not-requested" in item for item in result["not_verified"])
    cases = [json.loads(line) for line in (tmp_path / "out/cases.jsonl").read_text().splitlines()]
    assert any(c["name"] == "cleanup-incomplete" and c["status"] == "fail" for c in cases) == failures
