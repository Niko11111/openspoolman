import requests
import time
from config import SPOOLMAN_API_URL, SPOOL_SORTING
import json
from logger import append_to_rotating_file, log

SPOOLMAN_LOG_FILE = "/home/app/logs/spoolman.log"

# (connect, read). Without a timeout a SpoolMan that accepts the connection and
# then goes quiet holds the caller forever: gunicorn runs with four threads and
# the MQTT loop calls in here as well, so a single unreachable server could take
# the whole app down with it.
SPOOLMAN_TIMEOUT = (3.05, 10)

# One session for connection reuse. Every call in this module talks to the same
# host, and the spool list is fetched on nearly every request. No retries are
# configured on it: a stale page beats a slow one, and PUT /spool/{id}/use is
# not idempotent, so retrying a request that did reach SpoolMan would deduct
# the filament twice.
_SESSION = requests.Session()


def _log_spoolman_change(action, spool_id=None, payload=None, status=None):
  parts = [action]

  if spool_id is not None:
    parts.append(f"spool_id={spool_id}")

  if status is not None:
    parts.append(f"status={status}")

  if payload is not None:
    try:
      payload_str = json.dumps(payload)
    except TypeError:
      payload_str = str(payload)
    parts.append(f"payload={payload_str}")

  try:
    append_to_rotating_file(SPOOLMAN_LOG_FILE, " | ".join(parts))
  except Exception:
    pass

def patchExtraTags(spool_id, old_extras, new_extras):
  for key, value in new_extras.items():
    old_extras[key] = value

  resp = _SESSION.patch(f"{SPOOLMAN_API_URL}/spool/{spool_id}", timeout=SPOOLMAN_TIMEOUT, json={
    "extra": old_extras
  })
  _log_spoolman_change(
    "patch_extra_tags",
    spool_id=spool_id,
    payload={"extra": old_extras},
    status=resp.status_code,
  )
  #print(resp.text)
  #print(resp.status_code)


def getSpoolById(spool_id):
  response = _SESSION.get(f"{SPOOLMAN_API_URL}/spool/{spool_id}", timeout=SPOOLMAN_TIMEOUT)
  #print(response.status_code)
  #print(response.text)
  return response.json()


def fetchSpoolList():
  if SPOOL_SORTING:
    response = _SESSION.get(f"{SPOOLMAN_API_URL}/spool?sort={SPOOL_SORTING}", timeout=SPOOLMAN_TIMEOUT)
  else:
    response = _SESSION.get(f"{SPOOLMAN_API_URL}/spool", timeout=SPOOLMAN_TIMEOUT)
    
  #print(response.status_code)
  #print(response.text)
  return response.json()

def consumeSpool(spool_id, use_weight=None, use_length=None):
  if use_weight is None and use_length is None:
    raise ValueError("use_weight or use_length is required")

  payload = {}
  if use_weight is not None:
    payload["use_weight"] = use_weight
  if use_length is not None:
    payload["use_length"] = use_length

  log(f'Consuming {payload} from spool {spool_id}')

  response = _SESSION.put(f"{SPOOLMAN_API_URL}/spool/{spool_id}/use", timeout=SPOOLMAN_TIMEOUT, json=payload)
  _log_spoolman_change(
    "consume_spool",
    spool_id=spool_id,
    payload=payload,
    status=response.status_code,
  )
  #print(response.status_code)
  #print(response.text)

def fetchSettings():
  response = _SESSION.get(f"{SPOOLMAN_API_URL}/setting/", timeout=SPOOLMAN_TIMEOUT)
  #print(response.status_code)
  #print(response.text)

  # JSON in ein Python-Dictionary laden
  data = response.json()

  # Extrahiere die Werte aus den relevanten Feldern
  extra_fields_spool = json.loads(data["extra_fields_spool"]["value"])
  extra_fields_filament = json.loads(data["extra_fields_filament"]["value"])
  base_url = data["base_url"]["value"]
  currency = data["currency"]["value"]

  settings = {}
  settings["extra_fields_spool"] = extra_fields_spool 
  settings["extra_fields_filament"] = extra_fields_filament
  settings["base_url"] = base_url.replace('"', '')
  settings["currency"] = currency.replace('"', '')

  return settings


