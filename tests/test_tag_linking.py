"""Writing a tag through SpoolMan's relation, and the 409 that can come back."""

import json
import os

import pytest

# Importing app runs init_mqtt() unless the test data flag is set, and that
# thread is not a daemon - a plain `pytest` run would hang on it. CI sets the
# flag; skip rather than hang when it is missing.
pytestmark = pytest.mark.skipif(
    os.getenv("OPENSPOOLMAN_TEST_DATA") != "1",
    reason="needs OPENSPOOLMAN_TEST_DATA=1, as the CI workflow sets",
)

import app as app_module  # noqa: E402
import spoolman_client  # noqa: E402


@pytest.fixture
def spoolman(monkeypatch):
  """A SpoolMan that supports tags and records what it was told."""
  calls = {"link": [], "unlink": [], "patch": []}
  state = {"has_tags": True, "link_result": (201, None)}

  monkeypatch.setattr(spoolman_client, "hasTagApi", lambda: state["has_tags"])

  def _link(spool_id, uid, tag_format=None):
    calls["link"].append((int(spool_id), uid, tag_format))
    return state["link_result"]

  def _unlink(spool_id, uid):
    calls["unlink"].append((int(spool_id), uid))
    return 204

  def _patch(spool_id, old_extras, new_extras):
    for key, value in new_extras.items():
      old_extras[key] = value
    calls["patch"].append((int(spool_id), dict(old_extras)))

  monkeypatch.setattr(spoolman_client, "linkTag", _link)
  monkeypatch.setattr(spoolman_client, "unlinkTag", _unlink)
  monkeypatch.setattr(spoolman_client, "patchExtraTags", _patch)
  return calls, state


def test_nothing_is_linked_when_the_server_has_no_tag_relation(spoolman):
  calls, state = spoolman
  state["has_tags"] = False

  assert app_module.link_tag_natively(1, "04A2B3", "bambu") == (True, None)
  assert calls["link"] == []


def test_a_tag_is_linked_with_its_format(spoolman):
  calls, _ = spoolman

  assert app_module.link_tag_natively(1, "04A2B3", "bambu") == (True, None)
  assert calls["link"] == [(1, "04A2B3", "bambu")]


def test_a_conflict_is_reported_with_the_spool_that_holds_the_tag(spoolman):
  _, state = spoolman
  state["link_result"] = (409, 147)

  assert app_module.link_tag_natively(1, "04A2B3", "bambu") == (False, 147)


def test_any_other_failure_does_not_block_the_legacy_write(spoolman):
  """extra.tag has to keep working even when the relation is unavailable."""
  _, state = spoolman

  state["link_result"] = (500, None)
  assert app_module.link_tag_natively(1, "04A2B3", "bambu") == (True, None)

  state["link_result"] = (None, None)
  assert app_module.link_tag_natively(1, "04A2B3", "bambu") == (True, None)


def test_moving_a_tag_unlinks_it_and_clears_the_stale_legacy_value(spoolman, monkeypatch):
  calls, _ = spoolman
  monkeypatch.setattr(spoolman_client, "getSpoolById",
                      lambda spool_id: {"id": int(spool_id),
                                        "extra": {"tag": json.dumps("04A2B3"),
                                                  "active_tray": json.dumps("P_0_1")}})

  assert app_module.move_tag_natively("04A2B3", 147, 9, "bambu") == (True, None)

  assert calls["unlink"] == [(147, "04A2B3")]
  assert calls["link"] == [(9, "04A2B3", "bambu")]
  # The old spool's legacy field is cleared, and nothing else on it is lost.
  spool_id, extras = calls["patch"][0]
  assert spool_id == 147
  assert extras["tag"] == json.dumps("")
  assert extras["active_tray"] == json.dumps("P_0_1")


def test_moving_leaves_a_legacy_value_that_names_a_different_tag(spoolman, monkeypatch):
  calls, _ = spoolman
  monkeypatch.setattr(spoolman_client, "getSpoolById",
                      lambda spool_id: {"id": int(spool_id),
                                        "extra": {"tag": json.dumps("SOMETHINGELSE")}})

  app_module.move_tag_natively("04A2B3", 147, 9, "bambu")

  assert calls["unlink"] == [(147, "04A2B3")]
  assert calls["patch"] == []


def test_write_tag_keeps_the_other_extra_fields(spoolman, monkeypatch):
  """It used to send {} as the existing extras, wiping active_tray."""
  calls, _ = spoolman
  monkeypatch.setattr(spoolman_client, "getSpoolById",
                      lambda spool_id: {"id": int(spool_id),
                                        "extra": {"active_tray": json.dumps("P_0_1"),
                                                  "german_name": json.dumps("Weiss")}})

  with app_module.app.test_client() as web:
    response = web.get("/write_tag?spool_id=5")

  assert response.status_code == 200

  spool_id, extras = calls["patch"][0]
  assert spool_id == 5
  assert extras["active_tray"] == json.dumps("P_0_1")
  assert extras["german_name"] == json.dumps("Weiss")
  assert json.loads(extras["tag"])

  # The same uuid is linked natively, tagged as this app's own convention.
  linked_spool, linked_uid, linked_format = calls["link"][0]
  assert (linked_spool, linked_format) == (5, "openspool")
  assert linked_uid == json.loads(extras["tag"])
