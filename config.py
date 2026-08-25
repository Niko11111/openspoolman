import builtins
from functools import partial
import os
from pathlib import Path
from dotenv import load_dotenv

import settings_store

# Load environment variables from config.env when present so live runs have access
# to printer and Spoolman credentials without manual exports.
load_dotenv(Path(__file__).resolve().parent / "config.env")
EXTERNAL_SPOOL_AMS_ID = 255 # don't change
EXTERNAL_SPOOL_ID = 254 #  don't change

builtins.print = partial(builtins.print, flush=True)


# Read once, at import, on purpose. The MQTT connection is built from these
# at startup and is not rebuilt while the process runs, so a value that could
# change underneath it would only pretend to be current. The settings page
# says a restart is needed, and settings_store.pending_changes() compares the
# file against RUNNING_SETTINGS below to know when.
_SAVED = settings_store.load_settings()


def _setting(name: str, default=None):
    """A managed setting: the saved file first, then the environment."""
    value = _SAVED.get(name)
    if value is None or value == "":
        value = os.getenv(name)
    return default if value is None or value == "" else value


def _env_to_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _env_to_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


BASE_URL = os.getenv("OPENSPOOLMAN_BASE_URL")  # Where will this app be accessible
PRINTER_ID = (_setting("PRINTER_ID") or "").upper()  # Printer serial number - Run init_bambulab.py
PRINTER_CODE = _setting("PRINTER_ACCESS_CODE")  # Printer access code - Run init_bambulab.py
PRINTER_IP = _setting("PRINTER_IP")  # Printer local IP address - Check wireless on printer
PRINTER_NAME = _setting("PRINTER_NAME")  # Printer name - Check wireless on printer
SPOOLMAN_BASE_URL = _setting("SPOOLMAN_BASE_URL")
SPOOLMAN_API_URL = f"{SPOOLMAN_BASE_URL}/api/v1"
AUTO_SPEND = _env_to_bool("AUTO_SPEND", False)
TRACK_LAYER_USAGE = _env_to_bool("TRACK_LAYER_USAGE", False)
SPOOL_SORTING = os.getenv(
    "SPOOL_SORTING", "filament.material:asc,filament.vendor.name:asc,filament.name:asc"
)
DISABLE_MISMATCH_WARNING = _env_to_bool("DISABLE_MISMATCH_WARNING", False)
CLEAR_ASSIGNMENT_WHEN_EMPTY = _env_to_bool("CLEAR_ASSIGNMENT_WHEN_EMPTY", False)
COLOR_DISTANCE_TOLERANCE = _env_to_int("COLOR_DISTANCE_TOLERANCE", 40)

# What this process actually started with. The settings page compares the file
# against this to tell whether a restart is still outstanding - no flag to set,
# no state to keep, and it clears itself on the next start.
RUNNING_SETTINGS = {
    "PRINTER_NAME": PRINTER_NAME,
    "PRINTER_ID": PRINTER_ID,
    "PRINTER_ACCESS_CODE": PRINTER_CODE,
    "PRINTER_IP": PRINTER_IP,
    "SPOOLMAN_BASE_URL": SPOOLMAN_BASE_URL,
}
