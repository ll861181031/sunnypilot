"""


    magic       4B   "CARM"
    version     u32  1
    group_count u32
        vcount      u32

"""
import json
import os
import struct
import sys

import numpy as np

SCALE_POS = 1000.0
SCALE_NRM = 127.0


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
        s = np.diag([*n["scale"], 1.0])
        m = m @ s
    if "translation" in n:
        t = np.eye(4)
        t[:3, 3] = n["translation"]
        m = m @ t
    return m


class Glb:
    def __init__(self, path):
        raw = open(path, "rb").read()
        magic, ver, total = struct.unpack_from("<4sII", raw, 0)
        assert magic == b"glTF", "不是 GLB"
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
        comp = acc["componentType"]
        typ = acc["type"]
        ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[typ]
        dtype = {5120: np.int8, 5121: np.uint8, 5122: np.int16,
                 5123: np.uint16, 5125: np.uint32, 5126: np.float32}[comp]
        start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        stride = bv.get("byteStride") or np.dtype(dtype).itemsize * ncomp
        count = acc["count"]
        need = start + stride * (count - 1) + np.dtype(dtype).itemsize * ncomp
        if need > len(self.bin):
            return None
        if stride == np.dtype(dtype).itemsize * ncomp:
            arr = np.frombuffer(self.bin, dtype=dtype, count=count * ncomp,
                                offset=start).reshape(count, ncomp)
            return arr.astype(np.float64) if dtype == np.float32 else arr
        out = np.zeros((count, ncomp), dtype=dtype)
        for i in range(count):
            out[i] = np.frombuffer(self.bin, dtype=dtype, count=ncomp,
                                   offset=start + i * stride)
        return out.astype(np.float64) if dtype == np.float32 else out


def walk(glb, node_idx, parent, out):
    nodes = glb.js["nodes"]
    n = nodes[node_idx]
    world = parent @ node_matrix(n)
    if "mesh" in n:
        mesh = glb.js["meshes"][n["mesh"]]
        for prim in mesh.get("primitives", []):
            attrs = prim.get("attributes", {})
            pidx = attrs.get("POSITION")
            if pidx is None:
                continue
            pos = glb.accessor(pidx)
            nrm = glb.accessor(attrs["NORMAL"]) if "NORMAL" in attrs else None
            idx = None
            if "indices" in prim:
                idx = glb.accessor(prim["indices"])
            if pos is None or len(pos) == 0:
                continue
            if idx is None:
                continue
            if len(pos) % 3 != 0 and len(np.asarray(idx).reshape(-1)) % 3 != 0:
                continue
            hp = np.hstack([pos, np.ones((len(pos), 1))]) @ world.T
            p3 = np.nan_to_num(hp[:, :3], nan=0.0, posinf=0.0, neginf=0.0)
            if nrm is not None and len(nrm) == len(pos):
                r3 = world[:3, :3]
                n3 = nrm @ np.linalg.inv(r3).T
                ln = np.linalg.norm(n3, axis=1, keepdims=True)
                ln[ln < 1e-9] = 1.0
                n3 = np.nan_to_num(n3 / ln, nan=0.0, posinf=0.0, neginf=0.0)
            else:
                n3 = np.tile([0.0, 1.0, 0.0], (len(pos), 1))
            mat = prim.get("material", 0)
            keep = np.isfinite(p3).all(axis=1) & (np.abs(p3) < 15.0).all(axis=1)
            if not keep.any():
                continue
            iarr = np.asarray(idx, dtype=np.int64).reshape(-1)
            iarr = iarr[: (len(iarr) // 3) * 3].reshape(-1, 3)
            remap = np.full(len(p3), -1, dtype=np.int64)
            newidx = np.flatnonzero(keep)
            remap[newidx] = np.arange(len(newidx))
            tri = remap[iarr]
            tri = tri[(tri >= 0).all(axis=1)]
            if len(tri) == 0:
                continue
            out.append((mat, p3[keep], n3[keep], tri.reshape(-1)))
    for c in n.get("children", []):
        walk(glb, c, world, out)


def main():
    glb_path = sys.argv[1] if len(sys.argv) > 1 else r"D:\Desktop\openpilot\_byd_card\car_full.glb"
    out_path = sys.argv[2] if len(sys.argv) > 2 else r"D:\Desktop\openpilot\_navipilot_full\app\src\main\assets\car.bin"

    glb = Glb(glb_path)
    out = []
    scene = glb.js["scenes"][glb.js.get("scene", 0)]
    for root in scene["nodes"]:
        walk(glb, root, np.eye(4), out)

    groups = {}
    for mat, p3, n3, idx in out:
        g = groups.setdefault(mat, {"p": [], "n": [], "i": [], "base": 0})
        tri = np.asarray(idx, dtype=np.int64).reshape(-1)
        tri = tri[: (len(tri) // 3) * 3]
        g["p"].append(p3)
        g["n"].append(n3)
        g["i"].append(tri + g["base"])
        g["base"] += len(p3)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    chunks = []
    total_v = total_t = 0
    for mat in sorted(groups):
        g = groups[mat]
        p = np.vstack(g["p"])
        n = np.vstack(g["n"])
        i = np.concatenate(g["i"])
        p16 = np.clip(p * SCALE_POS, -32767, 32767).astype(np.int16)
        n8 = np.clip(n * SCALE_NRM, -127, 127).astype(np.int8)
        if len(p) > 65535:
            i32 = i.astype(np.uint32)
            flag = 1
        else:
            i32 = i.astype(np.uint16)
            flag = 0
        chunks.append(struct.pack("<IIIII", mat, len(p), len(i), flag, 0)
                      + p16.tobytes() + n8.tobytes() + i32.tobytes())
        total_v += len(p)
        total_t += len(i) // 3

    with open(out_path, "wb") as f:
        f.write(b"CARM")
        f.write(struct.pack("<II", 1, len(groups)))
        for c in chunks:
            f.write(c)

    size = os.path.getsize(out_path)
    print(f"groups={len(groups)}  vertices={total_v:,}  triangles={total_t:,}")
    print(f"wrote {out_path}  {size/1e6:.2f} MB")
    for mat in sorted(groups):
        g = groups[mat]
        print(f"  mat {mat:2d}: v={g['base']:6d}")


if __name__ == "__main__":
    main()
