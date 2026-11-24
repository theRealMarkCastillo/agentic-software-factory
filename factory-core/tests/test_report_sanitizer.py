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
        "test_orders.py::TestOrders::test_refund",
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


def test_compose_project_prefixed_verifier_stream_is_extracted():
    raw = (
        "holdout-task-a-1234abcd-sut-1       | INFO boot noise\n"
        "holdout-task-a-1234abcd-verifier-1  | E   assert 500 == 200\n"
    )
    out = sanitize_output(raw)
    assert out == "E   assert 500 == 200"


def test_short_or_metadata_fixture_values_do_not_corrupt_output(tmp_path):
    (tmp_path / "meta.json").write_text(
        '{"secret_version": "1", "token_type": "Bearer", "max_tokens": "4", "auth": {"api_key": "K1"}}'
    )
    secrets = fixture_secrets(tmp_path)
    assert secrets == {"K1"}
    raw = "E   assert 1 == 200 in 0.14s\nE   Bearer rejected K1 (K10 untouched)"
    out = sanitize_output(raw, secrets=secrets)
    assert "assert 1 == 200 in 0.14s" in out
    assert "[REDACTED_SECRET] (K10 untouched)" in out


def test_holdout_conftest_frames_are_stripped():
    raw = (
        "/holdout/conftest.py:18: in auth_client\n"
        "    assert token == 'secret-sauce'\n"
        "E   AssertionError\n"
    )
    out = sanitize_output(raw)
    assert "In Behavioral Contract Check (conftest.py -> auth_client)" in out
    assert "secret-sauce" not in out


def test_cache_sut_paths_are_stripped_before_host_home():
    raw = "COPY failed: /Users/alice/.cache/factory-core/tmp/sut-0f8fad5b-d9cb-469f-a165-70867728950e/src/app.py"
    assert sanitize_output(raw) == "COPY failed: src/app.py"


def test_contract_metadata_keeps_only_verifier_identifiers():
    raw = ("sut-1 | FAILED test_ignored.py::test_untrusted\n"
           "verifier-1 | FAILED suites/test_order.py::test_send[fixture-value] - error")
    assert failed_contracts(raw) == ["test_order.py::test_send"]
    assert "fixture-value" not in sanitize_output(raw)


def test_sanitizing_does_not_create_staging_directories(tmp_path, monkeypatch):
    base = tmp_path / "no-writes"
    monkeypatch.setenv("FACTORY_TMPDIR", str(base))
    assert sanitize_output("verifier-1 | ordinary failure") == "ordinary failure"
    assert not base.exists()


def test_short_secrets_respect_underscore_boundaries_and_do_not_drop_contract_ids():
    raw = (
        "verifier-1  | E   AssertionError: test_auth failed with api token test\n"
        "verifier-1  | FAILED suites/test_api.py::test_health - AssertionError\n"
    )
    out = sanitize_output(raw, secrets={"test", "api"})
    assert "test_auth failed" in out
    assert "token [REDACTED_SECRET]" in out
    assert failed_contracts(raw, secrets={"test", "api"}) == ["test_api.py::test_health"]
