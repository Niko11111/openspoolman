import json

import pytest

import spoolman_service as svc


def _spool(spool_id, active_tray=None):
  extra = {}
  if active_tray is not None:
    extra["active_tray"] = json.dumps(active_tray)
  return {"id": spool_id, "extra": extra}


@pytest.fixture
def patched(monkeypatch):
  """Record every extra-field PATCH instead of sending it to SpoolMan."""
  calls = []

  def _patch(spool_id, old_extras, new_extras):
    for key, value in new_extras.items():
      old_extras[key] = value
    calls.append((int(spool_id), dict(new_extras)))

  monkeypatch.setattr(svc.spoolman_client, "patchExtraTags", _patch)
  return calls


def _install_spools(monkeypatch, spools):
  """Serve a fixed spool list and record whether the cache was asked for."""
  seen = []

  def _fetch(cached=False):
    seen.append(cached)
    return spools

  monkeypatch.setattr(svc, "fetchSpools", _fetch)
  return seen


def test_claims_the_tray_and_clears_the_previous_owner(monkeypatch, patched):
  tray = svc.trayUid(0, 1)
  spools = [_spool(1), _spool(2, tray)]
  _install_spools(monkeypatch, spools)

  svc.setActiveTray(1, spools[0]["extra"], 0, 1)

  assert (1, {"active_tray": json.dumps(tray)}) in patched
  assert (2, {"active_tray": json.dumps("")}) in patched


def test_second_claimant_is_cleared_even_when_the_tray_is_already_claimed(monkeypatch, patched):
  """The regression behind #93.

  A spool that already claims the tray used to return early, which skipped the
  cleanup and left a second spool claiming the same tray. The renderer then
  matches whichever comes first in the list, so the wrong spool shows up.
  """
  tray = svc.trayUid(0, 1)
  spools = [_spool(1, tray), _spool(2, tray)]
  _install_spools(monkeypatch, spools)

  svc.setActiveTray(1, spools[0]["extra"], 0, 1)

  assert (2, {"active_tray": json.dumps("")}) in patched
  # The spool that already owns the tray is not written again.
  assert not [call for call in patched if call[0] == 1]
  assert sum(1 for s in spools if s["extra"].get("active_tray") == json.dumps(tray)) == 1


def test_the_skip_path_does_not_refetch_the_spool_list(monkeypatch, patched):
  """The MQTT thread calls this for every matched tray on every AMS report."""
  tray = svc.trayUid(0, 1)
  spools = [_spool(1, tray)]
  seen = _install_spools(monkeypatch, spools)

  svc.setActiveTray(1, spools[0]["extra"], 0, 1)

  assert seen == [True]


def test_the_write_path_refetches_the_spool_list(monkeypatch, patched):
  tray = svc.trayUid(0, 1)
  spools = [_spool(1)]
  seen = _install_spools(monkeypatch, spools)

  svc.setActiveTray(1, spools[0]["extra"], 0, 1)

  assert seen == [False]


def test_missing_extra_is_treated_as_unclaimed(monkeypatch, patched):
  tray = svc.trayUid(0, 1)
  spools = [_spool(1)]
  _install_spools(monkeypatch, spools)

  svc.setActiveTray(1, None, 0, 1)

  assert (1, {"active_tray": json.dumps(tray)}) in patched


def test_clear_active_spool_for_tray_clears_every_claimant(monkeypatch, patched):
  """It used to break after the first, which cannot undo a duplicate claim."""
  tray = svc.trayUid(0, 1)
  spools = [_spool(1, tray), _spool(2, tray), _spool(3, svc.trayUid(0, 2))]
  _install_spools(monkeypatch, spools)

  svc.clear_active_spool_for_tray(0, 1)

  cleared = {call[0] for call in patched if call[1] == {"active_tray": json.dumps("")}}
  assert cleared == {1, 2}
