import pytest
import requests

import spoolman_client as client


class _Response:
  def __init__(self, status_code, payload=None):
    self.status_code = status_code
    self._payload = payload

  def json(self):
    if self._payload is None:
      raise ValueError("no json")
    return self._payload


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
  """hasTagApi() caches, so every test starts from an unknown state."""
  monkeypatch.setattr(client, "_tag_api", {"known": None, "checked_at": 0.0})
  monkeypatch.setattr(client, "_log_spoolman_change", lambda *a, **k: None)


def _serve(monkeypatch, *responses, method="get"):
  """Answer each call with the next response; raise if one is an exception."""
  calls = []

  def _call(url, **kwargs):
    calls.append((url, kwargs))
    item = responses[min(len(calls) - 1, len(responses) - 1)]
    if isinstance(item, Exception):
      raise item
    return item

  monkeypatch.setattr(client._SESSION, method, _call)
  return calls


def test_probe_says_yes_on_200(monkeypatch):
  calls = _serve(monkeypatch, _Response(200, []))
  assert client.hasTagApi() is True
  assert calls[0][0].endswith("/tag/reader")
  assert "timeout" in calls[0][1]


def test_probe_says_no_on_404(monkeypatch):
  _serve(monkeypatch, _Response(404))
  assert client.hasTagApi() is False


def test_a_settled_answer_is_cached(monkeypatch):
  calls = _serve(monkeypatch, _Response(200, []))
  assert client.hasTagApi() is True
  assert client.hasTagApi() is True
  assert len(calls) == 1


def test_an_inconclusive_answer_is_not_cached(monkeypatch):
  """A 502 from a proxy must not switch the native path off for good."""
  calls = _serve(monkeypatch, _Response(502), _Response(200, []))

  assert client.hasTagApi() is False   # nothing known yet, so fall back
  assert client.hasTagApi() is True    # asked again, and got a real answer
  assert len(calls) == 2


def test_a_transport_failure_keeps_the_previous_answer(monkeypatch):
  _serve(monkeypatch, _Response(200, []))
  assert client.hasTagApi() is True

  monkeypatch.setattr(client, "_tag_api", {"known": True, "checked_at": 0.0})
  _serve(monkeypatch, requests.ConnectionError("boom"))
  assert client.hasTagApi() is True


def test_find_spool_by_tag_returns_the_single_match(monkeypatch):
  calls = _serve(monkeypatch, _Response(200, [{"id": 42}]))

  assert client.findSpoolByTag("04:a2:b3")["id"] == 42
  # The UID goes through as a parameter, so requests encodes it.
  assert calls[0][1]["params"] == {"tag": "04:a2:b3"}


def test_find_spool_by_tag_returns_none_when_nothing_matches(monkeypatch):
  _serve(monkeypatch, _Response(200, []))
  assert client.findSpoolByTag("DEADBEEF") is None


def test_find_spool_by_tag_survives_a_bad_uid(monkeypatch):
  """SpoolMan answers 400 for a UID it cannot normalize."""
  _serve(monkeypatch, _Response(400, {"message": "not hex"}))
  assert client.findSpoolByTag("nothex") is None


def test_link_tag_reports_the_conflicting_spool(monkeypatch):
  """The 409 body carries the spool that already holds the tag."""
  _serve(monkeypatch, _Response(409, {"message": "taken", "spool_id": 147}), method="post")

  status, conflict = client.linkTag(9, "04A2B3", "bambu")
  assert (status, conflict) == (409, 147)


def test_link_tag_passes_the_format_only_when_given(monkeypatch):
  calls = _serve(monkeypatch, _Response(201, {"uid": "04A2B3"}), method="post")

  client.linkTag(9, "04A2B3")
  assert calls[0][1]["json"] == {"uid": "04A2B3"}

  client.linkTag(9, "04A2B3", "bambu")
  assert calls[1][1]["json"] == {"uid": "04A2B3", "format": "bambu"}


def test_link_tag_reports_a_transport_failure_as_no_status(monkeypatch):
  _serve(monkeypatch, requests.ConnectionError("boom"), method="post")
  assert client.linkTag(9, "04A2B3") == (None, None)


def test_unlink_tag_returns_the_status(monkeypatch):
  _serve(monkeypatch, _Response(204), method="delete")
  assert client.unlinkTag(9, "04A2B3") == 204
