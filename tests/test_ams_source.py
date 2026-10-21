import json

import pytest
from flask import Flask

import ams_source_routes as src
import mqtt_bambulab
import spoolman_client
import spoolman_service


def _uid(ams_id, tray_id):
  return spoolman_service.trayUid(ams_id, tray_id)


def _spool(spool_id, ams_id=None, tray_id=None, remaining=None):
  extra = {}
  if ams_id is not None:
    extra["active_tray"] = json.dumps(_uid(ams_id, tray_id))
  spool = {"id": spool_id, "extra": extra}
  if remaining is not None:
    spool["remaining_weight"] = remaining
  return spool


def _config():
  return {
      "ams": [
          {
              "id": "0",
              "humidity": "4",
              "humidity_raw": "31",
              "temp": "24.6",
              "dry_time": 0,
              "tray": [
                  {"id": "0"},
                  {"id": "1", "tray_type": "PETG", "tray_sub_brands": "PETG HF",
                   "tray_color": "000000FF", "remain": 68},
                  {"id": "2", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": -1},
                  {"id": "3", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 10},
              ],
          },
          {"id": "128", "humidity_raw": "12", "temp": "40.0", "dry_time": 95,
           "tray": [{"id": "0", "tray_type": "PA-CF", "tray_color": "333333FF", "remain": 90}]},
      ],
      "vt_tray": {"id": "254", "tray_type": "TPU", "tray_color": "00FF00FF", "remain": 0},
      "get_version": {"models_by_id": {"0": "AMS 2 Pro", 0: "AMS 2 Pro", "128": "AMS HT"}},
      "status": {"tray_now": "1", "tray_exist_bits": "e", "gcode_state": "RUNNING",
                 "mc_percent": 42},
  }


def test_status_carries_the_spool_per_tray_and_never_active():
  spools = [_spool(16, 0, 1, remaining=683.94), _spool(39, 0, 3)]
  out = src.build_status(_config(), spools, "X1C", True)

  unit = out["ams"][0]
  trays = {t["id"]: t for t in unit["tray"]}
  assert trays[1]["spool_id"] == 16
  assert trays[1]["remain_g"] == 684
  assert trays[3]["spool_id"] == 39
  assert trays[3]["remain_g"] is None
  assert trays[0]["spool_id"] is None
  assert trays[1]["tray_sub_brands"] == "PETG HF"
  assert all("active" not in t for t in unit["tray"])
  assert out["tray_now"] == 1
  assert out["gcode_state"] == "RUNNING"
  assert out["mc_percent"] == 42
  assert out["id"] == 1 and out["version"] == 1


def test_humidity_is_the_percentage_never_the_index():
  out = src.build_status(_config(), [], "X1C", True)
  assert out["ams"][0]["humidity"] == 31

  config = _config()
  del config["ams"][0]["humidity_raw"]
  out = src.build_status(config, [], "X1C", True)
  assert out["ams"][0]["humidity"] is None


def test_exists_follows_the_printer_bits():
  out = src.build_status(_config(), [], "X1C", True)
  exists = [t["exists"] for t in out["ams"][0]["tray"]]
  # "e" = 1110: tray 0 empty, 1 to 3 loaded.
  assert exists == [False, True, True, True]

  config = _config()
  del config["status"]["tray_exist_bits"]
  out = src.build_status(config, [], "X1C", True)
  assert [t["exists"] for t in out["ams"][0]["tray"]] == [False, True, True, True]


def test_models_and_the_ht_unit():
  out = src.build_status(_config(), [], "X1C", True)
  assert out["ams"][0]["module_type"] == "n3f"
  assert out["ams"][0]["is_ams_ht"] is False
  ht = out["ams"][1]
  assert ht["module_type"] == "n3s"
  assert ht["is_ams_ht"] is True
  assert ht["dry_time"] == 95
  assert ht["temp"] == 40.0
  # Bits only describe the four regular units.
  assert ht["tray"][0]["exists"] is True


def test_external_holder_is_a_list_with_its_spool():
  spools = [_spool(7, src.EXTERNAL_SPOOL_AMS_ID, src.EXTERNAL_SPOOL_ID)]
  out = src.build_status(_config(), spools, "X1C", True)
  assert len(out["vt_tray"]) == 1
  assert out["vt_tray"][0]["id"] == 254
  assert out["vt_tray"][0]["spool_id"] == 7


def test_an_empty_report_is_a_valid_answer():
  out = src.build_status({}, [], "X1C", False)
  assert out["ams"] == [] and out["vt_tray"] == []
  assert out["ams_exists"] is False
  assert out["tray_now"] == src.TRAY_NOW_NONE
  assert out["mc_percent"] is None


def test_ams_lite_has_no_sensor_values():
  config = _config()
  config["get_version"]["models_by_id"] = {"0": "AMS Lite", 0: "AMS Lite"}
  out = src.build_status(config, [], "A1", True)
  assert out["ams"][0]["module_type"] == "ams_lite"
  assert out["ams"][0]["humidity"] is None
  assert out["ams"][0]["temp"] is None


@pytest.fixture
def client(monkeypatch):
  app = Flask(__name__)
  app.register_blueprint(src.ams_source_bp)
  monkeypatch.setattr(src, "READ_ONLY_MODE", False)
  monkeypatch.setattr(mqtt_bambulab, "isMqttClientConnected", lambda: True)
  monkeypatch.setattr(mqtt_bambulab, "getLastAMSConfig", _config)
  monkeypatch.setattr(mqtt_bambulab, "fetchSpools", lambda *a, **k: [_spool(16, 0, 1)])
  monkeypatch.setattr(src, "_printer_name", lambda: "X1C")
  return app.test_client()


def test_discovery_names_version_and_capabilities(client):
  resp = client.get("/api/v1/ams-source")
  assert resp.status_code == 200
  body = resp.get_json()
  assert body["version"] == 1
  assert body["server"]["name"] == "OpenSpoolMan"
  assert body["server"]["version"]
  assert {"assign", "unassign"} <= set(body["capabilities"])


def test_printer_list_is_a_bare_array(client):
  resp = client.get("/api/v1/ams-source/printers")
  assert resp.status_code == 200
  assert resp.get_json() == [{"id": 1, "name": "X1C", "connected": True}]


def test_status_route_and_unknown_printer(client):
  resp = client.get("/api/v1/ams-source/printers/1/status")
  assert resp.status_code == 200
  assert resp.get_json()["ams"][0]["tray"][1]["spool_id"] == 16
  assert client.get("/api/v1/ams-source/printers/2/status").status_code == 404


def test_assign_needs_all_three_fields(client):
  resp = client.post("/api/v1/ams-source/printers/1/assign", json={"spool_id": 5, "ams_id": 0})
  assert resp.status_code == 400


def test_assign_is_refused_while_the_printer_is_offline(client, monkeypatch):
  monkeypatch.setattr(mqtt_bambulab, "isMqttClientConnected", lambda: False)
  resp = client.post("/api/v1/ams-source/printers/1/assign",
                     json={"spool_id": 5, "ams_id": 0, "tray_id": 2})
  assert resp.status_code == 503


def test_assign_claims_the_tray(client, monkeypatch):
  # Imported before anything is patched: importing app applies the seeded
  # test-data patches, which would otherwise replace the ones below.
  import app as app_module
  claimed = []
  monkeypatch.setattr(spoolman_client, "getSpoolById", lambda sid: {"id": sid, "extra": {}})
  monkeypatch.setattr(mqtt_bambulab, "setActiveTray",
                      lambda sid, extra, ams, tray: claimed.append((sid, ams, tray)))
  monkeypatch.setattr(app_module, "setActiveSpool", lambda *a: None)

  resp = client.post("/api/v1/ams-source/printers/1/assign",
                     json={"spool_id": 5, "ams_id": 0, "tray_id": 2})
  assert resp.status_code == 200
  assert claimed == [(5, 0, 2)]
  assert resp.get_json()["spool_id"] == 5


def test_assign_is_refused_in_read_only_mode(client, monkeypatch):
  monkeypatch.setattr(src, "READ_ONLY_MODE", True)
  resp = client.post("/api/v1/ams-source/printers/1/assign",
                     json={"spool_id": 5, "ams_id": 0, "tray_id": 2})
  assert resp.status_code == 403


def test_unassign_clears_the_claim_and_is_idempotent(client, monkeypatch):
  spools = [_spool(16, 0, 1), _spool(17, 0, 2)]
  patched = []

  def _patch(spool_id, old_extras, new_extras):
    old_extras.update(new_extras)
    patched.append((spool_id, dict(new_extras)))

  monkeypatch.setattr(spoolman_service, "fetchSpools", lambda cached=False: spools)
  monkeypatch.setattr(spoolman_client, "patchExtraTags", _patch)

  resp = client.post("/api/v1/ams-source/printers/1/unassign", json={"ams_id": 0, "tray_id": 1})
  assert resp.status_code == 200
  assert resp.get_json()["spool_id"] == 16
  assert patched == [(16, {"active_tray": json.dumps("")})]

  resp = client.post("/api/v1/ams-source/printers/1/unassign", json={"ams_id": 0, "tray_id": 1})
  assert resp.status_code == 200
  assert resp.get_json()["spool_id"] is None
