"""The settings file that sits in front of the environment variables."""

import json
import os
import stat

import pytest

import settings_store


@pytest.fixture
def store(tmp_path, monkeypatch):
  """A settings file of our own, and an empty environment for the keys."""
  path = tmp_path / "settings.json"
  monkeypatch.setenv(settings_store.SETTINGS_FILE_ENV, str(path))
  monkeypatch.setattr(settings_store, "_cache", {"key": None, "values": {}})
  for key in settings_store.MANAGED_KEYS:
    monkeypatch.delenv(key, raising=False)
  return path


def test_no_file_means_no_settings(store):
  assert settings_store.load_settings() == {}


def test_an_empty_override_switches_the_file_off(monkeypatch):
  monkeypatch.setenv(settings_store.SETTINGS_FILE_ENV, "")
  assert settings_store.settings_path() is None
  assert settings_store.load_settings() == {}


def test_a_broken_file_is_ignored_rather_than_raised(store):
  store.write_text("{ this is not json", encoding="utf-8")
  assert settings_store.load_settings() == {}


def test_a_file_that_is_not_an_object_is_ignored(store):
  store.write_text('["nope"]', encoding="utf-8")
  assert settings_store.load_settings() == {}


def test_saving_and_reading_back(store):
  settings_store.save_settings({"PRINTER_ID": "00M09A371101058", "PRINTER_NAME": "X1C"})
  assert settings_store.load_settings() == {"PRINTER_ID": "00M09A371101058", "PRINTER_NAME": "X1C"}


def test_saving_drops_empty_values_so_the_environment_can_show_through(store):
  """A blank field means "use the environment variable", not "store nothing"."""
  settings_store.save_settings({"PRINTER_ID": "00M09A371101058", "PRINTER_NAME": ""})
  assert "PRINTER_NAME" not in settings_store.load_settings()


def test_the_file_is_not_world_readable(store):
  """It holds the printer access code."""
  path = settings_store.save_settings({"PRINTER_ACCESS_CODE": "12345678"})
  assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_leftovers_from_the_atomic_write(store):
  settings_store.save_settings({"PRINTER_ID": "AAAAAAAA"})
  assert [p.name for p in store.parent.iterdir()] == ["settings.json"]


def test_an_edit_from_outside_is_picked_up(store):
  settings_store.save_settings({"PRINTER_NAME": "old"})
  assert settings_store.load_settings()["PRINTER_NAME"] == "old"

  store.write_text(json.dumps({"PRINTER_NAME": "new"}), encoding="utf-8")
  os.utime(store, (0, 0))   # a different mtime is all the cache keys on
  assert settings_store.load_settings()["PRINTER_NAME"] == "new"


@pytest.mark.parametrize("key,raw,expected", [
  ("PRINTER_ID", " 00m09a371101058 ", "00M09A371101058"),
  ("SPOOLMAN_BASE_URL", "http://host:7912/", "http://host:7912"),
  ("SPOOLMAN_BASE_URL", "http://host:7912///", "http://host:7912"),
  ("PRINTER_NAME", "  X1 Carbon  ", "X1 Carbon"),
  ("PRINTER_IP", None, ""),
])
def test_normalize(key, raw, expected):
  assert settings_store.normalize(key, raw) == expected


@pytest.mark.parametrize("values,bad", [
  ({"PRINTER_ID": "00M09A371101058"}, []),
  ({"PRINTER_ID": "short"}, ["PRINTER_ID"]),
  ({"PRINTER_IP": "192.168.4.100"}, []),
  ({"PRINTER_IP": "printer.local"}, []),
  ({"PRINTER_IP": "not a host"}, ["PRINTER_IP"]),
  ({"SPOOLMAN_BASE_URL": "http://192.168.4.100:7912"}, []),
  ({"SPOOLMAN_BASE_URL": "192.168.4.100:7912"}, ["SPOOLMAN_BASE_URL"]),
  ({"SPOOLMAN_BASE_URL": "ftp://host"}, ["SPOOLMAN_BASE_URL"]),
  ({"PRINTER_NAME": "x" * 65}, ["PRINTER_NAME"]),
  ({}, []),
])
def test_validate(values, bad):
  assert sorted(settings_store.validate(values)) == sorted(bad)


def test_redact_hides_the_access_code_and_nothing_else():
  out = settings_store.redact({"PRINTER_ACCESS_CODE": "12345678", "PRINTER_NAME": "X1C"})
  assert out == {"PRINTER_ACCESS_CODE": "***", "PRINTER_NAME": "X1C"}


