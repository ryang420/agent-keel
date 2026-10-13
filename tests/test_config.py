import pytest

from keel.config import Settings

BASE = {"KEEL_API_KEY": "secret", "KEEL_DATABASE_URL": "sqlite:///tmp/keel.sqlite3"}


def test_defaults_from_the_minimum_environment():
    settings = Settings.from_env(BASE)
    assert settings.api_key == "secret"
    assert settings.workers == 4 and settings.max_running == 10
    assert settings.lease_seconds == 30.0 and settings.aws_region == "us-east-1"
    assert settings.instance_id and not settings.is_postgres


def test_overrides_and_postgres_detection():
    settings = Settings.from_env({**BASE, "KEEL_DATABASE_URL": "postgresql://u:" + "p@db/keel",
                                  "KEEL_WORKERS": "2", "KEEL_MAX_RUNNING": "6",
                                  "KEEL_LEASE_SECONDS": "12", "AWS_REGION": "us-west-2"})
    assert (settings.workers, settings.max_running, settings.lease_seconds) == (2, 6, 12.0)
    assert settings.aws_region == "us-west-2" and settings.is_postgres


@pytest.mark.parametrize(("env", "message"), [
    ({"KEEL_DATABASE_URL": "sqlite:///x"}, "KEEL_API_KEY"),
    ({"KEEL_API_KEY": "k"}, "KEEL_DATABASE_URL"),
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
    base = {"KEEL_API_KEY": "k", "KEEL_DATABASE_URL": "sqlite:///x.db"}
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
