"""Moving the legacy extra.tag values into SpoolMan's tag relation."""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("OPENSPOOLMAN_TEST_DATA") != "1",
    reason="needs OPENSPOOLMAN_TEST_DATA=1, as the CI workflow sets",
)

import app as app_module  # noqa: E402
import spoolman_client  # noqa: E402
import spoolman_service  # noqa: E402


def _spool(spool_id, tag=None, tags=None, name="PLA"):
  spool = {"id": spool_id, "filament": {"name": name}, "extra": {}}
  if tag is not None:
    spool["extra"]["tag"] = json.dumps(tag)
  if tags:
    spool["tags"] = [{"uid": uid} for uid in tags]
  return spool


def test_the_plan_skips_what_has_nothing_to_migrate():
  rows = app_module.plan_tag_migration([
      _spool(1),                                    # no tag field at all
      _spool(2, ""),                                # cleared
      _spool(3, spoolman_service.EMPTY_TRAY_UUID),  # third-party placeholder
  ])
  assert rows == []


def test_the_plan_marks_a_value_already_in_the_relation():
  rows = app_module.plan_tag_migration([_spool(4, "04a2b3", tags=["04A2B3"])])
  assert [row["state"] for row in rows] == ["linked"]


def test_the_plan_marks_a_value_spoolman_would_refuse():
  rows = app_module.plan_tag_migration([_spool(5, "shelf-left")])
  assert [row["state"] for row in rows] == ["invalid"]


def test_the_plan_puts_the_work_first():
  rows = app_module.plan_tag_migration([
      _spool(7, "shelf"),
      _spool(8, "AABB"),
      _spool(9, "CCDD", tags=["CCDD"]),
  ])
  assert [(row["spool_id"], row["state"]) for row in rows] == [
      (8, "ready"), (7, "invalid"), (9, "linked"),
  ]


def test_running_records_what_spoolman_answered(monkeypatch):
  answers = {1: (201, None), 2: (409, 147), 3: (400, None), 4: (503, None)}
  monkeypatch.setattr(spoolman_client, "linkTag",
                      lambda spool_id, uid, tag_format=None: answers[int(spool_id)])

  rows = app_module.run_tag_migration([
      {"spool_id": spool_id, "value": "AABB", "state": "ready"} for spool_id in (1, 2, 3, 4)
  ])

  assert [row["state"] for row in rows] == ["created", "conflict", "invalid", "failed"]
  assert rows[1]["conflict_spool_id"] == 147
  assert rows[3]["status"] == 503


def test_running_leaves_rows_that_were_not_ready_alone(monkeypatch):
  called = []
  monkeypatch.setattr(spoolman_client, "linkTag",
                      lambda *a, **k: called.append(a) or (201, None))

  rows = app_module.run_tag_migration([
      {"spool_id": 1, "value": "AABB", "state": "linked"},
      {"spool_id": 2, "value": "shelf", "state": "invalid"},
  ])

  assert called == []
  assert [row["state"] for row in rows] == ["linked", "invalid"]


def test_no_format_is_guessed(monkeypatch):
  """A Bambu tray uuid and a written uuid4 look the same after the fact."""
  seen = []
  monkeypatch.setattr(spoolman_client, "linkTag",
                      lambda spool_id, uid, tag_format=None: seen.append(tag_format) or (201, None))

  app_module.run_tag_migration([{"spool_id": 1, "value": "AABB", "state": "ready"}])
  assert seen == [None]


def test_the_page_is_refused_when_the_server_has_no_tag_relation(monkeypatch):
  monkeypatch.setattr(spoolman_client, "hasTagApi", lambda: False)

  with app_module.app.test_client() as web:
    response = web.get("/migrate_tags")

  assert response.status_code == 200
  assert b"no tag relation yet" in response.data


def test_the_dry_run_writes_nothing(monkeypatch):
  called = []
  monkeypatch.setattr(spoolman_client, "hasTagApi", lambda: True)
  monkeypatch.setattr(spoolman_service, "fetchSpools", lambda *a, **k: [_spool(2, "AABB")])
  monkeypatch.setattr(spoolman_client, "linkTag", lambda *a, **k: called.append(a) or (201, None))

  with app_module.app.test_client() as web:
    response = web.get("/migrate_tags")

  assert response.status_code == 200
  assert called == []
  assert b"will be linked" in response.data


def test_running_it_links(monkeypatch):
  called = []
  monkeypatch.setattr(spoolman_client, "hasTagApi", lambda: True)
  monkeypatch.setattr(spoolman_service, "fetchSpools", lambda *a, **k: [_spool(2, "AABB")])
  monkeypatch.setattr(spoolman_client, "linkTag",
                      lambda spool_id, uid, tag_format=None: called.append((spool_id, uid)) or (201, None))

  with app_module.app.test_client() as web:
    response = web.get("/migrate_tags?run=1")

  assert response.status_code == 200
  assert called == [(2, "AABB")]
