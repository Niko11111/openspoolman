"""The connection test. Offline: no broker, no SpoolMan, no sockets."""

import socket

import pytest
import requests

import connection_probe as probe


class _Response:
  def __init__(self, status_code, payload=None):
    self.status_code = status_code
    self._payload = payload

  def json(self):
    if self._payload is None:
      raise ValueError("no json")
    return self._payload


def _serve(monkeypatch, *responses):
  calls = []

  def _get(url, **kwargs):
    calls.append(url)
    item = responses[min(len(calls) - 1, len(responses) - 1)]
    if isinstance(item, Exception):
      raise item
    return item

  monkeypatch.setattr(probe.requests, "get", _get)
  return calls


def _by_name(results):
  return {r.name: r for r in results}


@pytest.mark.parametrize("rc,status", [
  (0, probe.OK), (1, probe.FAIL), (2, probe.FAIL),
  (3, probe.FAIL), (4, probe.FAIL), (5, probe.FAIL), (99, probe.FAIL),
])
def test_every_connack_code_has_an_answer(rc, status):
  assert probe._mqtt_rc_message(rc)[0] == status


def test_a_refused_access_code_says_so():
  for rc in (4, 5):
    assert "access code" in probe._mqtt_rc_message(rc)[1]


def test_spoolman_reports_its_version_and_native_tags(monkeypatch):
  _serve(monkeypatch, _Response(200, {"version": "0.26.1"}), _Response(200, []))

  results = _by_name(probe.probe_spoolman("http://host:7912"))
  assert results["spoolman_api"].status == probe.OK
  assert "0.26.1" in results["spoolman_api"].message
  assert results["spoolman_tag_reader"].status == probe.OK


def test_a_spoolman_without_the_tag_relation_is_a_note_not_a_failure(monkeypatch):
  """NFC is optional, so this must never read as something being broken."""
  _serve(monkeypatch, _Response(200, {"version": "0.26.1"}), _Response(404))

  results = _by_name(probe.probe_spoolman("http://host:7912"))
  assert results["spoolman_api"].status == probe.OK
  assert results["spoolman_tag_reader"].status == probe.WARN


def test_a_wrong_path_is_told_apart_from_an_unreachable_host(monkeypatch):
  _serve(monkeypatch, _Response(404))
  results = _by_name(probe.probe_spoolman("http://host:7912/spoolman"))
  assert results["spoolman_api"].status == probe.FAIL
  assert "no SpoolMan API" in results["spoolman_api"].message

  _serve(monkeypatch, requests.ConnectionError("refused"))
  results = _by_name(probe.probe_spoolman("http://host:7912"))
  assert "Could not reach" in results["spoolman_api"].message


def test_a_timeout_says_timeout(monkeypatch):
  _serve(monkeypatch, requests.Timeout("slow"))
  assert "in time" in _by_name(probe.probe_spoolman("http://h"))["spoolman_api"].message


def test_something_that_is_not_spoolman(monkeypatch):
  """A web server answering 200 with HTML is not a working configuration."""
  _serve(monkeypatch, _Response(200))
  results = _by_name(probe.probe_spoolman("http://host"))
  assert results["spoolman_api"].status == probe.FAIL
  assert "not with SpoolMan" in results["spoolman_api"].message


def test_no_url_is_a_failure_without_a_request(monkeypatch):
  calls = _serve(monkeypatch, _Response(200, {"version": "x"}))
  results = probe.probe_spoolman("")
  assert results[0].status == probe.FAIL
  assert calls == []


def test_an_unresolvable_printer_is_told_apart_from_a_closed_port(monkeypatch):
  monkeypatch.setattr(probe.socket, "create_connection",
                      lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("no such host")))
  results = probe.probe_printer("printer.invalid", "SERIAL", "code")
  assert "Cannot resolve" in results[0].message

  monkeypatch.setattr(probe.socket, "create_connection",
                      lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
  results = probe.probe_printer("192.168.1.50", "SERIAL", "code")
  assert "No answer from" in results[0].message


def test_a_live_session_is_the_answer_and_opens_nothing(monkeypatch):
  """The common case: somebody opens the page while the app is connected."""
  monkeypatch.setattr(probe.socket, "create_connection", lambda *a, **k: _Closable())
  monkeypatch.setattr(probe, "_probe_mqtt",
                      lambda *a, **k: pytest.fail("no second connection may be opened"))

  results = _by_name(probe.probe_printer("192.168.1.50", "SERIAL", "code", live_session=True))
  assert results["printer_mqtt"].status == probe.OK
  assert "No second connection" in results["printer_mqtt"].detail


def test_a_quiet_printer_is_a_warning_not_a_failure(monkeypatch):
  monkeypatch.setattr(probe.socket, "create_connection", lambda *a, **k: _Closable())
  monkeypatch.setattr(probe, "_probe_mqtt", lambda *a, **k: (probe.OK, "Connected.", False))

  results = _by_name(probe.probe_printer("192.168.1.50", "SERIAL", "code"))
  assert results["printer_mqtt"].status == probe.OK
  assert results["printer_report"].status == probe.WARN


def test_a_printer_publishing_under_the_serial_confirms_it(monkeypatch):
  monkeypatch.setattr(probe.socket, "create_connection", lambda *a, **k: _Closable())
  monkeypatch.setattr(probe, "_probe_mqtt", lambda *a, **k: (probe.OK, "Connected.", True))

  results = _by_name(probe.probe_printer("192.168.1.50", "SERIAL", "code"))
  assert results["printer_report"].status == probe.OK


def test_a_rejected_access_code_stops_before_the_serial_check(monkeypatch):
  monkeypatch.setattr(probe.socket, "create_connection", lambda *a, **k: _Closable())
  monkeypatch.setattr(probe, "_probe_mqtt", lambda *a, **k: (probe.FAIL, "refused", False))

  results = _by_name(probe.probe_printer("192.168.1.50", "SERIAL", "wrong"))
  assert results["printer_mqtt"].status == probe.FAIL
  assert "printer_report" not in results


class _Closable:
  def close(self):
    pass
