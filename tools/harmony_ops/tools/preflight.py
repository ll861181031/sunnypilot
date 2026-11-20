#!/usr/bin/env python3
"""Offline pre-flight checks for the harmony_ops HarmonyOS project.

This box has no DevEco Studio, no hvigor, no ohpm and no HarmonyOS SDK, so the
project cannot be compiled here. What *can* be checked without a toolchain is
everything that fails late and confusingly inside DevEco:

  * every .json5 parses (comments stripped first, since JSON5 is not JSON)
  * every ``$r('app.string.x')`` / ``$string:x`` / ``$media:x`` / ``$color:x`` /
    ``$profile:x`` reference resolves to a resource that actually exists
  * every page in ``resources/base/profile/main_pages.json`` has a file, and no
    page file is missing from that list (the classic "white screen" mistake)
  * every ability ``srcEntry`` and every relative import resolves to a file
  * ``mainElement`` names an ability that exists
  * each PNG is a real PNG
  * each .ets is brace/paren balanced, and avoids the constructs ArkTS forbids
    outright (``any``, ``eval``, ``require``, ``Object.assign``)

It is deliberately heuristic: a clean run is necessary but not sufficient, and
the report says so. Run it from the repo root:

    /data/sunnypilot/.venv/bin/python tools/harmony_ops/tools/preflight.py
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path


def strip_json5_comments(text: str) -> str:
  """Remove // and /* */ comments, leaving string contents alone.

  JSON5 also allows unquoted keys and trailing commas, but the DevEco templates
  this project follows quote every key and avoid trailing commas, so a comment
  strip is enough to hand the text to json.loads. A file that still fails to
  parse genuinely needs a look.
  """
  out: list[str] = []
  i = 0
  n = len(text)
  quote: str | None = None
  while i < n:
    ch = text[i]
    if quote is not None:
      out.append(ch)
      if ch == "\\" and i + 1 < n:
        out.append(text[i + 1])
        i += 2
        continue
      if ch == quote:
        quote = None
      i += 1
      continue
    if ch in "\"'":
      quote = ch
      out.append(ch)
      i += 1
      continue
    if text.startswith("//", i):
      while i < n and text[i] != "\n":
        i += 1
      continue
    if text.startswith("/*", i):
      end = text.find("*/", i + 2)
      i = n if end == -1 else end + 2
      continue
    out.append(ch)
    i += 1
  return "".join(out)


def load_json5(path: Path) -> object:
  return json.loads(strip_json5_comments(path.read_text(encoding="utf-8")))


def collect_resources(root: Path) -> dict[str, set[str]]:
  """Gather resource names by kind from AppScope and entry."""
  found: dict[str, set[str]] = {"string": set(), "color": set(), "media": set(), "profile": set()}

  for base in (root / "AppScope/resources", root / "entry/src/main/resources"):
    if not base.is_dir():
      continue
    for element in base.rglob("element/*.json"):
      kind = element.stem  # string.json -> "string"
      if kind not in found:
        continue
      data = load_json5(element)
      for entry in data.get(kind, []):  # type: ignore[union-attr]
        found[kind].add(entry["name"])
    for media in base.rglob("media/*"):
      if media.is_file():
        found["media"].add(media.stem)
    for profile in base.rglob("profile/*.json"):
      found["profile"].add(profile.stem)

  return found


_R_REF = re.compile(r"\$r\(\s*['\"]app\.(string|color|media|profile|plural)\.([A-Za-z0-9_]+)['\"]")
_DOLLAR_REF = re.compile(r"\$(string|color|media|profile|plural):([A-Za-z0-9_]+)")
_REL_IMPORT = re.compile(r"""from\s+['"](\.[^'"]+)['"]""")

# `import { A, B } from './x'` — the names are what the build resolves, and a wrong
# one is invisible until ArkTS compiles the file.
_IMPORT_NAMES = re.compile(r"""import\s*\{([^}]*)\}\s*from\s*['"](\.[^'"]+)['"]""")

# Every form an .ets file in this project uses to export a symbol.
_EXPORT_DECL = (r"export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
                r"(?:struct|class|interface|function|const|let|var|enum|type)\s+{name}\b")
_ANY_TYPE = re.compile(r":\s*any\b|<any>|\bas\s+any\b")
_FORBIDDEN = {
  r"\beval\s*\(": "eval() is not allowed in ArkTS",
  r"\brequire\s*\(": "require() is not available in ArkTS; use import",
  r"\bObject\.assign\s*\(": "Object.assign is not supported in ArkTS",
  r"\bwith\s*\(": "with is not allowed in ArkTS",
}


def strip_for_balance(text: str) -> str:
  """Drop comments and string/template contents, keeping code structure.

  A regex cannot do this: '//' inside 'http://host' is not a comment, and a brace
  inside a string is not a brace. Counting on the raw text therefore reported
  balance errors for correct files. This walks the text once, tracks whether it
  is inside a quote, and removes string contents entirely (template literals
  included, whose ${} bodies are balanced code anyway), leaving pure structure.
  """
  out: list[str] = []
  i = 0
  n = len(text)
  quote: str | None = None
  while i < n:
    ch = text[i]
    if quote is not None:
      if ch == "\\":
        i += 2
        continue
      if ch == quote:
        out.append(quote)
        out.append(quote)
        quote = None
        i += 1
        continue
      i += 1
      continue
    if ch == "/" and i + 1 < n and text[i + 1] == "/":
      while i < n and text[i] != "\n":
        i += 1
      continue
    if ch == "/" and i + 1 < n and text[i + 1] == "*":
      close = text.find("*/", i + 2)
      i = n if close == -1 else close + 2
      continue
    if ch in "\"'`":
      quote = ch
      out.append(ch)
      i += 1
      continue
    out.append(ch)
    i += 1
  return "".join(out)


def check_ets_syntax(path: Path) -> list[str]:
  """Brace/paren balance plus an ArkTS-forbidden-construct sweep."""
  problems: list[str] = []
  code = strip_for_balance(path.read_text(encoding="utf-8"))

  for opener, closer, label in (("{", "}", "braces"), ("(", ")", "parens"), ("[", "]", "brackets")):
    if code.count(opener) != code.count(closer):
      problems.append(
        f"{path}: unbalanced {label} ({code.count(opener)} '{opener}' vs {code.count(closer)} '{closer}')")

  for pattern, message in _FORBIDDEN.items():
    for match in re.finditer(pattern, code):
      line = code[: match.start()].count("\n") + 1
      problems.append(f"{path}:{line}: {message}")

  for match in _ANY_TYPE.finditer(code):
    line = code[: match.start()].count("\n") + 1
    problems.append(f"{path}:{line}: 'any' is not allowed in ArkTS; declare a concrete type")

  return problems


def resolve_relative_import(from_file: Path, spec: str) -> Path | None:
  """Resolve './x' or '../y/z' to a real file, trying the extensions ArkTS uses."""
  target = (from_file.parent / spec).resolve()
  for candidate in (target, target.with_suffix(".ets"), target.with_suffix(".ts")):
    if candidate.is_file():
      return candidate
  for index in ("index.ets", "index.ts"):
    if (target / index).is_file():
      return target / index
  return None


def png_size(path: Path) -> tuple[int, int] | None:
  """Read width/height straight from the IHDR chunk; no image library needed."""
  raw = path.read_bytes()[:33]
  if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
    return None
  return struct.unpack(">II", raw[16:24])


def run(root: Path) -> int:
  errors: list[str] = []
  warnings: list[str] = []
  notes: list[str] = []

  json_files = [p for p in sorted(list(root.rglob("*.json5")) + list(root.rglob("*.json")))
                if "node_modules" not in p.parts]

  parsed: dict[Path, object] = {}
  for path in json_files:
    try:
      parsed[path] = load_json5(path)
    except Exception as exc:  # noqa: BLE001 - the point is to report it
      errors.append(f"{path.relative_to(root)}: does not parse: {exc}")
  notes.append(f"parsed {len(parsed)}/{len(json_files)} json/json5 files")

  try:
    resources = collect_resources(root)
  except Exception as exc:  # noqa: BLE001
    errors.append(f"resource collection failed: {exc}")
    resources = {"string": set(), "color": set(), "media": set(), "profile": set()}

  notes.append("resources: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(resources.items())))

  ets_files = sorted(p for p in root.rglob("*.ets") if "node_modules" not in p.parts)
  notes.append(f"scanned {len(ets_files)} .ets files")

  # 1. resource references
  for path in ets_files + [p for p in json_files if p.suffix == ".json5"]:
    text = path.read_text(encoding="utf-8")
    for kind, name in _R_REF.findall(text) + _DOLLAR_REF.findall(text):
      table = "media" if kind == "plural" else kind
      if name not in resources.get(table, set()):
        errors.append(f"{path.relative_to(root)}: reference '{kind}:{name}' is not defined")

  # 2. pages listed vs pages present
  pages_profile = root / "entry/src/main/resources/base/profile/main_pages.json"
  pages_dir = root / "entry/src/main/ets/pages"
  page_files = {p.stem for p in pages_dir.glob("*.ets")} if pages_dir.is_dir() else set()
  if pages_profile.is_file():
    listed = {str(src).split("/")[-1] for src in parsed.get(pages_profile, {}).get("src", [])}  # type: ignore[union-attr]
    for missing in sorted(listed - page_files):
      errors.append(f"main_pages.json lists '{missing}' but pages/{missing}.ets does not exist")
    for orphan in sorted(page_files - listed):
      errors.append(f"pages/{orphan}.ets exists but is not listed in main_pages.json")
  else:
    errors.append("entry/src/main/resources/base/profile/main_pages.json is missing")

  # 3. module.json5 wiring
  module_json = root / "entry/src/main/module.json5"
  module = parsed.get(module_json, {}).get("module", {})  # type: ignore[union-attr]
  ability_names: set[str | None] = set()
  for ability in module.get("abilities", []):
    ability_names.add(ability.get("name"))
    src = ability.get("srcEntry")
    if not src:
      errors.append(f"ability {ability.get('name')}: no srcEntry")
    elif not (root / "entry/src/main" / src.lstrip("./")).is_file():
      errors.append(f"ability {ability.get('name')}: srcEntry '{src}' does not exist")
    for key in ("startWindowIcon", "icon"):
      ref = ability.get(key)
      if isinstance(ref, str) and ref.startswith("$media:") and ref[7:] not in resources["media"]:
        errors.append(f"ability {ability.get('name')}: {key} '{ref}' is not a known media resource")
    bg = ability.get("startWindowBackground")
    if isinstance(bg, str) and bg.startswith("$color:") and bg[7:] not in resources["color"]:
      errors.append(f"ability {ability.get('name')}: startWindowBackground '{bg}' is not a known color")
  if module.get("mainElement") not in ability_names:
    errors.append(f"module.mainElement '{module.get('mainElement')}' matches no ability {sorted(a for a in ability_names if a)}")
  notes.append(f"abilities: {sorted(a for a in ability_names if a)}")

  # 4. app.json5 icon/label
  app = parsed.get(root / "AppScope/app.json5", {}).get("app", {})  # type: ignore[union-attr]
  for key in ("icon", "label"):
    ref = app.get(key)
    if isinstance(ref, str) and ":" in ref:
      kind, name = ref.lstrip("$").split(":", 1)
      if name not in resources.get(kind, set()):
        errors.append(f"AppScope/app.json5: {key} '{ref}' is not a known {kind} resource")

  # 5. permissions for a networking app
  perms = {p.get("name") for p in module.get("requestPermissions", [])}
  if "ohos.permission.INTERNET" not in perms:
    errors.append("module.json5 does not request ohos.permission.INTERNET; every HTTP call would fail")
  if "ohos.permission.GET_NETWORK_INFO" not in perms:
    warnings.append("module.json5 does not request ohos.permission.GET_NETWORK_INFO; "
                    "reading the local IP may fail")
  notes.append(f"permissions: {sorted(p for p in perms if p)}")

  # 6. cleartext-HTTP config present
  meta = {m.get("name"): m.get("resource") for m in module.get("metadata", [])}
  if "ohos.net.network_security_config" not in meta:
    errors.append("module.json5 has no ohos.net.network_security_config metadata; "
                  "HTTP (not HTTPS) LAN requests would be blocked")
  else:
    ref = str(meta["ohos.net.network_security_config"])
    if ref.startswith("$profile:") and ref[9:] not in resources["profile"]:
      errors.append(f"network security config '{ref}' is not a known profile resource")

  # 7. relative imports
  for path in ets_files:
    for spec in _REL_IMPORT.findall(path.read_text(encoding="utf-8")):
      if resolve_relative_import(path, spec) is None:
        errors.append(f"{path.relative_to(root)}: import '{spec}' does not resolve to a file")

  # 7b. every imported name is exported by the file it comes from
  #
  # The path check above passes while the name is wrong, and ArkTS only reports a
  # missing export when it compiles — which is exactly what cannot be run here.
  # Checked names: a renamed struct, a typo, or an interface that was never exported.
  for path in ets_files:
    source = path.read_text(encoding="utf-8")
    for names_blob, spec in _IMPORT_NAMES.findall(source):
      target = resolve_relative_import(path, spec)
      if target is None:
        continue  # already reported above
      target_text = target.read_text(encoding="utf-8")
      for raw in names_blob.split(","):
        name = raw.strip().split(" as ")[0].strip()
        if not name:
          continue
        if re.search(_EXPORT_DECL.format(name=re.escape(name)), target_text):
          continue
        if re.search(r"export\s*\{[^}]*\b" + re.escape(name) + r"\b", target_text):
          continue
        if re.search(r"export\s+\*", target_text):
          continue
        errors.append(f"{path.relative_to(root)}: imports '{name}' from '{spec}', "
                      f"but {target.relative_to(root)} does not export it")

  # 8. per-file syntax sweep
  for path in ets_files:
    errors.extend(check_ets_syntax(path))

  # 9. png sanity
  for png in sorted(root.rglob("*.png")):
    size = png_size(png)
    if size is None:
      errors.append(f"{png.relative_to(root)}: not a valid PNG")
    else:
      notes.append(f"{png.relative_to(root)}: {size[0]}x{size[1]}")

  # --- report ---
  print("harmony_ops pre-flight")
  print("=" * 60)
  for note in notes:
    print(f"  . {note}")
  print()
  if warnings:
    print(f"{len(warnings)} warning(s):")
    for warning in warnings:
      print(f"  ! {warning}")
    print()
  if errors:
    print(f"{len(errors)} error(s):")
    for error in errors:
      print(f"  x {error}")
    print()
    print("FAIL - fix the above before opening the project in DevEco Studio.")
    return 1
  print("PASS - no structural problems found.")
  print()
  print("What this does NOT prove: ArkTS semantics, API signatures, the hvigor build")
  print("graph, signing, or that the UI renders. Only a real build in DevEco Studio")
  print("(and a run on a device) can show those.")
  return 0


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
  args = parser.parse_args()
  return run(args.project_root.resolve())


if __name__ == "__main__":
  sys.exit(main())
