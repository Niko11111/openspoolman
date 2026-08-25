"""Settings a person can change in the browser, kept in a file.

Everything here used to be an environment variable and nothing else, which
means a printer serial or an access code could only be corrected by editing
config.env on the host and restarting. The file this module owns sits in front
of those variables: what it holds wins, what it does not hold still comes from
the environment, and the defaults built into config.py are the last word.

The file is not config.env. That one is handed to Docker through env_file:,
which Docker reads on the host when the container starts - it is never inside
the container, so the app cannot write it even in principle.

Nothing in here imports config, because config imports this.
"""

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from logger import log

# Point this somewhere else to keep settings out of the way, or set it to an
# empty string to read no file at all - which is what the test suite does, so
# a developer's own settings cannot leak into a test run.
SETTINGS_FILE_ENV = "OPENSPOOLMAN_SETTINGS_FILE"
DEFAULT_FILENAME = "settings.json"

# What the settings page may write. Everything else stays an environment
# variable, because it changes behaviour rather than describing this install.
MANAGED_KEYS = (
    "PRINTER_NAME",
    "PRINTER_ID",
    "PRINTER_ACCESS_CODE",
    "PRINTER_IP",
    "SPOOLMAN_BASE_URL",
)

# Never rendered back into a page, never written to a log.
SECRET_KEYS = frozenset({"PRINTER_ACCESS_CODE"})

_SERIAL = re.compile(r"^[A-Z0-9]{8,20}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
                       r"(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$")

# Keyed on (path, mtime) so a file edited from outside is picked up, and so the
# restart banner can ask on every request without re-reading the file.
_cache = {"key": None, "values": {}}


def settings_path():
  """Where the settings file lives, or None when reading one is switched off."""
  override = os.getenv(SETTINGS_FILE_ENV)
  if override is not None:
    return Path(override).expanduser() if override else None

  return Path(__file__).resolve().parent / "data" / DEFAULT_FILENAME


def load_settings():
  """The saved settings, or an empty dict.

  Never raises. A settings file that is missing, unreadable or not valid JSON
  must not stop the app from starting - the whole point of the page it feeds is
  to be reachable when the configuration is wrong.
  """
  path = settings_path()
  if path is None:
    return {}

  try:
    stat = path.stat()
  except OSError:
    _cache["key"], _cache["values"] = None, {}
    return {}

  key = (str(path), stat.st_mtime_ns, stat.st_size)
  if _cache["key"] == key:
    return _cache["values"]

  try:
    values = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError) as exc:
    log(f"Ignoring the settings file at {path}: {exc}")
    values = {}

  if not isinstance(values, dict):
    log(f"Ignoring the settings file at {path}: it is not an object")
    values = {}

  values = {str(k): str(v) for k, v in values.items() if v is not None}
  _cache["key"], _cache["values"] = key, values
  return values


def save_settings(values):
  """Write the settings, replacing what was there. Returns the path."""
  path = settings_path()
  if path is None:
    raise RuntimeError(f"{SETTINGS_FILE_ENV} is set to an empty value, so nothing can be saved")

  path.parent.mkdir(parents=True, exist_ok=True)
  payload = json.dumps({k: v for k, v in values.items() if v not in (None, "")},
                       indent=2, sort_keys=True)

  # Written beside the target and moved into place, so a crash halfway through
  # leaves the previous settings rather than half of the new ones.
  handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".json")
  try:
    with os.fdopen(handle, "w", encoding="utf-8") as target:
      target.write(payload + "\n")
    os.chmod(temporary, 0o600)  # it holds the printer access code
    os.replace(temporary, path)
  except BaseException:
    try:
      os.unlink(temporary)
    except OSError:
      pass
    raise

  _cache["key"] = None
  return path


def normalize(key, raw):
  """One spelling per value, so the same setting compares equal to itself."""
  value = (raw or "").strip()

  if key == "PRINTER_ID":
    return value.upper()

  if key == "SPOOLMAN_BASE_URL":
    # A trailing slash turns every request into a double slash. Fixing it
    # quietly is kinder than explaining it.
    return value.rstrip("/")

  return value


def validate(values):
  """Which of the given settings are wrong, and why. Empty means all fine."""
  errors = {}

  serial = values.get("PRINTER_ID")
  if serial and not _SERIAL.match(serial):
    errors["PRINTER_ID"] = "A printer serial is 8 to 20 letters and digits."

  host = values.get("PRINTER_IP")
  if host:
    try:
      ipaddress.ip_address(host)
    except ValueError:
      if not _HOSTNAME.match(host):
        errors["PRINTER_IP"] = "Enter an IP address or a hostname."

  url = values.get("SPOOLMAN_BASE_URL")
  if url:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
      errors["SPOOLMAN_BASE_URL"] = "Enter a full URL, for example http://192.168.1.10:7912"

  name = values.get("PRINTER_NAME")
  if name and len(name) > 64:
    errors["PRINTER_NAME"] = "Keep the name under 65 characters."

  return errors


def redact(values):
  """The same settings with every secret replaced, safe to log or render."""
  return {key: ("***" if key in SECRET_KEYS and value else value)
          for key, value in values.items()}


def source_of(key):
  """Where the value in force comes from: the file, the environment, nowhere."""
  if load_settings().get(key):
    return "file"
  if os.getenv(key):
    return "environment"
  return "unset"


def pending_changes(running):
  """Which saved settings differ from what the process started with.

  Compared after normalize(), because config.py upper-cases the serial on the
  way in - without that a lower-case serial in the file would look like a
  change forever and the restart banner would never go away.
  """
  saved = load_settings()
  changed = []

  for key, current in running.items():
    if key not in saved:
      continue
    if normalize(key, saved[key]) != normalize(key, current or ""):
      changed.append(key)

  return sorted(changed)


def restart_required(running):
  """Whether the running process is out of date with what is saved."""
  return bool(pending_changes(running))
