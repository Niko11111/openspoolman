"""AMS source contract, version 1.

A small, read-mostly view of the printer's AMS for clients that are not a
browser, such as a filament scale. It answers in the shape the Bambu printer
itself reports (ams[].tray[], vt_tray[], tray_now), which other servers with an
AMS view already hand out, plus the one thing only this app knows: which
SpoolMan spool sits in which tray.

Kept apart from api_routes on purpose:

- No {success, data} envelope. The HTTP status says whether it worked.
- Printers are numbered from 1. This instance watches one printer, so 1 is
  its id; the serial stays out of the URL.
- "active" is never sent. Its meaning differs between servers ("assigned"
  here, "feeding right now" on the printer), so the client derives the
  feeding tray from tray_now and the assignment from spool_id.
- humidity is a percentage from humidity_raw, or null. Bambu's 1-5 humidity
  index runs the other way round (high means dry), so it is never passed on
  in its place. An AMS Lite has no sensor: humidity and temp are null there.
- GET /api/v1/ams-source says which version this is and what it can do, so a
  client offers only what works.
"""

import json
import traceback
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

import mqtt_bambulab
import spoolman_client
import spoolman_service
from api_routes import READ_ONLY_MODE
from __version__ import __version__
from config import EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID, PRINTER_NAME

ams_source_bp = Blueprint("ams_source", __name__, url_prefix="/api/v1/ams-source")

AMS_SOURCE_VERSION = 1
AMS_SOURCE_SERVER_NAME = "OpenSpoolMan"
# Assigning also sends material and colour to the printer (setActiveSpool).
AMS_SOURCE_CAPABILITIES = ["assign", "unassign", "sets_printer_filament"]
AMS_SOURCE_PRINTER_ID = 1

# The printer's "nothing loaded" for tray_now.
TRAY_NOW_NONE = 255

# The model names this app detects, in the module codes the printer itself
# uses, so a client reads one spelling whichever server it talks to.
MODULE_TYPES = {
    "AMS": "ams",
    "AMS 2 Pro": "n3f",
    "AMS HT": "n3s",
    "AMS Lite": "ams_lite",
}


def _error(status: int, message: str):
  return jsonify({"error": message}), status


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
  try:
    return int(value)
  except (TypeError, ValueError):
    return default


def _as_float(value: Any) -> Optional[float]:
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _decoded_extra(value: Any) -> Any:
  """SpoolMan keeps extra values JSON encoded; a text arrives quoted."""
  if isinstance(value, str):
    try:
      return json.loads(value)
    except ValueError:
      return value
  return value


