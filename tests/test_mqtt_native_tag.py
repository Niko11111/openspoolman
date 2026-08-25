"""The AMS tray loop must recognise a spool linked through SpoolMan's tags."""

import json

import pytest

import mqtt_bambulab


TRAY_UUID = "6AB8A4FE42EB4D7291C3F0A5B7D8E9F0"


def _ams_payload(tray_uuid):
  return {"print": {"ams": {"ams": [{
    "id": 0, "humidity": "4", "temp": "25",
    "tray": [{"id": 0, "tray_uuid": tray_uuid, "tray_sub_brands": "PLA Basic",
              "tray_color": "FFFFFFFF", "remain": 80}],
  }]}}}


class _Msg:
  def __init__(self, payload):
    self.payload = json.dumps(payload).encode()


@pytest.fixture
def harness(monkeypatch):
  assigned = []
  monkeypatch.setattr(mqtt_bambulab, "append_to_rotating_file", lambda *a, **k: None)
  monkeypatch.setattr(mqtt_bambulab, "MQTT_CLIENT", None)
  monkeypatch.setattr(mqtt_bambulab, "clear_active_spool_for_tray", lambda *a, **k: None)
  monkeypatch.setattr(mqtt_bambulab, "clear_ams_tray_assignment", lambda *a, **k: None)
  monkeypatch.setattr(mqtt_bambulab, "setActiveTray",
                      lambda spool_id, extra, ams_id, tray_id: assigned.append(spool_id))
  mqtt_bambulab.LAST_AMS_CONFIG = {}

  def _use(spools):
    monkeypatch.setattr(mqtt_bambulab, "fetchSpools", lambda *a, **k: spools)

  return _use, assigned


def test_a_spool_found_through_the_native_relation_is_assigned(harness):
  use, assigned = harness
  use([{"id": 279, "extra": {}, "tags": [{"uid": TRAY_UUID, "format": "bambu"}]}])

  mqtt_bambulab.on_message(None, None, _Msg(_ams_payload(TRAY_UUID)))

  assert assigned == [279]
  assert "unmapped_bambu_tag" not in mqtt_bambulab.LAST_AMS_CONFIG["ams"][0]["tray"][0]


def test_the_legacy_extra_tag_still_works(harness):
  use, assigned = harness
  use([{"id": 12, "extra": {"tag": json.dumps(TRAY_UUID)}}])

  mqtt_bambulab.on_message(None, None, _Msg(_ams_payload(TRAY_UUID)))

  assert assigned == [12]


def test_a_lowercase_tray_uuid_still_finds_the_spool(harness):
  """Exact string comparison used to miss this."""
  use, assigned = harness
  use([{"id": 12, "extra": {"tag": json.dumps(TRAY_UUID)}}])

  mqtt_bambulab.on_message(None, None, _Msg(_ams_payload(TRAY_UUID.lower())))

  assert assigned == [12]


def test_an_unknown_tag_assigns_nothing_and_is_reported(harness):
  use, assigned = harness
  use([{"id": 12, "extra": {"tag": json.dumps("AABBCCDD")}}])

  mqtt_bambulab.on_message(None, None, _Msg(_ams_payload(TRAY_UUID)))

  tray = mqtt_bambulab.LAST_AMS_CONFIG["ams"][0]["tray"][0]
  assert assigned == []
  assert tray["unmapped_bambu_tag"] == TRAY_UUID


def test_a_third_party_spool_is_not_looked_up(harness):
  use, assigned = harness
  use([{"id": 12, "extra": {"tag": json.dumps("0" * 32)}}])

  mqtt_bambulab.on_message(None, None, _Msg(_ams_payload("0" * 32)))

  tray = mqtt_bambulab.LAST_AMS_CONFIG["ams"][0]["tray"][0]
  assert assigned == []
  assert "unmapped_bambu_tag" not in tray