def test_redact_does_not_invent_a_secret_where_there_is_none():
  assert settings_store.redact({"PRINTER_ACCESS_CODE": ""})["PRINTER_ACCESS_CODE"] == ""


def test_source_of_prefers_the_file(store, monkeypatch):
  monkeypatch.setenv("PRINTER_NAME", "from env")
  assert settings_store.source_of("PRINTER_NAME") == "environment"

  settings_store.save_settings({"PRINTER_NAME": "from file"})
  assert settings_store.source_of("PRINTER_NAME") == "file"


def test_source_of_reports_nothing_set(store):
  assert settings_store.source_of("PRINTER_NAME") == "unset"


def test_nothing_pending_when_the_file_agrees_with_the_process(store):
  settings_store.save_settings({"PRINTER_NAME": "X1C"})
  assert settings_store.pending_changes({"PRINTER_NAME": "X1C"}) == []


def test_a_changed_value_is_pending(store):
  settings_store.save_settings({"PRINTER_NAME": "new"})
  assert settings_store.pending_changes({"PRINTER_NAME": "old"}) == ["PRINTER_NAME"]
  assert settings_store.restart_required({"PRINTER_NAME": "old"})


def test_a_lowercase_serial_in_the_file_is_not_a_pending_change(store):
  """config.py upper-cases it on the way in; comparing raw would never clear."""
  settings_store.save_settings({"PRINTER_ID": "00m09a371101058"})
  assert settings_store.pending_changes({"PRINTER_ID": "00M09A371101058"}) == []


def test_a_key_only_in_the_environment_is_not_pending(store):
  """Nothing was saved for it, so there is nothing a restart would change."""
  assert settings_store.pending_changes({"PRINTER_NAME": "from env"}) == []


def _config_in_a_fresh_process(env):
  """What config.py resolves to, read from an interpreter of its own.

  config is imported once per process and every consumer takes its values by
  value, so precedence can only honestly be checked from a clean start.
  """
  import subprocess
  import sys

  root = os.path.dirname(os.path.abspath(settings_store.__file__))

  full = dict(os.environ)
  for key in settings_store.MANAGED_KEYS:
    full.pop(key, None)
  full.update(env)

  result = subprocess.run(
      [sys.executable, "-c",
       "import config, json; "
       "print(json.dumps({k: getattr(config, a) for k, a in "
       "(('PRINTER_ID','PRINTER_ID'),('PRINTER_NAME','PRINTER_NAME'),"
       "('SPOOLMAN_BASE_URL','SPOOLMAN_BASE_URL'))}))"],
      cwd=root, env=full, capture_output=True, text=True, timeout=30)

  assert result.returncode == 0, result.stderr
  return json.loads(result.stdout.strip().splitlines()[-1])


def test_config_takes_the_file_over_the_environment(tmp_path):
  path = tmp_path / "settings.json"
  path.write_text(json.dumps({"PRINTER_NAME": "from file"}), encoding="utf-8")

  values = _config_in_a_fresh_process({
      settings_store.SETTINGS_FILE_ENV: str(path),
      "PRINTER_NAME": "from environment",
  })
  assert values["PRINTER_NAME"] == "from file"


def test_config_falls_back_to_the_environment(tmp_path):
  path = tmp_path / "settings.json"
  path.write_text(json.dumps({"PRINTER_ID": "AAAAAAAA"}), encoding="utf-8")

  values = _config_in_a_fresh_process({
      settings_store.SETTINGS_FILE_ENV: str(path),
      "PRINTER_NAME": "from environment",
  })
  assert values["PRINTER_NAME"] == "from environment"


def test_config_still_upper_cases_the_serial_from_the_file(tmp_path):
  path = tmp_path / "settings.json"
  path.write_text(json.dumps({"PRINTER_ID": "00m09a371101058"}), encoding="utf-8")

  values = _config_in_a_fresh_process({settings_store.SETTINGS_FILE_ENV: str(path)})
  assert values["PRINTER_ID"] == "00M09A371101058"


def test_config_works_with_no_settings_file_at_all(tmp_path):
  """The default install: nothing saved, everything from the environment."""
  values = _config_in_a_fresh_process({
      settings_store.SETTINGS_FILE_ENV: "",
      "SPOOLMAN_BASE_URL": "http://192.168.4.100:7912",
  })
  assert values["SPOOLMAN_BASE_URL"] == "http://192.168.4.100:7912"
