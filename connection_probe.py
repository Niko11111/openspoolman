"""Does this configuration actually reach the printer and SpoolMan.

Diagnostics only. Nothing here writes anything anywhere, and the printer probe
never publishes - it connects, listens, and hangs up. A running print must not
notice that somebody opened the settings page.

There are no retries on purpose: a diagnostic should report the first failure,
not hide it behind a second attempt.
"""

import socket
import ssl
from dataclasses import dataclass

import paho.mqtt.client as mqtt
import requests

from logger import log

MQTT_PORT = 8883
PROBE_TIMEOUT = 5.0
SPOOLMAN_TIMEOUT = (3.05, 5)

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"


@dataclass(frozen=True)
class ProbeResult:
  name: str
  status: str
  message: str
  detail: str = ""


def _mqtt_rc_message(rc):
  """What a CONNACK code means, as (status, message).

  Pure, so the mapping can be tested without a broker.
  """
  if rc == 0:
    return OK, "Connected. The access code is correct."
  if rc in (4, 5):
    return FAIL, "The printer refused the access code."
  if rc == 3:
    return FAIL, "The printer is not accepting connections right now."
  if rc in (1, 2):
    return FAIL, f"The printer rejected the connection (code {rc})."
  return FAIL, f"Could not connect (code {rc})."


def probe_spoolman(base_url, timeout=SPOOLMAN_TIMEOUT):
  """Reachability, version, and whether it has the native tag relation."""
  if not base_url:
    return [ProbeResult("spoolman_api", FAIL, "No SpoolMan URL is set.")]

  api = f"{base_url.rstrip('/')}/api/v1"
  results = []

  try:
    response = requests.get(f"{api}/info", timeout=timeout)
  except requests.Timeout:
    return [ProbeResult("spoolman_api", FAIL, "SpoolMan did not answer in time.", api)]
  except requests.RequestException as exc:
    return [ProbeResult("spoolman_api", FAIL, "Could not reach SpoolMan.", f"{api} - {exc}")]

  if response.status_code == 404:
    results.append(ProbeResult(
        "spoolman_api", FAIL,
        "Reachable, but there is no SpoolMan API at that address.",
        f"{api}/info answered 404. Check for a trailing path or a wrong port."))
    return results

  if response.status_code != 200:
    results.append(ProbeResult("spoolman_api", FAIL,
                               f"SpoolMan answered HTTP {response.status_code}.", api))
    return results

  try:
    version = response.json().get("version") or "unknown"
  except ValueError:
    return [ProbeResult("spoolman_api", FAIL,
                        "That address answered, but not with SpoolMan's API.", api)]

  results.append(ProbeResult("spoolman_api", OK, f"SpoolMan {version} is reachable.", api))

  # A capability probe, not a requirement. NFC stays optional either way, so a
  # server without the relation is a note, never a failure.
  try:
    reader = requests.get(f"{api}/tag/reader", timeout=timeout)
  except requests.RequestException as exc:
    results.append(ProbeResult("spoolman_tag_reader", WARN,
                               "Could not tell whether this SpoolMan has native tags.", str(exc)))
    return results

  if reader.status_code == 200:
    results.append(ProbeResult("spoolman_tag_reader", OK,
                               "Native tags are available.",
                               "Spools linked by other readers are recognised, "
                               "and the tags on extra.tag can be migrated."))
  elif reader.status_code == 404:
    results.append(ProbeResult("spoolman_tag_reader", WARN,
                               "This SpoolMan has no native tag relation yet.",
                               "OpenSpoolMan's own NFC workflow still works; it "
                               "keeps using the extra.tag field."))
  else:
    results.append(ProbeResult("spoolman_tag_reader", WARN,
                               "Could not tell whether this SpoolMan has native tags.",
                               f"HTTP {reader.status_code}"))

  return results


def probe_printer(printer_ip, printer_id, access_code, live_session=False, timeout=PROBE_TIMEOUT):
  """Whether the printer is reachable and the access code is accepted.

  With live_session the app's own MQTT connection is already up on exactly
  these settings, which is the common case when somebody just opens the page.
  Then nothing is opened at all - the standing connection is the answer.
  """
  if not printer_ip:
    return [ProbeResult("printer_tcp", FAIL, "No printer IP address is set.")]

  results = []

  try:
    socket.create_connection((printer_ip, MQTT_PORT), timeout=min(timeout, 3.0)).close()
  except socket.gaierror:
    return [ProbeResult("printer_tcp", FAIL, f"Cannot resolve {printer_ip}.")]
  except OSError as exc:
    return [ProbeResult("printer_tcp", FAIL,
                        f"No answer from {printer_ip} on port {MQTT_PORT}.",
                        f"{exc}. Check the address, and that LAN mode is on.")]

  results.append(ProbeResult("printer_tcp", OK, f"{printer_ip}:{MQTT_PORT} is reachable."))

  if live_session:
    results.append(ProbeResult("printer_mqtt", OK,
                               "The app's own connection to this printer is up.",
                               "No second connection was opened."))
    return results

  if not access_code:
    results.append(ProbeResult("printer_mqtt", SKIPPED, "No access code to check."))
    return results

  status, message, saw_report = _probe_mqtt(printer_ip, printer_id, access_code, timeout)
  results.append(ProbeResult("printer_mqtt", status, message))

  if status != OK:
    return results

  if not printer_id:
    results.append(ProbeResult("printer_report", SKIPPED, "No serial number to check."))
  elif saw_report:
    results.append(ProbeResult("printer_report", OK,
                               "The printer is publishing under this serial number."))
  else:
    results.append(ProbeResult("printer_report", WARN,
                               "Connected, but nothing arrived under this serial number.",
                               f"device/{printer_id}/report stayed quiet for "
                               f"{timeout:.0f} s. The serial may be wrong, or the "
                               f"printer may simply have nothing to say."))

  return results


def _probe_mqtt(printer_ip, printer_id, access_code, timeout):
  """One short-lived connection. Subscribes, never publishes."""
  import threading

  connected = threading.Event()
  reported = threading.Event()
  outcome = {"rc": None}

  def _on_connect(client, userdata, flags, rc):
    outcome["rc"] = rc
    connected.set()
    if rc == 0 and printer_id:
      client.subscribe(f"device/{printer_id}/report")

  def _on_message(client, userdata, message):
    reported.set()

  # A client id of its own, so this can never evict the app's session by
  # colliding with its name.
  client = mqtt.Client()
  client.username_pw_set("bblp", access_code)
  ssl_ctx = ssl.create_default_context()
  ssl_ctx.check_hostname = False
  ssl_ctx.verify_mode = ssl.CERT_NONE
  client.tls_set_context(ssl_ctx)
  client.tls_insecure_set(True)
  client.on_connect = _on_connect
  client.on_message = _on_message

  try:
    client.connect(printer_ip, MQTT_PORT, keepalive=int(timeout) + 5)
    client.loop_start()

    if not connected.wait(timeout):
      return FAIL, "The printer accepted the connection but never completed it.", False

    status, message = _mqtt_rc_message(outcome["rc"])
    saw_report = reported.wait(timeout) if status == OK and printer_id else False
    return status, message, saw_report
  except Exception as exc:
    log(f"Printer probe failed: {exc}")
    return FAIL, f"Could not connect: {exc}", False
  finally:
    try:
      client.loop_stop()
      client.disconnect()
    except Exception:
      pass
