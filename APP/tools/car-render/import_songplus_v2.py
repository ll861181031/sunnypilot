#!/usr/bin/env python3
"""Statically import the user's Song PLUS V2 ZIP; never execute archive scripts."""

import argparse
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SOURCE_SHA256 = "4b2ffab4cc240ad6b8d0d958da9038db5a75530480cd6c12c063d42b6cec0b87"
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
MAX_MODEL_JS_BYTES = 50 * 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_unique_member(archive, basename, max_bytes):
    matches = [entry for entry in archive.infolist()
               if not entry.is_dir() and entry.filename.replace("\\", "/").rsplit("/", 1)[-1] == basename]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {basename} in ZIP, found {len(matches)}")
    entry = matches[0]
    if entry.file_size <= 0 or entry.file_size > max_bytes:
        raise ValueError(f"Unexpected size for {basename}: {entry.file_size}")
    # ZipFile.read validates CRC; no member paths are extracted to the filesystem.
    return archive.read(entry)


def extract_glb(source_js):
    source = source_js.decode("utf-8-sig")
    matches = list(re.finditer(
        r"(?<![\w$])(?:window\.)?D3_CAR_GLB\s*=\s*(['\"])([^'\"]*)\1\s*;", source))
    if len(matches) != 1:
        raise ValueError(f"Expected one literal D3_CAR_GLB assignment, found {len(matches)}")
    try:
        result = base64.b64decode(matches[0].group(2), validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("D3_CAR_GLB is not strict base64") from error
    parse_glb(result)
    return result


def _reject_non_finite(value):
    raise ValueError(f"Non-finite GLB JSON value: {value}")


def parse_glb(raw):
    if len(raw) < 28:
        raise ValueError("GLB is truncated")
    magic, version, length = struct.unpack_from("<III", raw)
    if magic != 0x46546C67 or version != 2 or length != len(raw):
        raise ValueError("Invalid GLB magic, version, or declared length")
    chunks, cursor = [], 12
    while cursor < len(raw):
        if cursor + 8 > len(raw):
            raise ValueError("Truncated GLB chunk header")
        count, kind = struct.unpack_from("<II", raw, cursor)
        cursor += 8
        if count % 4 or cursor + count > len(raw):
            raise ValueError("Invalid GLB chunk length or alignment")
        chunks.append((kind, raw[cursor:cursor + count]))
        cursor += count
    if [kind for kind, _ in chunks] != [JSON_CHUNK, BIN_CHUNK]:
        raise ValueError("Expected one JSON chunk followed by one embedded BIN chunk")
    try:
        doc = json.loads(chunks[0][1].decode("utf-8"), parse_constant=_reject_non_finite)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid GLB JSON") from error
    if not isinstance(doc, dict) or doc.get("asset", {}).get("version") != "2.0":
        raise ValueError("Expected a glTF 2.0 asset")
    binary = chunks[1][1]
    buffers = doc.get("buffers", [])
    if len(buffers) != 1 or "uri" in buffers[0]:
        raise ValueError("Only one embedded GLB buffer is supported")
    declared = buffers[0].get("byteLength")
    if not isinstance(declared, int) or not 0 <= len(binary) - declared <= 3:
        raise ValueError("Embedded buffer size differs from its declared length")
    for view in doc.get("bufferViews", []):
        offset, size = view.get("byteOffset", 0), view.get("byteLength")
        if (view.get("buffer", 0) != 0 or not isinstance(offset, int) or
                not isinstance(size, int) or offset < 0 or size < 0 or offset + size > declared):
            raise ValueError("Out-of-bounds or external GLB bufferView")
    for texture in doc.get("images", []):
        if "uri" in texture or not isinstance(texture.get("bufferView"), int):
            raise ValueError("Only embedded image bufferViews are supported")
        if not 0 <= texture["bufferView"] < len(doc.get("bufferViews", [])):
            raise ValueError("Image references an invalid bufferView")
    return doc, binary


def encode_glb(doc, binary):
    encoded = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    encoded += b" " * (-len(encoded) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    return (struct.pack("<III", 0x46546C67, 2, 28 + len(encoded) + len(binary)) +
            struct.pack("<II", len(encoded), JSON_CHUNK) + encoded +
            struct.pack("<II", len(binary), BIN_CHUNK) + binary)


def patch_turn_material_aliases(source):
    doc, binary = parse_glb(source)
    config = doc.get("asset", {}).get("extras", {}).get("d3car")
    if not isinstance(config, dict) or not isinstance(config.get("mats"), dict):
        raise ValueError("Model has no d3car material configuration")
    material_names = [material.get("name") for material in doc.get("materials", [])]
    applied = {}
    for role, old, new in [("turnL", "turn_LFL", "turn_L"), ("turnR", "turn_LFR", "turn_R")]:
        references = config["mats"].get(role, [])
        if old in references:
            if old in material_names or material_names.count(new) != 1:
                raise ValueError(f"Cannot safely alias {old} to {new}")
            config["mats"][role] = [new if reference == old else reference for reference in references]
            applied[old] = new
    return encode_glb(doc, binary), applied


def write_if_changed(path, content):
    path = Path(path)
    if path.exists() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def geometry_stats(doc):
    primitives = [primitive for mesh in doc.get("meshes", []) for primitive in mesh.get("primitives", [])]
    accessors = doc.get("accessors", [])
    return {
        "meshes": len(doc.get("meshes", [])), "nodes": len(doc.get("nodes", [])),
        "primitives": len(primitives),
        "vertices": sum(accessors[primitive["attributes"]["POSITION"]]["count"] for primitive in primitives),
        "triangles": sum(accessors[primitive["indices"]]["count"] // 3 for primitive in primitives),
        "materials": len(doc.get("materials", [])), "embeddedImages": len(doc.get("images", [])),
    }


def import_package(archive_path, output, extract_only=False):
    archive_path, output = Path(archive_path).resolve(), Path(output).resolve()
    with zipfile.ZipFile(archive_path) as archive:
        source = extract_glb(read_unique_member(archive, "car-model.js", MAX_MODEL_JS_BYTES))
        original_license = read_unique_member(archive, "LICENSE-car.txt", 100_000).decode("utf-8-sig")
    source_hash = sha256(source)
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise ValueError("This importer is scoped to the audited Song PLUS V2 model; "
                         f"unexpected GLB SHA-256: {source_hash}")
    doc, _ = parse_glb(source)
    config = doc["asset"]["extras"]["d3car"]
    adapted, aliases = patch_turn_material_aliases(source)
    source_path, adapted_path = ROOT / "build/songplus-v2-input.glb", ROOT / "build/songplus-v2-export.glb"
    write_if_changed(source_path, source)
    write_if_changed(adapted_path, adapted)
    manifest = {
        "modelName": config.get("name"), "credit": config.get("credit"),
        "sourceArchive": {"fileName": archive_path.name, "sha256": file_sha256(archive_path)},
        "sourceGlb": {"sha256": source_hash, "bytes": len(source)},
        "exportGlb": {"sha256": sha256(adapted), "bytes": len(adapted)},
        "format": 6, "materialAliases": aliases, "geometry": geometry_stats(doc),
        "importer": "tools/car-render/import_songplus_v2.py",
        "importBehavior": [
            "Read ZIP members by unique basename; never execute archive JavaScript.",
            "Preserve the original GLB; patch only the two audited turn-material aliases in the export copy.",
            "Keep the app's white paint; ignore the webpage's red presentation preference.",
            "Export UVs, base-color images, explicit material flags, authored normals and configured parts.",
            "Use 16-bit-safe geometry chunks; approximate materials in the existing Blinn-Phong renderer.",
            "Expose head geometry as v6 role 8 for user-selected default presentation headlights and road beams; this does not report the real vehicle headlight switch.",
            "Do not synthesize unavailable headlight or rear-fog telemetry; rear fog remains unlit.",
        ],
        "license": {
            "originalFilename": "LICENSE-car.txt", "originalModelTitle": "2016 Exotic Rides W70",
            "modelTitleMismatch": True,
            "note": "The archive's license title/source URL names a different model; original text is preserved for provenance. The GLB credit declares Ddiaz Design and CC-BY-NC-SA-4.0 (non-commercial).",
        },
    }
    write_if_changed(ROOT / "build/songplus-v2-extraction.json", json_bytes(manifest))
    if extract_only:
        print(f"Extracted and validated: {source_path}")
        print(f"Adapted GLB: {adapted_path}")
        print("Extract-only: no car.bin or asset provenance files changed.")
        return manifest

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".songplus-v2-", suffix=".bin", dir=output.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        subprocess.run(["node", str(ROOT / "tools/car-render/export_car_node.js"),
                        str(adapted_path), str(temporary_path), "--v6"], check=True, cwd=ROOT)
        exported = temporary_path.read_bytes()
        if len(exported) < 12 or exported[:4] != b"CARM" or struct.unpack_from("<I", exported, 4)[0] != 6:
            raise ValueError("Exporter did not produce a CARM v6 asset; existing asset is preserved")
        if struct.unpack_from("<I", exported, 8)[0] == 0:
            raise ValueError("Exporter produced no mesh groups; existing asset is preserved")
        manifest["output"] = {"fileName": output.name, "bytes": len(exported), "sha256": sha256(exported)}
        license_text = (
            "宋 PLUS 修复版 V2 模型来源记录\n"
            f"实际 GLB 名称：{config.get('name')}\n"
            f"实际 GLB 声明：{config.get('credit')}\n"
            f"来源压缩包：{archive_path.name}\n"
            "注意：原包 LICENSE-car.txt 的车型标题及来源链接仍指向 2016 Exotic Rides W70，"
            "与本次 GLB 车型不一致；下方原文原样保留，不能据此认定两者为同一车型。\n"
            "本次适配保留几何、贴图和部件配置，修正两处转向灯材质别名，转换为 CARM v6，"
            "保留 APP 白色车漆。模型声明的非商业及相同方式共享限制随来源保留。\n\n"
            "===== 原包 LICENSE-car.txt 原文 =====\n" + original_license
        )
        temporary_path.replace(output)
        write_if_changed(output.parent / "car-model-LICENSE.txt", license_text.encode("utf-8"))
        write_if_changed(output.parent / "car-model-source.json", json_bytes(manifest))
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f"Imported: {output}")
    print(f"Source GLB SHA-256: {source_hash}")
    print(f"CARM v6 SHA-256: {manifest['output']['sha256']}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip", type=Path, help="The original Song PLUS fixed V2 ZIP")
    parser.add_argument("--output", type=Path, default=ROOT / "app/src/main/assets/car.bin")
    parser.add_argument("--extract-only", action="store_true", help="Only validate and prepare build/*.glb; leave app assets unchanged")
    args = parser.parse_args()
    try:
        import_package(args.zip, args.output, args.extract_only)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
