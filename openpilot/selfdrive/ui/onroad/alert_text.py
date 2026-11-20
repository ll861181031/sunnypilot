import re

from openpilot.common.camera_config import driver_camera_enabled
from openpilot.system.ui.lib.multilang import tr


def camera_alert_text(alert_type: str, text1: str, text2: str) -> tuple[str, str]:
  # Only clarify the permanent notice for an explicitly disabled cabin camera.
  # Keep mixed/road faults, engagement refusals and takeover alerts unchanged.
  if (not driver_camera_enabled() and alert_type == "cameraMalfunction/permanent"
      and text1 == "Camera Malfunction" and text2 == "cabinCamera"):
    return tr("Driver Camera Disabled"), tr("Driver monitoring unavailable; assistance disabled")
  return text1, text2


MOUNT_PART_TEMPLATES = {
  'left': 'Turn camera left {}°',
  'right': 'Turn camera right {}°',
  'up': 'tilt up {}°',
  'down': 'tilt down {}°',
}
MOUNT_PART_RE = re.compile(r'(Turn camera (?:left|right)|tilt (?:up|down)) ([0-9]+\.[0-9])°')


def mount_alert_text(text: str) -> str:
  """Rebuild the mount guidance from translated catalog templates.

  selfdrived composes the sentence from English literals, so the whole string is
  not a catalog key. Splitting it back into parts keeps every Chinese character
  inside the catalog, which is what the font preparation step scans: composing
  Chinese server-side would render as '?' on the aiming guide.
  """
  if '°' not in text:
    return text
  parts = []
  templates = []
  for label, value in MOUNT_PART_RE.findall(text):
    axis = label.split()[-1]
    template = MOUNT_PART_TEMPLATES[axis]
    templates.append(template)
    parts.append(tr(template).format(value))
  if not parts:
    return text
  # Only rebuild when the sentence is nothing but the direction hints; an alert
  # that wraps them in more wording is handled by the dynamic patterns below.
  remainder = MOUNT_PART_RE.sub('', text)
  if remainder.replace(',', '').strip():
    return text
  # Keep the separator in the source language when nothing is translated.
  separator = '，' if any(tr(template) != template for template in templates) else ', '
  return separator.join(parts)


# selfdrived formats these alerts before sending them, so only the surrounding
# wording can be matched. Templates stay in the catalog for the same glyph
# reason as the mount guidance.
DYNAMIC_ALERT_PATTERNS = (
  (re.compile(r'Drive above (.+) to engage'), 'Drive above {} to engage'),
  (re.compile(r'Steer Assist Unavailable Below (.+)'), 'Steer Assist Unavailable Below {}'),
  (re.compile(r'Drive Above (.+)'), 'Drive Above {}'),
  (re.compile(r'Speed Error: (.+) m/s'), 'Speed Error: {} m/s'),
  (re.compile(r'Calibration Invalid: (.+)'), 'Calibration Invalid: {}'),
  (re.compile(r'Angle offset too high \(Offset: (.+)\)'), 'Angle offset too high (Offset: {})'),
  (re.compile(r'Check tires, pressure, or alignment \(Factor: (.+)\)'),
   'Check tires, pressure, or alignment (Factor: {})'),
)


def localize_dynamic_alert_text(text: str) -> str:
  """Translate alerts whose values are spliced in server-side."""
  for pattern, template in DYNAMIC_ALERT_PATTERNS:
    match = pattern.fullmatch(text)
    if match is not None:
      # The nested value may itself be a mount guidance sentence.
      return tr(template).format(*[mount_alert_text(group) for group in match.groups()])
  return text


def localize_alert_text(text: str) -> str:
  return mount_alert_text(localize_dynamic_alert_text(text))
