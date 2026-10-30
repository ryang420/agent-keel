import pytest

from keel.config import ServiceCredential, Settings

BASE = {"KEEL_API_KEY": "secret", "KEEL_API_KEY_TENANT": "1",
        "KEEL_DATABASE_URL": "postgresql://keel@localhost/keel_test"}


def test_defaults_from_the_minimum_environment():
    settings = Settings.from_env(BASE)
    assert [(c.tenant_id, c.secret) for c in settings.credentials] == [(1, "secret")]
    assert settings.tenant_backfill == {}
    assert "secret" not in repr(settings)
    assert settings.workers == 4 and settings.max_running == 10
    assert settings.lease_seconds == 30.0 and settings.aws_region == "us-east-1"
    assert settings.instance_id


def test_overrides():
    settings = Settings.from_env({**BASE, "KEEL_DATABASE_URL": "postgresql://u:" + "p@db/keel",
                                  "KEEL_WORKERS": "2", "KEEL_MAX_RUNNING": "6",
                                  "KEEL_LEASE_SECONDS": "12", "AWS_REGION": "us-west-2"})
    assert (settings.workers, settings.max_running, settings.lease_seconds) == (2, 6, 12.0)
    assert settings.aws_region == "us-west-2"


@pytest.mark.parametrize(("env", "message"), [
    ({"KEEL_DATABASE_URL": "postgresql://keel@localhost/keel_test"}, "KEEL_API_KEY"),
    ({"KEEL_API_KEY": "k", "KEEL_API_KEY_TENANT": "1"}, "KEEL_DATABASE_URL"),
    ({**BASE, "KEEL_DATABASE_URL": "mysql://x"}, "KEEL_DATABASE_URL"),
    ({**BASE, "KEEL_WORKERS": "0"}, "KEEL_WORKERS"),
    ({**BASE, "KEEL_MAX_RUNNING": "many"}, "KEEL_MAX_RUNNING"),
    ({**BASE, "KEEL_LEASE_SECONDS": "abc"}, "KEEL_LEASE_SECONDS"),
    ({**BASE, "KEEL_LEASE_SECONDS": "0"}, "KEEL_LEASE_SECONDS"),
    ({**BASE, "KEEL_LEASE_SECONDS": "nan"}, "KEEL_LEASE_SECONDS"),
])
def test_bad_settings_name_the_variable(env, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env)


def test_sandbox_settings_have_defaults_and_validation():
    base = {"KEEL_API_KEY": "k", "KEEL_API_KEY_TENANT": "1", "KEEL_DATABASE_URL": "postgresql://keel@localhost/keel_test"}
    settings = Settings.from_env(base)
    assert (settings.max_iterations, settings.sandbox, settings.sandbox_idle_seconds, settings.sandbox_mount_root,
            settings.docker, settings.deployment) == (60, "docker", 1800.0, None, "docker", "keel")
    custom = Settings.from_env({**base, "KEEL_MAX_ITERATIONS": "5", "KEEL_SANDBOX": "none",
                                "KEEL_SANDBOX_MOUNT_ROOT": "/srv/data", "KEEL_DEPLOYMENT": "eval-1"})
    assert (custom.max_iterations, custom.sandbox, custom.sandbox_mount_root, custom.deployment) == (
        5, "none", "/srv/data", "eval-1")
    for env, name in (({"KEEL_SANDBOX": "vm"}, "KEEL_SANDBOX"), ({"KEEL_SANDBOX_MOUNT_ROOT": "rel"}, "MOUNT_ROOT"),
                      ({"KEEL_DEPLOYMENT": "Bad Name"}, "KEEL_DEPLOYMENT")):
        with pytest.raises(ValueError, match=name):
            Settings.from_env({**base, **env})


def test_mount_root_is_normalised_and_blank_values_fall_back():
    assert Settings.from_env({**BASE, "KEEL_SANDBOX_MOUNT_ROOT": "/srv//data/../data/"}).sandbox_mount_root == "/srv/data"
    blank = Settings.from_env({**BASE, "KEEL_SANDBOX_MOUNT_ROOT": "  ", "KEEL_DOCKER": "  "})
    assert blank.sandbox_mount_root is None and blank.docker == "docker"


def test_the_openai_key_comes_from_either_variable_and_stays_out_of_repr():
    assert Settings.from_env(BASE).openai_api_key is None
    assert Settings.from_env(BASE).openai_base_url == "https://api.openai.com/v1"
    assert Settings.from_env({**BASE, "OPENAI_API_KEY": "sk-a"}).openai_api_key == "sk-a"
    both = Settings.from_env({**BASE, "OPENAI_API_KEY": "sk-a", "KEEL_OPENAI_API_KEY": "sk-b",
                              "KEEL_OPENAI_BASE_URL": "http://llm:8000/v1"})
    assert both.openai_api_key == "sk-b" and both.openai_base_url == "http://llm:8000/v1"
    assert "sk-b" not in repr(both)


