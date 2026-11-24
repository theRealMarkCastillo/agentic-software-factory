from factory_core.report_sanitizer import failed_contracts, fixture_secrets, sanitize_output

RAW = """\
sut-1       | INFO server listening on 8080 from /tmp/sut-0f8fad5b-d9cb-469f-a165-70867728950e/src/app.py
verifier-1  | F                                                                        [100%]
verifier-1  | =================================== FAILURES ===================================
verifier-1  | _______________________ test_health_endpoint_reports_ok ________________________
verifier-1  | /holdout/suites/test_contract_example.py:34: in test_health_endpoint_reports_ok
verifier-1  |     assert response.status_code == expected["status_code"]
verifier-1  | E   assert 500 == 200
verifier-1  | E    +  where 500 = <Response [500 Internal Server Error]>.status_code
verifier-1  | =========================== short test summary info ============================
verifier-1  | FAILED ../holdout/suites/test_contract_example.py::test_health_endpoint_reports_ok - assert 500 == 200
verifier-1  | 1 failed in 0.21s
verifier-1 exited with code 1
"""


def test_keeps_the_behavioral_failure():
    out = sanitize_output(RAW)
    assert "E   assert 500 == 200" in out
    assert "1 failed" in out


def test_replaces_suite_frame_with_contract_label():
    out = sanitize_output(RAW)
    assert "In Behavioral Contract Check (test_contract_example.py -> test_health_endpoint_reports_ok)" in out
    assert ":34" not in out


def test_drops_sealed_test_source_lines():
    out = sanitize_output(RAW)
    assert 'expected["status_code"]' not in out
    assert "response.status_code ==" not in out


def test_drops_other_services_and_sut_temp_paths():
    out = sanitize_output(RAW)
    assert "server listening" not in out
    assert "/tmp/sut-" not in out


def test_strips_holdout_host_paths():
    raw = "Error reading /Users/alice/.factory-holdouts/my-repo/suites/test_x.py: denied\nbind /Users/alice/.factory-holdouts/my-repo failed"
    out = sanitize_output(raw)
    assert ".factory-holdouts" not in out
    assert "alice" not in out
    assert out.count("[SEALED_HOLDOUT_FILE]") == 2


def test_strips_sut_and_private_tmp_paths():
    raw = "COPY failed: /private/tmp/sut-0f8fad5b-d9cb-469f-a165-70867728950e/src/app.py not found"
    assert sanitize_output(raw) == "COPY failed: src/app.py not found"


def test_strips_container_fixture_paths_and_home_dirs():
    raw = "open('/holdout/fixtures/golden_example.json') failed\nsee /home/bob/project/log.txt\nfixtures/golden.json mismatch"
    out = sanitize_output(raw)
    assert "golden" not in out
    assert "bob" not in out and "[HOST_HOME]/project/log.txt" in out


def test_python_traceback_frames_are_relabelled():
    raw = 'Traceback (most recent call last):\n  File "/holdout/suites/test_api.py", line 12, in test_login\nAssertionError'
    out = sanitize_output(raw)
    assert "In Behavioral Contract Check (test_api.py -> test_login)" in out
    assert "line 12" not in out


def test_redacts_secret_assignments():
    raw = "E   assert headers == {'api_key': 'sk-live-123', \"password\": \"hunter2\"}\nBEARER_TOKEN = 'abc.def.ghi'\nx_secret=\"zzz\""
    out = sanitize_output(raw)
    for leaked in ("sk-live-123", "hunter2", "abc.def.ghi", "zzz"):
        assert leaked not in out
    assert out.count("[REDACTED_SECRET]") == 4


def test_redacts_authorization_header_values():
    out = sanitize_output("E   Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig was rejected")
    assert "eyJhbGci" not in out and "Bearer [REDACTED_SECRET]" in out


def test_redacts_literal_fixture_secret_values_anywhere():
    raw = "E   AssertionError: expected header value example-holdout-key-replace-me in echo"
    out = sanitize_output(raw, secrets={"example-holdout-key-replace-me"})
    assert "example-holdout-key" not in out and "[REDACTED_SECRET]" in out


def test_fixture_secrets_reads_secret_named_keys_only(tmp_path):
    (tmp_path / "a.json").write_text('{"auth": {"api_key": "K1", "nested": [{"password": "P1"}]}, "status": "ok", "token": "T1"}')
    (tmp_path / "broken.json").write_text("{not json")
    assert fixture_secrets(tmp_path) == {"K1", "P1", "T1"}


def test_failed_contracts_extracts_ids_in_order():
    raw = RAW + "verifier-1  | FAILED suites/test_orders.py::TestOrders::test_refund[case-1] - boom\n" + \
        "verifier-1  | FAILED ../holdout/suites/test_contract_example.py::test_health_endpoint_reports_ok - again\n"
    assert failed_contracts(raw) == [
        "test_contract_example.py::test_health_endpoint_reports_ok",
        "test_orders.py::TestOrders::test_refund[case-1]",
    ]


def test_failed_contracts_empty_for_build_failure():
    assert failed_contracts("failed to solve: process did not complete successfully") == []


def test_build_failure_without_verifier_output_is_still_reported():
    raw = "#8 ERROR: process \"/bin/sh -c pip install nothing-real\" did not complete successfully: exit code: 1\nfailed to solve"
    out = sanitize_output(raw)
    assert "did not complete successfully" in out


def test_output_is_bounded():
    raw = "\n".join(f"verifier-1  | E   line {i}" for i in range(500))
    out = sanitize_output(raw).splitlines()
    assert len(out) <= 61
    assert out[0].startswith("[... earlier output truncated")
    assert out[-1] == "E   line 499"


def test_strips_ansi_sequences():
    assert sanitize_output("\x1b[31mE   assert 1 == 2\x1b[0m") == "E   assert 1 == 2"