def _spools_by_tray(spools: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
  """Every spool that claims a tray, keyed by the tray's uid."""
  by_tray: Dict[str, Dict[str, Any]] = {}
  for spool in spools or []:
    tray_uid = _decoded_extra((spool.get("extra") or {}).get("active_tray"))
    if tray_uid:
      by_tray[str(tray_uid)] = spool
  return by_tray


def _tray_exists(tray: Dict[str, Any], ams_id: int, exist_bits: Optional[int]) -> bool:
  """Whether a spool is physically in the tray.

  tray_exist_bits is the printer's own answer, one bit per tray numbered
  ams_id * 4 + tray. Without it, a tray that names a material holds one.
  """
  tray_id = _as_int(tray.get("id"), 0)
  if exist_bits is not None and ams_id < 4:
    return bool(exist_bits & (1 << (ams_id * 4 + tray_id)))
  return bool(tray.get("tray_type"))


def _serialize_tray(tray: Dict[str, Any], ams_id: int, by_tray: Dict[str, Dict[str, Any]],
                    exist_bits: Optional[int]) -> Dict[str, Any]:
  tray_id = _as_int(tray.get("id"), 0)
  spool = by_tray.get(spoolman_service.trayUid(ams_id, tray_id))
  remain_g = None
  if spool and spool.get("remaining_weight") is not None:
    remain_g = int(round(float(spool["remaining_weight"])))

  return {
      "id": tray_id,
      "tray_color": tray.get("tray_color") or "",
      "tray_type": tray.get("tray_type") or "",
      "tray_sub_brands": tray.get("tray_sub_brands") or "",
      "remain": _as_int(tray.get("remain"), -1),
      "exists": _tray_exists(tray, ams_id, exist_bits),
      "spool_id": spool.get("id") if spool else None,
      "remain_g": remain_g,
  }


def _serialize_unit(ams: Dict[str, Any], by_tray: Dict[str, Dict[str, Any]],
                    models: Dict[Any, str], exist_bits: Optional[int]) -> Dict[str, Any]:
  ams_id = _as_int(ams.get("id"), 0)
  trays = ams.get("tray") or []
  humidity = _as_float(ams.get("humidity_raw"))
  model = models.get(ams_id) or models.get(str(ams_id))
  module_type = MODULE_TYPES.get(model or "", "")
  # The AMS Lite reports zeros from a sensor it does not have.
  has_sensor = module_type != MODULE_TYPES["AMS Lite"]
  temp = _as_float(ams.get("temp")) if has_sensor else None

  return {
      "id": ams_id,
      "humidity": int(round(humidity)) if humidity is not None and has_sensor else None,
      "temp": temp,
      "module_type": module_type,
      "is_ams_ht": ams_id >= 128 or len(trays) == 1,
      "dry_time": _as_int(ams.get("dry_time"), 0),
      "tray": [_serialize_tray(t, ams_id, by_tray, exist_bits) for t in trays],
  }


def build_status(config: Dict[str, Any], spools: List[Dict[str, Any]], name: str,
                 connected: bool) -> Dict[str, Any]:
  """The status answer from the last report and the spool list.

  Pure, so it can be tested without a printer or a SpoolMan.
  """
  by_tray = _spools_by_tray(spools)
  status = config.get("status") or {}
  models = (config.get("get_version") or {}).get("models_by_id") or {}

  exist_bits = None
  raw_bits = status.get("tray_exist_bits")
  if raw_bits not in (None, ""):
    try:
      exist_bits = int(str(raw_bits), 16)
    except ValueError:
      exist_bits = None

  units = [_serialize_unit(a, by_tray, models, exist_bits) for a in config.get("ams") or []]

  # One holder on most printers, two on a dual nozzle machine; the printer
  # sends one object or a list depending on the model.
  vt = config.get("vt_tray") or []
  if isinstance(vt, dict):
    vt = [vt]
  vt_trays = []
  for tray in vt:
    if not isinstance(tray, dict):
      continue
    tray = dict(tray)
    tray.setdefault("id", EXTERNAL_SPOOL_ID)
    vt_trays.append(_serialize_tray(tray, EXTERNAL_SPOOL_AMS_ID, by_tray, None))

  return {
      "version": AMS_SOURCE_VERSION,
      "id": AMS_SOURCE_PRINTER_ID,
      "name": name,
      "connected": connected,
      "ams_exists": bool(units),
      "tray_now": _as_int(status.get("tray_now"), TRAY_NOW_NONE),
      "gcode_state": status.get("gcode_state"),
      "mc_percent": _as_int(status.get("mc_percent")),
      "ams": units,
      "vt_tray": vt_trays,
  }


def _printer_name() -> str:
  return PRINTER_NAME or mqtt_bambulab.getPrinterModel().get("devicename") or "Printer"


def _known_printer(printer_id: int) -> bool:
  return printer_id == AMS_SOURCE_PRINTER_ID


def _tray_from_body(body: Dict[str, Any]):
  ams_id = _as_int(body.get("ams_id"))
  tray_id = _as_int(body.get("tray_id"))
  if ams_id is None or tray_id is None or ams_id < 0 or tray_id < 0:
    return None, None
  return ams_id, tray_id


@ams_source_bp.route("", methods=["GET"])
def ams_source_discovery():
  return jsonify({
      "version": AMS_SOURCE_VERSION,
      "server": {"name": AMS_SOURCE_SERVER_NAME, "version": __version__},
      "capabilities": AMS_SOURCE_CAPABILITIES,
  })


@ams_source_bp.route("/printers", methods=["GET"])
def ams_source_printers():
  try:
    return jsonify([{
        "id": AMS_SOURCE_PRINTER_ID,
        "name": _printer_name(),
        "connected": bool(mqtt_bambulab.isMqttClientConnected()),
    }])
  except Exception as exc:
    traceback.print_exc()
    return _error(500, f"Failed to list printers: {exc}")


@ams_source_bp.route("/printers/<int:printer_id>/status", methods=["GET"])
def ams_source_status(printer_id: int):
  if not _known_printer(printer_id):
    return _error(404, f"Printer {printer_id} not found")
  try:
    payload = build_status(
        mqtt_bambulab.getLastAMSConfig() or {},
        mqtt_bambulab.fetchSpools(),
        _printer_name(),
        bool(mqtt_bambulab.isMqttClientConnected()),
    )
    return jsonify(payload)
  except Exception as exc:
    traceback.print_exc()
    return _error(500, f"Failed to read the AMS: {exc}")


@ams_source_bp.route("/printers/<int:printer_id>/assign", methods=["POST"])
def ams_source_assign(printer_id: int):
  if not _known_printer(printer_id):
    return _error(404, f"Printer {printer_id} not found")
  if READ_ONLY_MODE:
    return _error(403, "Live read-only mode: assigning spools to trays is disabled.")

  body = request.get_json(silent=True) or {}
  spool_id = _as_int(body.get("spool_id"))
  ams_id, tray_id = _tray_from_body(body)
  if not spool_id or spool_id <= 0 or ams_id is None:
    return _error(400, "spool_id, ams_id and tray_id are required.")
  if not mqtt_bambulab.isMqttClientConnected():
    return _error(503, "MQTT is disconnected. Is the printer online?")

  try:
    spool = spoolman_client.getSpoolById(spool_id)
  except Exception as exc:
    traceback.print_exc()
    return _error(502, f"Failed to fetch spool {spool_id}: {exc}")
  if not spool or spool.get("id") is None:
    return _error(404, f"Spool {spool_id} not found")

  try:
    mqtt_bambulab.setActiveTray(spool_id, spool.get("extra"), ams_id, tray_id)
    # The same path as the tray page, so the printer gets the filament
    # setting for the tray as well.
    from app import setActiveSpool  # Local import: app imports this module.
    setActiveSpool(ams_id, tray_id, spool)
  except Exception as exc:
    traceback.print_exc()
    return _error(500, f"Failed to assign spool {spool_id}: {exc}")

  return jsonify({"printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id,
                  "spool_id": spool_id})


@ams_source_bp.route("/printers/<int:printer_id>/unassign", methods=["POST"])
def ams_source_unassign(printer_id: int):
  if not _known_printer(printer_id):
    return _error(404, f"Printer {printer_id} not found")
  if READ_ONLY_MODE:
    return _error(403, "Live read-only mode: assigning spools to trays is disabled.")

  body = request.get_json(silent=True) or {}
  ams_id, tray_id = _tray_from_body(body)
  if ams_id is None:
    return _error(400, "ams_id and tray_id are required.")

  try:
    # Fresh rather than cached: the claim may have been made a moment ago.
    target = spoolman_service.trayUid(ams_id, tray_id)
    cleared = None
    for spool in spoolman_service.fetchSpools(cached=False):
      extras = spool.get("extra") or {}
      if _decoded_extra(extras.get("active_tray")) == target:
        spoolman_client.patchExtraTags(spool["id"], extras, {"active_tray": json.dumps("")})
        cleared = spool["id"]
  except Exception as exc:
    traceback.print_exc()
    return _error(500, f"Failed to unassign the tray: {exc}")

  # An empty tray is already what was asked for, so that is not an error.
  return jsonify({"printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id,
                  "spool_id": cleared})
