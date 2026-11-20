import base64
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "import_songplus_v2", Path(__file__).with_name("import_songplus_v2.py"))
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)


def fixture_glb():
    doc = {
        "asset": {"version": "2.0", "extras": {"d3car": {
            "name": "fixture", "credit": "fixture credit",
            "mats": {"turnL": ["turn_LFL", "turn_tail_L"],
                     "turnR": ["turn_LFR", "turn_tail_R"], "paint": ["CarPaint"]},
            "parts": {"doorFL": {"meshes": [0, 1]}},
        }}},
        "materials": [{"name": n} for n in
                      ["turn_L", "turn_R", "turn_tail_L", "turn_tail_R", "CarPaint"]],
        "buffers": [{"byteLength": 4}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 4}],
    }
    raw = json.dumps(doc).encode("utf-8")
    raw += b" " * (-len(raw) % 4)
    return struct.pack("<III", 0x46546C67, 2, 28 + len(raw) + 4) + \
        struct.pack("<II", len(raw), 0x4E4F534A) + raw + \
        struct.pack("<II", 4, 0x004E4942) + b"\x00\x01\x02\x03"


class ImportSongplusTests(unittest.TestCase):
    def package_fixture(self, folder):
        root = Path(folder)
        archive_path = root / "user-model.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("folder/car-model.js", b"window.D3_CAR_GLB = '" + base64.b64encode(fixture_glb()) + b"';")
            archive.writestr("folder/LICENSE-car.txt", "original license text")
        output = root / "assets/car.bin"
        output.parent.mkdir()
        output.write_bytes(b"old asset")
        (output.parent / "car-model-source.json").write_bytes(b"old provenance")
        return root, archive_path, output

    def test_extracts_literal_without_evaluating_javascript(self):
        source = b"throw new Error('MUST NOT RUN');\nwindow.D3_CAR_GLB = '" + \
            base64.b64encode(fixture_glb()) + b"';\nprocess.exit(99);"
        self.assertEqual(importer.extract_glb(source), fixture_glb())

    def test_rejects_invalid_or_ambiguous_base64(self):
        for source in [b"window.D3_CAR_GLB = '!!!!';", b"D3_CAR_GLB = 'AA=='; D3_CAR_GLB = 'AA==';"]:
            with self.assertRaises(ValueError):
                importer.extract_glb(source)

    def test_rejects_truncated_or_misreported_glb(self):
        raw = bytearray(fixture_glb())
        struct.pack_into("<I", raw, 8, len(raw) + 4)
        for invalid in [bytes(raw), fixture_glb()[:-1], b"not a GLB"]:
            with self.assertRaises(ValueError):
                importer.parse_glb(invalid)

    def test_rewrites_only_known_material_aliases_and_preserves_binary(self):
        original = fixture_glb()
        patched, aliases = importer.patch_turn_material_aliases(original)
        before, before_bin = importer.parse_glb(original)
        after, after_bin = importer.parse_glb(patched)
        self.assertEqual(before_bin, after_bin)
        self.assertEqual(after["asset"]["extras"]["d3car"]["mats"]["turnL"], ["turn_L", "turn_tail_L"])
        self.assertEqual(after["asset"]["extras"]["d3car"]["mats"]["turnR"], ["turn_R", "turn_tail_R"])
        self.assertEqual(aliases, {"turn_LFL": "turn_L", "turn_LFR": "turn_R"})
        before["asset"]["extras"]["d3car"]["mats"]["turnL"][0] = "turn_L"
        before["asset"]["extras"]["d3car"]["mats"]["turnR"][0] = "turn_R"
        self.assertEqual(before, after)

    def test_aliases_require_actual_unambiguous_target_materials(self):
        doc, binary = importer.parse_glb(fixture_glb())
        doc["materials"] = [m for m in doc["materials"] if m["name"] != "turn_L"]
        with self.assertRaises(ValueError):
            importer.patch_turn_material_aliases(importer.encode_glb(doc, binary))

    def test_zip_members_are_selected_by_unique_basename(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("nested/car-model.js", b"first")
                archive.writestr("other/car-model.js", b"second")
            with zipfile.ZipFile(path) as archive:
                with self.assertRaises(ValueError):
                    importer.read_unique_member(archive, "car-model.js", 100)

    def test_external_buffers_and_out_of_bounds_views_are_rejected(self):
        for update in ["external", "bounds"]:
            doc, binary = importer.parse_glb(fixture_glb())
            if update == "external":
                doc["buffers"][0]["uri"] = "outside.bin"
            else:
                doc["bufferViews"][0]["byteLength"] = 8
            with self.assertRaises(ValueError):
                importer.parse_glb(importer.encode_glb(doc, binary))

    def test_identical_source_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.glb"
            importer.write_if_changed(path, fixture_glb())
            before = path.stat().st_mtime_ns
            importer.write_if_changed(path, fixture_glb())
            self.assertEqual(before, path.stat().st_mtime_ns)

    def test_extract_only_preserves_active_asset_and_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            root, archive, output = self.package_fixture(folder)
            with mock.patch.object(importer, "ROOT", root), \
                    mock.patch.object(importer, "EXPECTED_SOURCE_SHA256", importer.sha256(fixture_glb())), \
                    mock.patch.object(importer.subprocess, "run") as run:
                importer.import_package(archive, output, extract_only=True)
            run.assert_not_called()
            self.assertEqual(output.read_bytes(), b"old asset")
            self.assertEqual((output.parent / "car-model-source.json").read_bytes(), b"old provenance")
            self.assertTrue((root / "build/songplus-v2-export.glb").exists())

    def test_failed_export_preserves_active_asset_and_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            root, archive, output = self.package_fixture(folder)
            with mock.patch.object(importer, "ROOT", root), \
                    mock.patch.object(importer, "EXPECTED_SOURCE_SHA256", importer.sha256(fixture_glb())), \
                    mock.patch.object(importer.subprocess, "run", side_effect=importer.subprocess.CalledProcessError(1, ["node"])):
                with self.assertRaises(importer.subprocess.CalledProcessError):
                    importer.import_package(archive, output)
            self.assertEqual(output.read_bytes(), b"old asset")
            self.assertEqual((output.parent / "car-model-source.json").read_bytes(), b"old provenance")
            self.assertEqual(list(output.parent.glob(".songplus-v2-*")), [])

    def test_old_exporter_output_is_rejected_without_relabeling_old_asset(self):
        with tempfile.TemporaryDirectory() as folder:
            root, archive, output = self.package_fixture(folder)

            def legacy_export(command, **kwargs):
                self.assertIsInstance(command, list)
                self.assertEqual(command[-1], "--v6")
                Path(command[3]).write_bytes(b"CARM" + struct.pack("<II", 5, 1))

            with mock.patch.object(importer, "ROOT", root), \
                    mock.patch.object(importer, "EXPECTED_SOURCE_SHA256", importer.sha256(fixture_glb())), \
                    mock.patch.object(importer.subprocess, "run", side_effect=legacy_export):
                with self.assertRaisesRegex(ValueError, "CARM v6"):
                    importer.import_package(archive, output)
            self.assertEqual(output.read_bytes(), b"old asset")
            self.assertEqual((output.parent / "car-model-source.json").read_bytes(), b"old provenance")


if __name__ == "__main__":
    unittest.main()
