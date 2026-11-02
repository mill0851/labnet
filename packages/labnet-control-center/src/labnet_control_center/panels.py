"""Which control panel a device page loads: the driver package's own, a built-in one, or the generic one.

A driver package ships a panel for its device type by registering, in the
entry-point group ``labnet.control_center.panels``, the package that holds
``<device_type>.js``::

    [project.entry-points."labnet.control_center.panels"]
    acme_laser = "acme_drivers.panels"

The file registers ``LabNet.panels["<device_type>"]``, exactly like the
built-in panels in ``static/js/panels/``, and the Control Center serves it at
``/api/panels/<device_type>.js``. A driver's panel wins over a built-in one;
a type with neither gets ``panels/generic.js`` (the script box only).

The panel runs in the Control Center's pages with the signed-in person's
session, so it is code the Control Center trusts like the driver package
itself: install only driver packages you would install on the Control Center
anyway (it imports their contracts).
"""

from __future__ import annotations

import functools
import logging
from importlib import resources
from importlib.metadata import entry_points
from pathlib import Path

from labnet.identifiers import LABNET_IDENTIFIER_REGEX


LOGGER = logging.getLogger(__name__)
GROUP = "labnet.control_center.panels"
BUILT_IN = Path(__file__).resolve().parent / "static" / "js" / "panels"


@functools.cache
def _registered() -> dict[str, str]:
    """Device type -> the package holding its panel (a seam for tests)."""

    found: dict[str, str] = {}
    for entry in entry_points(group=GROUP):
        if LABNET_IDENTIFIER_REGEX.fullmatch(entry.name) is None:
            LOGGER.warning("Ignoring the panel entry %r: not a device type.", entry.name)
            continue
        if entry.name in found and found[entry.name] != entry.value:
            LOGGER.warning("Two packages ship a panel for %r; using %s.", entry.name, found[entry.name])
            continue
        found[entry.name] = entry.value
    return found


def driver_panel(device_type: str) -> str | None:
    """The source of the panel a driver package ships for ``device_type``, or None."""

    package = _registered().get(device_type)
    if package is None:
        return None
    try:
        return resources.files(package).joinpath(f"{device_type}.js").read_text(encoding="utf-8")
    except (ImportError, OSError) as exc:
        LOGGER.warning("The panel for %r in %s could not be read: %s", device_type, package, exc)
        return None


def panel_for(device_type: str) -> tuple[str, str]:
    """``(name, url)`` of the panel a device page loads; it registers ``LabNet.panels[name]``."""

    if device_type in _registered():
        return device_type, f"/api/panels/{device_type}.js"
    name = device_type if (BUILT_IN / f"{device_type}.js").is_file() else "generic"
    return name, f"/static/js/panels/{name}.js"


__all__ = ["GROUP", "driver_panel", "panel_for"]