def test_openai_compat_window_and_base_url_validation():
    d = Settings.from_env(BASE)
    assert d.openai_compat is False and d.openai_context_window == 128_000
    s = Settings.from_env({**BASE, "KEEL_OPENAI_COMPAT": "1", "KEEL_OPENAI_CONTEXT_WINDOW": "32000"})
    assert s.openai_compat is True and s.openai_context_window == 32000
    with pytest.raises(ValueError, match="KEEL_OPENAI_BASE_URL"):
        Settings.from_env({**BASE, "KEEL_OPENAI_BASE_URL": "llm:8000/v1"})
    with pytest.raises(ValueError, match="KEEL_OPENAI_CONTEXT_WINDOW"):
        Settings.from_env({**BASE, "KEEL_OPENAI_CONTEXT_WINDOW": "0"})


def test_the_compression_threshold_is_a_fraction_of_the_window():
    assert Settings.from_env(BASE).compression_threshold == 0.75
    assert Settings.from_env({**BASE, "KEEL_COMPRESSION_THRESHOLD": "0.5"}).compression_threshold == 0.5
    for bad in ("0", "1.5", "half", "nan"):
        with pytest.raises(ValueError, match="KEEL_COMPRESSION_THRESHOLD"):
            Settings.from_env({**BASE, "KEEL_COMPRESSION_THRESHOLD": bad})


@pytest.mark.parametrize(("lease", "timeout"), [("30", 10.0), ("9", 5.0), ("90", 30.0)])
def test_the_database_command_timeout_is_a_third_of_the_lease(lease, timeout):
    assert Settings.from_env({**BASE, "KEEL_LEASE_SECONDS": lease}).db_command_timeout == timeout


def test_event_retention_defaults_to_seven_days_and_must_be_positive():
    assert Settings.from_env(BASE).event_retention_days == 7
    assert Settings.from_env({**BASE, "KEEL_EVENT_RETENTION_DAYS": "30"}).event_retention_days == 30
    with pytest.raises(ValueError, match="KEEL_EVENT_RETENTION_DAYS"):
        Settings.from_env({**BASE, "KEEL_EVENT_RETENTION_DAYS": "0"})


def test_background_limit_defaults_and_rejects_zero():
    assert Settings.from_env(BASE).background_limit == 4
    with pytest.raises(ValueError, match="KEEL_BACKGROUND_LIMIT"):
        Settings.from_env({**BASE, "KEEL_BACKGROUND_LIMIT": "0"})


def test_a_key_without_a_tenant_is_refused_and_two_keys_can_share_a_tenant(tmp_path):
    with pytest.raises(ValueError, match="KEEL_API_KEY_TENANT"):
        Settings.from_env({**BASE, "KEEL_API_KEY_TENANT": ""})
    with pytest.raises(ValueError, match="KEEL_API_KEY"):
        Settings.from_env({"KEEL_API_KEY_TENANT": "1", "KEEL_DATABASE_URL": BASE["KEEL_DATABASE_URL"]})
    with pytest.raises(ValueError, match="KEEL_API_KEY or KEEL_API_KEYS"):
        Settings.from_env({"KEEL_DATABASE_URL": BASE["KEEL_DATABASE_URL"]})
    path = tmp_path / "tenants.json"
    path.write_text('{"p1": 1, "p2": 2}')
    settings = Settings.from_env({
        "KEEL_DATABASE_URL": BASE["KEEL_DATABASE_URL"],
        "KEEL_API_KEYS": "1:key-a,2:key-b",
        "KEEL_API_KEY": "key-legacy", "KEEL_API_KEY_TENANT": "1",
        "KEEL_TENANT_BACKFILL": str(path),
    })
    assert [(c.tenant_id, c.secret) for c in settings.credentials] == [
        (1, "key-a"), (2, "key-b"), (1, "key-legacy")]
    assert settings.tenant_backfill == {"p1": 1, "p2": 2}
    assert "key-a" not in repr(settings) and "key-legacy" not in repr(settings)
    for raw, message in (
        ("1:key-a,2:key-a", "more than one tenant"),
        ("no-colon", "tenant:secret"),
        (":key", "tenant id"),
        ("bad id:key", "tenant id"),
        ("1:", "tenant:secret"),
        ("1:has space", "tenant:secret"),
    ):
        with pytest.raises(ValueError, match=message):
            Settings.from_env({"KEEL_DATABASE_URL": BASE["KEEL_DATABASE_URL"], "KEEL_API_KEYS": raw})
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="KEEL_TENANT_BACKFILL"):
        Settings.from_env({**BASE, "KEEL_TENANT_BACKFILL": str(missing)})
    path.write_text("[1]")
    with pytest.raises(ValueError, match="KEEL_TENANT_BACKFILL"):
        Settings.from_env({**BASE, "KEEL_TENANT_BACKFILL": str(path)})
    for invalid in ('{"p1": "1"}', '{"p1": 2147483648}', '{"p1": -2147483649}', '{"p1": true}'):
        path.write_text(invalid)
        with pytest.raises(ValueError, match="KEEL_TENANT_BACKFILL"):
            Settings.from_env({**BASE, "KEEL_TENANT_BACKFILL": str(path)})
    path.write_text('{"p1": 0, "p2": -2147483648}')
    settings = Settings.from_env({"KEEL_DATABASE_URL": BASE["KEEL_DATABASE_URL"],
                                  "KEEL_API_KEYS": "0:key-zero,-2147483648:key-min,2147483647:key-max",
                                  "KEEL_TENANT_BACKFILL": str(path)})
    assert [c.tenant_id for c in settings.credentials] == [0, -2_147_483_648, 2_147_483_647]
    assert settings.tenant_backfill == {"p1": 0, "p2": -2_147_483_648}
    single = Settings.from_env({**BASE, "KEEL_API_KEY_TENANT": "-7"})
    assert [c.tenant_id for c in single.credentials] == [-7]
    for invalid in ("2147483648:key", "-2147483649:key", "01:key", "-0:key", "-01:key", "+1:key", "- 1:key"):
        with pytest.raises(ValueError, match="tenant id"):
            Settings.from_env({"KEEL_DATABASE_URL": BASE["KEEL_DATABASE_URL"], "KEEL_API_KEYS": invalid})


