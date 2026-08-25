import requests
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
