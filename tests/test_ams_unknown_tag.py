"""An AMS tag that SpoolMan does not know must not reset the AMS tray.

Regression cover for #95: the printer identified a new Bambu spool, this app
answered every report with an ams_filament_setting that blanked the tray, and
the panel fell back to "?" until the tag was linked.
"""

import json

import pytest

import mqtt_bambulab


UNKNOWN_TAG = "AABBCCDDEEFF00112233445566778899"
KNOWN_TAG = "11223344556677889900112233445566"
EMPTY_TAG = "0" * 32


class _RecordingClient:
  def __init__(self):
    self.published = []

  def publish(self, topic, payload):
    self.published.append((topic, json.loads(payload)))
    return (0, 1)


def _ams_payload(tray_uuid):
  return {
    "print": {
      "ams": {
        "ams": [
          {
            "id": 0,
            "humidity": "4",
            "temp": "25",
            "tray": [
              {
                "id": 0,
                "tray_uuid": tray_uuid,
                "tray_sub_brands": "PLA Basic",
                "tray_color": "FFFFFFFF",
                "remain": 80,
              }
            ],
          }
        ]
      }
    }
  }


class _Msg:
  def __init__(self, payload):
    self.payload = json.dumps(payload).encode()


@pytest.fixture
def harness(monkeypatch):
  client = _RecordingClient()
  cleared = []

  # The MQTT log path is a container-absolute path; never touch it from a test.
  monkeypatch.setattr(mqtt_bambulab, "append_to_rotating_file", lambda *a, **k: None)
  monkeypatch.setattr(mqtt_bambulab, "MQTT_CLIENT", client)
  monkeypatch.setattr(mqtt_bambulab, "clear_active_spool_for_tray",
                      lambda ams_id, tray_id: cleared.append((ams_id, tray_id)))

  spool = {"id": 7, "extra": {"tag": json.dumps(KNOWN_TAG)}}
  monkeypatch.setattr(mqtt_bambulab, "fetchSpools", lambda *a, **k: [spool])

  assigned = []
  monkeypatch.setattr(mqtt_bambulab, "setActiveTray",
                      lambda spool_id, extra, ams_id, tray_id: assigned.append(spool_id))

  mqtt_bambulab.LAST_AMS_CONFIG = {}
  return client, cleared, assigned


def test_unknown_tag_does_not_reset_the_ams_tray(harness):
  client, cleared, _ = harness

  mqtt_bambulab.on_message(None, None, _Msg(_ams_payload(UNKNOWN_TAG)))

  tray = mqtt_bambulab.LAST_AMS_CONFIG["ams"][0]["tray"][0]
  # The branch under test really ran.
  assert tray["unmapped_bambu_tag"] == UNKNOWN_TAG
  assert tray["issue"] is True
  assert cleared == [(0, 0)]
  # ...and it sent nothing to the printer.
  assert client.published == []


def test_a_known_tag_assigns_the_spool_and_stays_quiet(harness):
  client, cleared, assigned = harness

  mqtt_bambulab.on_message(None, None, _Msg(_ams_payload(KNOWN_TAG)))

  assert assigned == [7]
  assert cleared == []
  assert client.published == []


def test_an_empty_tray_uuid_is_left_alone(harness):
  client, cleared, assigned = harness

  mqtt_bambulab.on_message(None, None, _Msg(_ams_payload(EMPTY_TAG)))

  tray = mqtt_bambulab.LAST_AMS_CONFIG["ams"][0]["tray"][0]
  assert "unmapped_bambu_tag" not in tray
  assert assigned == []
  assert cleared == []
  assert client.published == []
