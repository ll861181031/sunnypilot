"""


    magic 4B "CARM" | version u32 | group_count u32
          positions vcount×3 int16 (×1000) | normals vcount×3 int8 (×127) | indices
"""
import json
import os
import struct
import sys

import numpy as np

POS_SCALE = 1000.0
NRM_SCALE = 127.0
MAX_ABS = 8.0


def quat_to_mat(x, y, z, w):
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)


def node_matrix(n):
    if "matrix" in n:
        return np.array(n["matrix"], dtype=np.float64).reshape(4, 4).T
    m = np.eye(4)
    if "rotation" in n:
        x, y, z, w = n["rotation"]
        r = np.eye(4)
        r[:3, :3] = quat_to_mat(x, y, z, w)
        m = m @ r
    if "scale" in n:
        m = m @ np.diag([*n["scale"], 1.0])
    if "translation" in n:
        t = np.eye(4)
        t[:3, 3] = n["translation"]
        m = m @ t
    return m


class Glb:
    def __init__(self, path):
        raw = open(path, "rb").read()
        magic, _ver, _total = struct.unpack_from("<4sII", raw, 0)
        assert magic == b"glTF"
        jlen, _ = struct.unpack_from("<II", raw, 12)
        self.js = json.loads(raw[20:20 + jlen].decode("utf-8"))
        off = 20 + jlen
        blen, _ = struct.unpack_from("<II", raw, off)
        self.bin = raw[off + 8: off + 8 + blen]

    def accessor(self, idx):
        acc = self.js["accessors"][idx]
        if "bufferView" not in acc:
            return None
        bv = self.js["bufferViews"][acc["bufferView"]]
        ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[acc["type"]]
        dtype = {5120: np.int8, 5121: np.uint8, 5122: np.int16,
                 5123: np.uint16, 5125: np.uint32, 5126: np.float32}[acc["componentType"]]
        start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        isz = np.dtype(dtype).itemsize
        stride = bv.get("byteStride") or isz * ncomp
        count = acc["count"]
        need = start + stride * (count - 1) + isz * ncomp
        if count == 0 or need > len(self.bin):
            return None
        if stride == isz * ncomp:
            arr = np.frombuffer(self.bin, dtype=dtype, count=count * ncomp,
                                offset=start).reshape(count, ncomp)
        else:
            arr = np.zeros((count, ncomp), dtype=dtype)
            for i in range(count):
                arr[i] = np.frombuffer(self.bin, dtype=dtype, count=ncomp,
                                       offset=start + i * stride)
        return arr.astype(np.float64) if acc["componentType"] == 5126 else arr


def walk(glb, idx, parent, acc_list, stats):
    nodes = glb.js["nodes"]
    n = nodes[idx]
    world = parent @ node_matrix(n)
    if "mesh" in n:
        for prim in glb.js["meshes"][n["mesh"]].get("primitives", []):
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs or "indices" not in prim:
                continue
            pos = glb.accessor(attrs["POSITION"])
            idxs = glb.accessor(prim["indices"])
            if pos is None or idxs is None:
                stats["skip"] += 1
                continue
            nrm = glb.accessor(attrs["NORMAL"]) if "NORMAL" in attrs else None
            p3 = (np.hstack([pos, np.ones((len(pos), 1))]) @ world.T)[:, :3]
            if nrm is not None and len(nrm) == len(pos):
                n3 = nrm @ np.linalg.inv(world[:3, :3]).T
                ln = np.linalg.norm(n3, axis=1, keepdims=True)
                ln[ln < 1e-9] = 1.0
                n3 = n3 / ln
            else:
                n3 = np.tile([0.0, 1.0, 0.0], (len(pos), 1))

            flat = np.asarray(idxs, dtype=np.int64).reshape(-1)
            flat = flat[(flat >= 0) & (flat < len(p3))]
            flat = flat[: (len(flat) // 3) * 3].reshape(-1, 3)
            if len(flat) == 0:
                stats["skip"] += 1
                continue

            keep = np.isfinite(p3).all(axis=1) & np.isfinite(n3).all(axis=1) \
                & (np.abs(p3) < MAX_ABS).all(axis=1)
            remap = np.full(len(p3), -1, dtype=np.int64)
            good = np.flatnonzero(keep)
            remap[good] = np.arange(len(good))
            tri = remap[flat]
            tri = tri[(tri >= 0).all(axis=1)]
            if len(good) == 0 or len(tri) == 0:
                stats["skip"] += 1
                continue

            stats["prims"] += 1
            stats["v"] += len(good)
            stats["t"] += len(tri)
            acc_list.append((prim.get("material", 0), p3[good], n3[good],
                             tri.reshape(-1)))
    for c in n.get("children", []):
        walk(glb, c, world, acc_list, stats)


def main():
    glb_path = sys.argv[1] if len(sys.argv) > 1 else \
        r"D:\Desktop\openpilot\_byd_card\car_full.glb"
    out_path = sys.argv[2] if len(sys.argv) > 2 else \
        r"D:\Desktop\openpilot\_navipilot_full\app\src\main\assets\car.bin"

    glb = Glb(glb_path)
    acc = []
    stats = {"prims": 0, "v": 0, "t": 0, "skip": 0}
    for root in glb.js["scenes"][glb.js.get("scene", 0)]["nodes"]:
        walk(glb, root, np.eye(4), acc, stats)
    print(f"prims used={stats['prims']} skipped={stats['skip']} "
          f"v={stats['v']:,} t={stats['t']:,}")

    groups = {}
    for mat, p3, n3, tri in acc:
        g = groups.setdefault(mat, {"p": [], "n": [], "i": [], "base": 0})
        g["p"].append(p3)
        g["n"].append(n3)
        g["i"].append(tri + g["base"])
        g["base"] += len(p3)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    chunks = []
    for mat in sorted(groups):
        g = groups[mat]
        p = np.vstack(g["p"])
        n = np.vstack(g["n"])
        i = np.concatenate(g["i"])
        p16 = np.clip(np.round(p * POS_SCALE), -32767, 32767).astype(np.int16)
        n8 = np.clip(np.round(n * NRM_SCALE), -127, 127).astype(np.int8)
        if len(p) > 65535:
            ib, flag = i.astype(np.uint32), 1
        else:
            ib, flag = i.astype(np.uint16), 0
        chunks.append(struct.pack("<IIIII", mat, len(p), len(i), flag, 0)
                      + p16.tobytes() + n8.tobytes() + ib.tobytes())

    with open(out_path, "wb") as f:
        f.write(b"CARM")
        f.write(struct.pack("<II", 2, len(groups)))
        for c in chunks:
            f.write(c)

    raw = open(out_path, "rb").read()
    _v, gc = struct.unpack_from("<II", raw, 4)
    off, mn, mx = 12, np.array([1e9] * 3), np.array([-1e9] * 3)
    for _ in range(gc):
        _m, vc, ic, flag, _p = struct.unpack_from("<IIIII", raw, off)
        off += 20
        arr = np.frombuffer(raw, dtype=np.int16, count=vc * 3, offset=off).reshape(vc, 3) / POS_SCALE
        off += vc * 6 + vc * 3 + ic * (4 if flag else 2)
        mn = np.minimum(mn, arr.min(0))
        mx = np.maximum(mx, arr.max(0))
    print(f"groups={len(groups)}  file={os.path.getsize(out_path)/1e6:.2f} MB")
    print(f"bbox min={mn.round(2)} max={mx.round(2)} size={(mx-mn).round(2)}")
    print("期望：大约 4.8(长) × 1.9(宽) × 1.7(高)")


if __name__ == "__main__":
    main()
