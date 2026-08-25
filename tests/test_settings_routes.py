"""The settings page: the first form and the first POST route in this app."""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("OPENSPOOLMAN_TEST_DATA") != "1",
    reason="needs OPENSPOOLMAN_TEST_DATA=1, as the CI workflow sets",
)

import app as app_module  # noqa: E402
import connection_probe  # noqa: E402
import settings_store  # noqa: E402
import spoolman_client  # noqa: E402

ACCESS_CODE = "13572468"
SERIAL = "00M09A371101058"


@pytest.fixture
def store(tmp_path, monkeypatch):
  path = tmp_path / "settings.json"
  monkeypatch.setenv(settings_store.SETTINGS_FILE_ENV, str(path))
  monkeypatch.setattr(settings_store, "_cache", {"key": None, "values": {}})
  for key in settings_store.MANAGED_KEYS:
    monkeypatch.delenv(key, raising=False)
  return path


@pytest.fixture
def web(store):
  with app_module.app.test_client() as client:
    yield client


def _form(**overrides):
  values = {"action": "save", "PRINTER_NAME": "Workshop", "PRINTER_ID": SERIAL,
            "PRINTER_IP": "192.168.1.50", "SPOOLMAN_BASE_URL": "http://192.168.1.10:7912",
            "PRINTER_ACCESS_CODE": ""}
  values.update(overrides)
  return values


def test_the_page_opens_on_an_install_with_nothing_configured(web):
  """This is the page you go to *because* the settings are wrong.

  Every other route in the app turns a missing printer into an error screen.
  """
  response = web.get("/settings")
  assert response.status_code == 200
  assert b"Serial number" in response.data


def test_the_page_never_talks_to_spoolman(web, monkeypatch):
  """With no URL set, SPOOLMAN_API_URL is the string "None/api/v1"."""
  def _boom(*args, **kwargs):
    raise AssertionError("the settings page must not call SpoolMan on render")

  monkeypatch.setattr(spoolman_client, "fetchSpoolList", _boom)
  monkeypatch.setattr(spoolman_client, "fetchSettings", _boom)

  assert web.get("/settings").status_code == 200


def test_saving_writes_the_file(web, store):
  response = web.post("/settings", data=_form(), follow_redirects=False)

  assert response.status_code == 302
  saved = json.loads(store.read_text(encoding="utf-8"))
  assert saved["PRINTER_ID"] == SERIAL
  assert saved["SPOOLMAN_BASE_URL"] == "http://192.168.1.10:7912"


def test_the_url_loses_its_trailing_slash(web, store):
  web.post("/settings", data=_form(SPOOLMAN_BASE_URL="http://host:7912/"))
  assert json.loads(store.read_text())["SPOOLMAN_BASE_URL"] == "http://host:7912"


def test_the_access_code_is_never_rendered(web, store):
  web.post("/settings", data=_form(PRINTER_ACCESS_CODE=ACCESS_CODE))
  body = web.get("/settings").data

  assert ACCESS_CODE.encode() not in body
  assert b"unchanged" in body


def test_a_blank_access_code_keeps_the_stored_one(web, store):
  web.post("/settings", data=_form(PRINTER_ACCESS_CODE=ACCESS_CODE))
  web.post("/settings", data=_form(PRINTER_NAME="Renamed", PRINTER_ACCESS_CODE=""))

  saved = json.loads(store.read_text())
  assert saved["PRINTER_ACCESS_CODE"] == ACCESS_CODE
  assert saved["PRINTER_NAME"] == "Renamed"


def test_the_checkbox_removes_the_access_code(web, store):
  web.post("/settings", data=_form(PRINTER_ACCESS_CODE=ACCESS_CODE))
  web.post("/settings", data=_form(clear_printer_access_code="1"))

  assert "PRINTER_ACCESS_CODE" not in json.loads(store.read_text())


def test_a_bad_value_is_rejected_and_nothing_is_written(web, store):
  response = web.post("/settings", data=_form(PRINTER_ID="short"))

  assert response.status_code == 200
  assert b"8 to 20 letters and digits" in response.data
  assert not store.exists()


def test_a_request_from_somewhere_else_is_refused(web, store):
  response = web.post("/settings", data=_form(), headers={"Origin": "http://evil.example"})

  assert response.status_code == 400
  assert not store.exists()


def test_a_request_from_this_app_is_accepted(web, store):
  response = web.post("/settings", data=_form(),
                      headers={"Origin": "http://localhost"})
  assert response.status_code == 302


def test_testing_the_connection_writes_nothing(web, store, monkeypatch):
  monkeypatch.setattr(connection_probe, "probe_printer",
                      lambda *a, **k: [connection_probe.ProbeResult("printer_tcp", "ok", "reachable")])
  monkeypatch.setattr(connection_probe, "probe_spoolman",
                      lambda *a, **k: [connection_probe.ProbeResult("spoolman_api", "ok", "SpoolMan 0.26.1 is reachable.")])

  response = web.post("/settings", data=_form(action="test"))

  assert response.status_code == 200
  assert b"SpoolMan 0.26.1 is reachable." in response.data
  assert not store.exists()


def test_testing_uses_the_access_code_typed_into_the_form(web, store, monkeypatch):
  seen = {}
  monkeypatch.setattr(connection_probe, "probe_printer",
                      lambda ip, serial, code, **k: seen.update(code=code) or [])
  monkeypatch.setattr(connection_probe, "probe_spoolman", lambda *a, **k: [])

  web.post("/settings", data=_form(action="test", PRINTER_ACCESS_CODE=ACCESS_CODE))
  assert seen["code"] == ACCESS_CODE


def test_read_only_mode_refuses_to_save(web, store, monkeypatch):
  monkeypatch.setattr(app_module, "READ_ONLY_MODE", True)

  response = web.post("/settings", data=_form())

  assert b"read-only" in response.data
  assert not store.exists()


def test_the_restart_notice_appears_once_the_file_disagrees(web, store, monkeypatch):
  monkeypatch.setattr(app_module, "RUNNING_SETTINGS", {"PRINTER_NAME": "Old name"})
  import settings_routes
  monkeypatch.setattr(settings_routes, "RUNNING_SETTINGS", {"PRINTER_NAME": "Old name"})

  web.post("/settings", data=_form(PRINTER_NAME="New name"))
  assert b"not in use yet" in web.get("/settings").data