def test_a_direct_service_credential_requires_an_integer_tenant_id():
    for valid in (1, 0, -1, 2_147_483_647, -2_147_483_648):
        assert ServiceCredential(valid, "key").tenant_id == valid
    for invalid in ("1", True, 2_147_483_648, -2_147_483_649):
        with pytest.raises(ValueError, match="tenant id"):
            ServiceCredential(invalid, "key")


def test_memory_limits_default_and_must_be_positive():
    settings = Settings.from_env(BASE)
    assert (settings.memory_review_turns, settings.memory_chars, settings.user_chars) == (10, 2200, 1375)
    assert (settings.memory_review_idle_seconds, settings.memory_review_max_age_seconds,
            settings.memory_review_workers) == (60.0, 600.0, 1)
    custom = Settings.from_env({**BASE, "KEEL_MEMORY_REVIEW_TURNS": "3", "KEEL_MEMORY_CHARS": "100",
                                "KEEL_USER_CHARS": "80",
                                "KEEL_MEMORY_REVIEW_IDLE_SECONDS": "5", "KEEL_MEMORY_REVIEW_MAX_AGE_SECONDS": "50",
                                "KEEL_MEMORY_REVIEW_WORKERS": "2"})
    assert (custom.memory_review_turns, custom.memory_chars, custom.user_chars) == (3, 100, 80)
    assert (custom.memory_review_idle_seconds, custom.memory_review_max_age_seconds,
            custom.memory_review_workers) == (5.0, 50.0, 2)
    with pytest.raises(ValueError, match="KEEL_USER_CHARS"):
        Settings.from_env({**BASE, "KEEL_USER_CHARS": "0"})


def test_model_stall_limits_have_defaults_and_must_be_positive():
    from keel.providers.base import StallLimits

    assert Settings.from_env(BASE).stall_limits == StallLimits(first_event=120.0, between_events=60.0,
                                                                high_effort=300.0)
    custom = Settings.from_env({**BASE, "KEEL_MODEL_FIRST_EVENT_SECONDS": "30", "KEEL_MODEL_STALL_SECONDS": "45",
                                "KEEL_MODEL_HIGH_EFFORT_STALL_SECONDS": "900"})
    assert custom.stall_limits == StallLimits(first_event=30.0, between_events=45.0, high_effort=900.0)
    for name in ("KEEL_MODEL_FIRST_EVENT_SECONDS", "KEEL_MODEL_STALL_SECONDS", "KEEL_MODEL_HIGH_EFFORT_STALL_SECONDS"):
        with pytest.raises(ValueError, match=name):
            Settings.from_env({**BASE, name: "0"})


def test_sandbox_reap_timeout_has_finite_positive_default_and_override():
    assert Settings.from_env(BASE).sandbox_reap_seconds == 5.0
    assert Settings.from_env({**BASE, "KEEL_SANDBOX_REAP_SECONDS": "8.5"}).sandbox_reap_seconds == 8.5
    assert Settings.from_env({**BASE, "KEEL_SANDBOX_REAP_SECONDS": "86400"}).sandbox_reap_seconds == 86400.0
    for bad in ("0", "-1", "nan", "inf", "86400.5", "1e300"):
        with pytest.raises(ValueError, match="KEEL_SANDBOX_REAP_SECONDS"):
            Settings.from_env({**BASE, "KEEL_SANDBOX_REAP_SECONDS": bad})


@pytest.mark.parametrize("url", ["sqlite:///keel.db", "sqlite:///:memory:"])
def test_sqlite_database_urls_are_rejected(url):
    with pytest.raises(ValueError, match="KEEL_DATABASE_URL"):
        Settings.from_env({**BASE, "KEEL_DATABASE_URL": url})
