"""The settings page: the first form in this app, and the first POST route.

Deliberately a plain form post rather than a JSON endpoint driven by fetch.
The app already redirects with a success_message and renders it in base.html,
a form submit is a full navigation and therefore survives the standalone-mode
shim in base.html untouched, and the whole thing is testable with a test
client and no browser.
"""

import os
from urllib.parse import urlparse

from flask import Blueprint, redirect, render_template, request, url_for

import connection_probe
import settings_store
from config import BASE_URL, RUNNING_SETTINGS
from logger import log

settings_bp = Blueprint("settings", __name__)

# Submitting the access code field empty means "leave it as it is"; this is the
# separate, explicit way to remove it.
CLEAR_SECRET_FIELD = "clear_printer_access_code"


def _origin_allowed(req):
  """Accept only a form that came from this app.

  A stateless CSRF defence. A token would need a SECRET_KEY that survives
  restarts, which is a whole new persistence problem for one form, and
  flask-wtf would be a new dependency for the same. Browsers always send
  Origin on a cross-site POST, so this is not something a hostile page can
  work around.

  A request with neither Origin nor Referer is let through: some privacy
  tooling strips both, and refusing them would break the page for those users.
  """
  origin = req.headers.get("Origin")

  if not origin and req.referrer:
    parsed = urlparse(req.referrer)
    if parsed.scheme and parsed.netloc:
      origin = f"{parsed.scheme}://{parsed.netloc}"

  if not origin:
    return True

  allowed = {req.host_url.rstrip("/")}
  if BASE_URL:
    allowed.add(BASE_URL.rstrip("/"))

  return origin.rstrip("/") in allowed


def _current_values():
  """What each managed setting would be at the next start."""
  saved = settings_store.load_settings()
  return {key: saved.get(key) or os.getenv(key) or ""
          for key in settings_store.MANAGED_KEYS}


def _submitted_values(form, current):
  """The form, with the access code carried over when it was left blank."""
  values = {}

  for key in settings_store.MANAGED_KEYS:
    if key in settings_store.SECRET_KEYS:
      continue
    values[key] = settings_store.normalize(key, form.get(key, ""))

  if form.get(CLEAR_SECRET_FIELD):
    values["PRINTER_ACCESS_CODE"] = ""
  else:
    typed = (form.get("PRINTER_ACCESS_CODE") or "").strip()
    values["PRINTER_ACCESS_CODE"] = typed or current.get("PRINTER_ACCESS_CODE", "")

  return values


def _page(values, errors=None, probes=None, message=None):
  """Everything settings.html needs, and never the access code itself."""
  return render_template(
      'settings.html',
      values={k: v for k, v in values.items() if k not in settings_store.SECRET_KEYS},
      sources={key: settings_store.source_of(key) for key in settings_store.MANAGED_KEYS},
      access_code_set=bool(values.get("PRINTER_ACCESS_CODE")),
      errors=errors or {},
      probes=probes or [],
      pending=settings_store.pending_changes(RUNNING_SETTINGS),
      success_message=message,
  )


@settings_bp.route("/settings", methods=["GET"])
def settings_page():
  """Never guarded on the MQTT connection.

  This is the page somebody opens *because* the printer settings are wrong,
  and every other route in this app turns that state into an error screen.
  It must also not touch SpoolMan on render: with no URL configured,
  SPOOLMAN_API_URL is the literal string "None/api/v1" and requests raises.
  """
  return _page(_current_values(), message=request.args.get("success_message"))


@settings_bp.route("/settings", methods=["POST"])
def settings_save():
  import app as app_module

  if not _origin_allowed(request):
    return render_template('error.html',
                           exception="That request did not come from this page."), 400

  current = _current_values()
  values = _submitted_values(request.form, current)
  action = request.form.get("action", "save")

  if action == "test":
    # Tests what is in the form, including a freshly typed access code, and
    # writes nothing.
    return _page(values, probes=_run_probes(values, current))

  if app_module.READ_ONLY_MODE:
    return render_template('error.html',
                           exception="Live read-only mode: saving settings is disabled.")

  errors = settings_store.validate(values)
  if errors:
    return _page(values, errors=errors)

  settings_store.save_settings(values)
  # Names only. The values include the access code.
  log(f"Settings saved: {sorted(k for k, v in values.items() if v)}")

  return redirect(url_for('settings.settings_page',
                          success_message="Settings saved. Restart OpenSpoolMan to apply them."))


def _run_probes(values, current):
  import app as app_module
  import mqtt_bambulab

  unchanged = all(values.get(key) == (current.get(key) or "")
                  for key in ("PRINTER_IP", "PRINTER_ID", "PRINTER_ACCESS_CODE"))
  live = False
  if unchanged and not app_module.USE_TEST_DATA:
    try:
      live = mqtt_bambulab.isMqttClientConnected()
    except Exception:
      live = False

  probes = connection_probe.probe_printer(
      values.get("PRINTER_IP"), values.get("PRINTER_ID"),
      values.get("PRINTER_ACCESS_CODE"), live_session=live)

  return probes + connection_probe.probe_spoolman(values.get("SPOOLMAN_BASE_URL"))
