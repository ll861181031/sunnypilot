"""Complete desktop font subsets from installed fonts without replacing originals.

Uses fontTools from the existing desktop tools extra. Generated fonts stay in
the user's local data directory; device fonts and UI font sizes are unchanged.
"""
import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

from openpilot.common.hardware import PC
from openpilot.system.ui.lib.application import (
  EXTRA_FONT_CHARS, FONT_DIR, LOCAL_CJK_FONT, LOCAL_COMPLETE_FONT_DIR, FontWeight, font_codepoints,
)
from openpilot.system.ui.lib.multilang import FONT_FALLBACK_LANGUAGES, TRANSLATIONS_DIR, multilang


def build_font(primary: Path, donors: list[Path], codepoints: list[int], output: Path) -> dict:
  """Retain primary outlines/metrics and fill only missing Unicode mappings."""
  with ExitStack() as stack:
    sources = [primary] + [path for path in donors if path != primary]
    fonts = [stack.enter_context(TTFont(path)) for path in sources]
    cmaps = [font.getBestCmap() or {} for font in fonts]
    assignments = {cp: next((i for i, cmap in enumerate(cmaps) if cp in cmap and cmap[cp] != '.notdef'), None)
                   for cp in codepoints}
    missing = [f"U+{cp:04X}" for cp, source in assignments.items() if source is None]
    if missing:
      raise ValueError(f"No installed outline font covers: {', '.join(missing)}")

    base = fonts[0]
    upem = base['head'].unitsPerEm
    order = ['.notdef'] + [f'u{cp:06X}' for cp in codepoints]
    cmap = dict(zip(codepoints, order[1:], strict=True))
    charstrings, metrics = {}, {}
    glyph_sets = [font.getGlyphSet() for font in fonts]

    def copy_glyph(name, source, source_name):
      scale = upem / fonts[source]['head'].unitsPerEm
      width, lsb = fonts[source]['hmtx'][source_name]
      width, lsb = round(width * scale), round(lsb * scale)
      pen = T2CharStringPen(width, glyph_sets[source])
      glyph_sets[source][source_name].draw(TransformPen(pen, (scale, 0, 0, scale, 0, 0)))
      charstrings[name] = pen.getCharString()
      metrics[name] = (width, lsb)

    copy_glyph('.notdef', 0, '.notdef')
    for cp, source in assignments.items():
      copy_glyph(cmap[cp], source, cmaps[source][cp])

    family = f"SP Desktop {primary.stem}"
    ps_name = 'SPDesktop-' + primary.stem.replace(' ', '')
    builder = FontBuilder(upem, isTTF=False)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    builder.setupCFF(ps_name, {'FullName': family, 'FamilyName': family, 'Weight': 'Regular'}, charstrings, {})
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=base['hhea'].ascent, descent=base['hhea'].descent, lineGap=base['hhea'].lineGap)
    builder.setupNameTable({'familyName': family, 'styleName': 'Regular', 'fullName': family,
                            'uniqueFontIdentifier': ps_name, 'psName': ps_name})
    os2 = base['OS/2']
    builder.setupOS2(sTypoAscender=os2.sTypoAscender, sTypoDescender=os2.sTypoDescender,
                     sTypoLineGap=os2.sTypoLineGap, usWinAscent=os2.usWinAscent, usWinDescent=os2.usWinDescent,
                     usWeightClass=os2.usWeightClass, usWidthClass=os2.usWidthClass)
    builder.setupPost(italicAngle=base['post'].italicAngle, isFixedPitch=base['post'].isFixedPitch)
    builder.save(output)
    return {'source': str(primary), 'requested_glyphs': len(codepoints),
            'fallback_codepoints': [cp for cp, source in assignments.items() if source != 0],
            'donor_fonts': sorted({str(sources[source]) for source in assignments.values() if source != 0})}


def font_requests() -> list[tuple[Path, list[int]]]:
  root = Path(str(FONT_DIR))
  base_chars = set(map(chr, range(32, 127))) | set(EXTRA_FONT_CHARS)
  unifont_chars = set(base_chars)
  for language, code in multilang.languages.items():
    unifont_chars.update(language)
    if code not in FONT_FALLBACK_LANGUAGES:
      base_chars.update(TRANSLATIONS_DIR.joinpath(f'app_{code}.po').read_text(encoding='utf-8'))
  requests = [(root / weight, font_codepoints(unifont_chars if weight == FontWeight.UNIFONT else base_chars))
              for weight in FontWeight]
  if LOCAL_CJK_FONT.is_file():
    chinese_chars = set(map(chr, range(32, 127))) | set(EXTRA_FONT_CHARS)
    for code in ('zh-CHS', 'zh-CHT'):
      chinese_chars.update(TRANSLATIONS_DIR.joinpath(f'app_{code}.po').read_text(encoding='utf-8'))
    requests.append((LOCAL_CJK_FONT, font_codepoints(chinese_chars)))
  return requests


def prepare_fonts(output_dir: Path = LOCAL_COMPLETE_FONT_DIR) -> dict:
  requests = font_requests()
  donors = [path for path in [
    Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
    Path('/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf'),
    Path('/usr/share/fonts/truetype/freefont/FreeSerif.ttf'),
    Path(str(FONT_DIR)) / 'unifont.otf',
  ] if path.is_file()]
  inputs = sorted({source for source, _ in requests} | set(donors) | {Path(__file__)})
  signature = hashlib.sha256(json.dumps({str(source): cps for source, cps in requests}, sort_keys=True).encode())
  for source in inputs:
    signature.update(str(source).encode())
    signature.update(source.read_bytes())
  fingerprint = signature.hexdigest()
  manifest_path = output_dir / 'manifest.json'
  if manifest_path.is_file():
    try:
      manifest = json.loads(manifest_path.read_text())
      if manifest['fingerprint'] == fingerprint and all(
        (output_dir / name).is_file() and hashlib.sha256((output_dir / name).read_bytes()).hexdigest() == item['sha256']
        for name, item in manifest['fonts'].items()
      ):
        return {'rebuilt': False, 'fonts': len(manifest['fonts'])}
    except (KeyError, ValueError):
      pass

  output_dir.mkdir(parents=True, exist_ok=True)
  manifest = {'fingerprint': fingerprint, 'fonts': {}}
  # Finish every font before installing any of them; failures keep the previous set.
  with tempfile.TemporaryDirectory(dir=output_dir) as temporary:
    staging = Path(temporary)
    for source, cps in requests:
      target = staging / source.with_suffix('.otf').name
      item = build_font(source, donors, cps, target)
      item['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
      manifest['fonts'][target.name] = item
    (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    for target in staging.iterdir():
      os.replace(target, output_dir / target.name)
  return {'rebuilt': True, 'fonts': len(manifest['fonts'])}


if __name__ == '__main__' and PC:
  result = prepare_fonts()
  if result['rebuilt']:
    print(f"已补齐 {result['fonts']} 个桌面字体子集，原始字体保持不变。")