# --- Native tag relation -----------------------------------------------------
# SpoolMan grew a tag relation in the release after 0.26.1: a spool can be
# linked to one or more tag UIDs, and any client can look a spool up by one.
# Until then the only way to bind a tag to a spool was a convention on a free
# text extra field, which no two tools spelled the same way.

# How long a settled answer from hasTagApi() is trusted.
_TAG_API_TTL_SECONDS = 300
_tag_api = {"known": None, "checked_at": 0.0}


def hasTagApi():
  """Whether this SpoolMan supports the native tag relation.

  Deliberately a capability probe and not a version check. GET /api/v1/info
  still reports 0.26.1 on a build that has the feature, so a client keying off
  the version number would never enable it. GET /tag/reader takes no
  parameters, changes nothing, and answers 200 or 404.

  Anything other than 200 or 404 - a timeout, a proxy in a bad mood, a 502 -
  is inconclusive and is never cached. Caching it as a no would switch the
  native path off for the rest of the process over one bad second.
  """
  now = time.monotonic()
  known = _tag_api["known"]
  if known is not None and now - _tag_api["checked_at"] < _TAG_API_TTL_SECONDS:
    return known

  try:
    status = _SESSION.get(f"{SPOOLMAN_API_URL}/tag/reader", timeout=SPOOLMAN_TIMEOUT).status_code
  except requests.RequestException as exc:
    log(f"Native tag probe failed ({exc}); keeping the previous answer")
    return bool(known)

  if status in (200, 404):
    _tag_api["known"] = status == 200
    _tag_api["checked_at"] = now
    return _tag_api["known"]

  log(f"Native tag probe inconclusive (HTTP {status}); not caching")
  return bool(known)


def findSpoolByTag(uid):
  """The spool a tag UID is linked to, or None.

  SpoolMan normalizes the UID itself - separators are stripped and case does
  not matter - so whatever spelling the reader used can be passed straight in.
  """
  try:
    response = _SESSION.get(f"{SPOOLMAN_API_URL}/spool", params={"tag": uid},
                            timeout=SPOOLMAN_TIMEOUT)
  except requests.RequestException as exc:
    log(f"Tag lookup for {uid} failed: {exc}")
    return None

  if response.status_code != 200:
    log(f"Tag lookup for {uid} answered HTTP {response.status_code}")
    return None

  spools = response.json()
  return spools[0] if spools else None


def linkTag(spool_id, uid, tag_format=None):
  """Link a tag UID to a spool.

  Returns (status_code, conflicting_spool_id). SpoolMan answers 409 when the
  tag already belongs to a different spool and puts that spool's id in the
  body, which is what lets the UI offer to move it instead of just failing.
  Linking a tag to the spool that already holds it is a 201 and changes
  nothing. A status of None means the request never got through.
  """
  payload = {"uid": uid}
  if tag_format:
    payload["format"] = tag_format

  try:
    response = _SESSION.post(f"{SPOOLMAN_API_URL}/spool/{spool_id}/tag",
                             json=payload, timeout=SPOOLMAN_TIMEOUT)
  except requests.RequestException as exc:
    log(f"Linking tag {uid} to spool {spool_id} failed: {exc}")
    return None, None

  _log_spoolman_change("link_tag", spool_id=spool_id, payload=payload,
                       status=response.status_code)

  conflict = None
  if response.status_code == 409:
    try:
      conflict = response.json().get("spool_id")
    except ValueError:
      conflict = None

  return response.status_code, conflict


def unlinkTag(spool_id, uid):
  """Remove a tag UID from a spool. 204 on success, 404 if it was not there."""
  try:
    response = _SESSION.delete(f"{SPOOLMAN_API_URL}/spool/{spool_id}/tag/{uid}",
                               timeout=SPOOLMAN_TIMEOUT)
  except requests.RequestException as exc:
    log(f"Unlinking tag {uid} from spool {spool_id} failed: {exc}")
    return None

  _log_spoolman_change("unlink_tag", spool_id=spool_id, payload={"uid": uid},
                       status=response.status_code)
  return response.status_code
