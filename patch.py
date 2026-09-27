#!/usr/bin/env python3
"""Apply this repository's fixes to an unmodified copy of the original web port.

Usage:
    python patch.py <path-to-web-port> [--check]

Only the Python standard library is needed (Python 3.8+). Inputs are verified by SHA-256
before anything is written, results are verified afterwards, and running it twice is
harmless (already-patched files are skipped).
"""
import argparse
import base64
import bisect
import hashlib
import io
import json
import lzma
import os
import shutil
import struct
import sys
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "patch")


# ---------------------------------------------------------------- helpers
def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class PatchError(Exception):
    pass


# ---------------------------------------------------------------- LZ4 block format (pure Python)
def lz4_decompress(src, size):
    dst = bytearray()
    p, n = 0, len(src)
    while p < n:
        t = src[p]; p += 1
        ll = t >> 4
        if ll == 15:
            while True:
                b = src[p]; p += 1; ll += b
                if b != 255:
                    break
        dst += src[p:p + ll]; p += ll
        if p >= n:
            break
        off = src[p] | src[p + 1] << 8; p += 2
        ml = t & 15
        if ml == 15:
            while True:
                b = src[p]; p += 1; ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start + ml]
        else:  # overlapping copy
            for k in range(ml):
                dst.append(dst[start + k])
    if len(dst) != size:
        raise PatchError("LZ4 block decoded to an unexpected size")
    return bytes(dst)


def lz4_compress(src):
    """Greedy LZ4 block compressor (valid LZ4 block format, not size-optimal)."""
    n = len(src)
    out = bytearray()

    def emit(lit, off, ml):
        ll = len(lit)
        m = ml - 4 if ml else 0
        out.append((min(ll, 15) << 4) | (min(m, 15) if ml else 0))
        if ll >= 15:
            r = ll - 15
            while r >= 255:
                out.append(255); r -= 255
            out.append(r)
        out.extend(lit)
        if not ml:
            return
        out.extend((off & 255, off >> 8))
        if m >= 15:
            r = m - 15
            while r >= 255:
                out.append(255); r -= 255
            out.append(r)

    table = {}
    anchor = i = 0
    mflimit = n - 12          # a match may not start in the last 12 bytes
    while i < mflimit:
        key = src[i:i + 4]
        cand = table.get(key, -1)
        table[key] = i
        if cand >= 0 and i - cand <= 0xFFFF:
            ml = 4
            maxml = n - 5 - i  # the last 5 bytes must stay literals
            while ml < maxml and src[cand + ml] == src[i + ml]:
                ml += 1
            emit(src[anchor:i], i - cand, ml)
            i += ml
            anchor = i
            if i - 2 >= 0 and i - 2 < mflimit:
                table[src[i - 2:i + 2]] = i - 2
        else:
            i += 1
    emit(src[anchor:], 0, 0)
    return bytes(out)


# ---------------------------------------------------------------- UnityFS bundles
class UnityFS:
    """Minimal UnityFS reader/writer: edit bytes inside nodes, re-encode only the touched blocks."""

    def __init__(self, data):
        b = data
        if not b.startswith(b"UnityFS\0"):
            raise PatchError("not a UnityFS file")
        self.version = struct.unpack_from(">I", b, 8)[0]
        p = 12
        for _ in range(2):
            p = b.index(b"\0", p) + 1
        self.hdr = p
        self.size, csz, usz, self.flags = struct.unpack_from(">qIII", b, p)
        p += 20
        if self.version >= 7:
            p = (p + 15) & ~15
        self.bi_pos = p
        if self.flags & 0x80:
            raise PatchError("blocks info at end of file is not supported")
        raw = b[p:p + csz]
        self.bi = bytearray(self._dec(raw, usz, self.flags & 0x3F))
        ds = p + csz
        if self.flags & 0x200:
            ds = (ds + 15) & ~15
        self.data_start = ds
        n = struct.unpack_from(">i", self.bi, 16)[0]
        self.blocks = [list(struct.unpack_from(">IIH", self.bi, 20 + i * 10)) for i in range(n)]
        q = 20 + n * 10
        nc = struct.unpack_from(">i", self.bi, q)[0]; q += 4
        self.nodes = {}
        for _ in range(nc):
            off, sz, fl = struct.unpack_from(">qqI", self.bi, q); q += 20
            e = self.bi.index(b"\0", q)
            self.nodes[self.bi[q:e].decode()] = (off, sz)
            q = e + 1
        self.raw = b
        self.ustart, self.cstart = [], []
        u, c = 0, ds
        for us, cs, fl in self.blocks:
            self.ustart.append(u); self.cstart.append(c); u += us; c += cs
        self.total = u
        self.dec = {}

    @staticmethod
    def _dec(raw, usz, comp):
        if comp == 0:
            return raw
        if comp in (2, 3):
            return lz4_decompress(raw, usz)
        raise PatchError(f"unsupported compression {comp}")

    def _block(self, i):
        if i not in self.dec:
            us, cs, fl = self.blocks[i]
            self.dec[i] = bytearray(self._dec(self.raw[self.cstart[i]:self.cstart[i] + cs], us, fl & 0x3F))
        return self.dec[i]

    def read(self, pos, length):
        out = bytearray()
        i = bisect.bisect_right(self.ustart, pos) - 1
        while length > 0:
            blk = self._block(i); k = pos - self.ustart[i]
            take = min(length, len(blk) - k)
            out += blk[k:k + take]; pos += take; length -= take; i += 1
        return bytes(out)

    def write(self, pos, data):
        i = bisect.bisect_right(self.ustart, pos) - 1; j = 0
        while j < len(data):
            blk = self._block(i); k = pos - self.ustart[i]
            take = min(len(data) - j, len(blk) - k)
            blk[k:k + take] = data[j:j + take]; pos += take; j += take; i += 1

    def node_pos(self, node, offset):
        if node not in self.nodes:
            raise PatchError(f"node {node} not found")
        return self.nodes[node][0] + offset

    def build(self):
        body = bytearray()
        bi = bytearray(self.bi)
        for i, (us, cs, fl) in enumerate(self.blocks):
            if i in self.dec:
                comp = fl & 0x3F
                enc = bytes(self.dec[i]) if comp == 0 else lz4_compress(bytes(self.dec[i]))
                body += enc
                struct.pack_into(">IIH", bi, 20 + i * 10, us, len(enc), fl)
            else:
                body += self.raw[self.cstart[i]:self.cstart[i] + cs]
        comp = self.flags & 0x3F
        bi_enc = bytes(bi) if comp == 0 else lz4_compress(bytes(bi))
        out = bytearray(self.raw[:self.hdr + 20])
        if self.version >= 7:
            while len(out) % 16:
                out.append(0)
        out += bi_enc
        if self.flags & 0x200:
            while len(out) % 16:
                out.append(0)
        out += body
        struct.pack_into(">qIII", out, self.hdr, len(out), len(bi_enc), len(bi), self.flags)
        return bytes(out)


def replace_catalog_crc(data, path, old_crc, new_crc):
    """Swap a bundle CRC inside an Addressables catalog: binary (catalog.bin, uint32 LE) or
    JSON (catalog.json, UTF-16 JSON inside base64 m_ExtraDataString; padded to keep offsets)."""
    if not path.endswith(".json"):
        data = bytearray(data)
        ob, nb = struct.pack("<I", old_crc), struct.pack("<I", new_crc)
        if data.count(nb) == 1 and data.count(ob) == 0:
            return bytes(data)
        if data.count(ob) != 1:
            raise PatchError(f"{path}: CRC {old_crc:#010x} not found exactly once")
        i = data.find(ob)
        data[i:i + 4] = nb
        return bytes(data)
    txt = data.decode("utf-8")
    b64 = json.loads(txt)["m_ExtraDataString"]
    ex = base64.b64decode(b64)
    old_num, new_num = str(old_crc), str(new_crc)
    if len(new_num) > len(old_num):
        raise PatchError("new CRC has more digits than the old one")
    old_s = ('"m_Crc":' + old_num).encode("utf-16-le")
    new_s = ('"m_Crc":' + new_num + " " * (len(old_num) - len(new_num))).encode("utf-16-le")
    if ex.count(new_s) == 1 and ex.count(old_s) == 0:
        return data
    if ex.count(old_s) != 1 or txt.count(b64) != 1:
        raise PatchError(f"{path}: CRC {old_crc} not found exactly once")
    return txt.replace(b64, base64.b64encode(ex.replace(old_s, new_s)).decode()).encode("utf-8")


def crc32_update(old_crc, total_len, changes):
    """New CRC32 of a stream of total_len bytes after same-length changes [(pos, old, new)].
    Uses CRC linearity: crc(a) ^ crc(b) == crc(a ^ b) ^ crc(zeros)."""
    diff = bytearray(total_len)
    for pos, old, new in changes:
        diff[pos:pos + len(old)] = bytes(x ^ y for x, y in zip(old, new))
    return old_crc ^ zlib.crc32(diff) ^ zlib.crc32(bytes(total_len))


# ---------------------------------------------------------------- UnityWebData (Unity <= 2019 .data packs)
def read_webdata(blob):
    if not blob.startswith(b"UnityWebData1.0\0"):
        raise PatchError("not a UnityWebData pack")
    hs = struct.unpack_from("<I", blob, 16)[0]
    p, entries = 20, []
    while p < hs:
        off, size, nl = struct.unpack_from("<III", blob, p)
        entries.append([p, off, size, blob[p + 12:p + 12 + nl].decode()])
        p += 12 + nl
    return hs, entries


def write_webdata(blob, hs, entries, changed):
    """Rebuild a pack with some entries replaced ({name: new content}); offsets are recomputed."""
    blobs = {e[3]: blob[e[1]:e[1] + e[2]] for e in entries}
    blobs.update(changed)
    out = bytearray(blob[:hs]); cur = hs
    for hp, off, size, nm in sorted(entries, key=lambda e: e[1]):
        struct.pack_into("<II", out, hp, cur, len(blobs[nm])); cur += len(blobs[nm])
    for hp, off, size, nm in sorted(entries, key=lambda e: e[1]):
        out += blobs[nm]
    return bytes(out)


class SplitReader(io.RawIOBase):
    """Read-only seekable view over several files concatenated (split zip archives)."""

    def __init__(self, paths):
        self.paths, self.starts, self.pos, self.handles = paths, [0], 0, {}
        for p in paths:
            self.starts.append(self.starts[-1] + os.path.getsize(p))

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = [off, self.pos + off, self.starts[-1] + off][whence]
        return self.pos

    def readinto(self, b):
        n, done = len(b), 0
        while done < n and self.pos < self.starts[-1]:
            i = bisect.bisect_right(self.starts, self.pos) - 1
            f = self.handles.get(i) or self.handles.setdefault(i, open(self.paths[i], "rb"))
            f.seek(self.pos - self.starts[i])
            chunk = f.read(min(n - done, self.starts[i + 1] - self.pos))
            b[done:done + len(chunk)] = chunk; done += len(chunk); self.pos += len(chunk)
        return done

    def close(self):
        for f in self.handles.values():
            f.close()
        super().close()


# ---------------------------------------------------------------- patcher
class Patcher:
    def __init__(self, root, check_only):
        self.root, self.check_only = root, check_only
        self.m = json.load(open(os.path.join(DATA, "manifest.json"), encoding="utf-8"))
        sub = self.m["upstream"].get("subdir")  # the game files live in a subfolder
        if sub and os.path.isdir(os.path.join(root, sub)):
            self.root = os.path.join(root, sub)
        dp = os.path.join(DATA, "delta.bin.xz")
        self.delta = lzma.decompress(open(dp, "rb").read()) if os.path.exists(dp) else b""
        self.crlf = set()     # text files checked out with CRLF line endings (git core.autocrlf)
        self.pending = set()  # files --check could not look at yet because they come out of an archive
        # final hash of each file, so an extracted file that was patched afterwards counts as done
        self.final = {s["file"]: s["dst"] for s in self.m["steps"] if "file" in s and "dst" in s}
        self.renamed = {s["from"]: s["to"] for s in self.m["steps"] if s["op"] == "rename"}

    def p(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def new_bytes(self, e):
        if "new" in e:
            return bytes.fromhex(e["new"])
        off, n = e["delta"]
        return self.delta[off:off + n]

    def check_old(self, where, got, e):
        ok = got == bytes.fromhex(e["old"]) if "old" in e else sha256_bytes(got) == e["old_sha256"]
        if not ok:
            raise PatchError(f"{where}: bytes at the patch location are not the expected original ones")

    def file_hash(self, f, want):
        path = self.p(f)
        if not os.path.exists(path) and f in self.renamed:
            path = self.p(self.renamed[f])  # patched and renamed by an earlier run
        if not os.path.exists(path):
            return None
        h = sha256_file(path)
        if h not in want and os.path.getsize(path) < 64 << 20:
            data = open(path, "rb").read()
            lf = data.replace(b"\r\n", b"\n")
            if lf != data and sha256_bytes(lf) in want:
                self.crlf.add(f)
                return sha256_bytes(lf)
        return h

    def status(self, files, src, dst):
        hs = [self.file_hash(f, (s, d)) for f, s, d in zip(files, src, dst)]
        if hs == dst:
            return "done"
        if hs == src:
            return "todo"
        bad = [f for f, h, s in zip(files, hs, src) if h != s]
        raise PatchError(f"{bad[0]}: {'missing' if not os.path.exists(self.p(bad[0])) else 'unexpected version'}"
                         f" - use an unmodified copy of the original web port (version {self.m['upstream']['commit'][:12]})")

    # -- steps
    def step_extract_zip(self, s):
        dest = self.p(s["dest"])
        todo = {n: h for n, h in s["files"].items()
                if not (os.path.exists(os.path.join(dest, n))
                        and sha256_file(os.path.join(dest, n)) in (h, self.final.get(s["dest"] + "/" + n)))}
        if not todo:
            return f"{len(s['files'])} files already extracted"
        parts = [self.p(x) for x in s["parts"]]
        miss = [x for x in parts if not os.path.exists(x)]
        if miss:
            raise PatchError(f"split archive part missing: {miss[0]}")
        if self.check_only:
            self.pending |= {s["dest"] + "/" + n for n in todo}
            return f"would extract {len(todo)} files"
        with zipfile.ZipFile(io.BufferedReader(SplitReader(parts), 1 << 20)) as z:
            for n, h in todo.items():
                out = os.path.join(dest, n)
                with z.open(n) as src, open(out + ".tmp", "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                if sha256_file(out + ".tmp") != h:
                    os.remove(out + ".tmp")
                    raise PatchError(f"extracted {n} has an unexpected hash")
                os.replace(out + ".tmp", out)
                print(f"    extracted {n}")
        return f"extracted {len(todo)} files"

    def step_bytes(self, s):
        if s["file"] in self.pending:
            return "ok (after extraction)"
        if self.status([s["file"]], [s["src"]], [s["dst"]]) == "done":
            return "already patched"
        if self.check_only:
            return "ok"
        path = self.p(s["file"])
        with open(path, "r+b") as f:
            for e in s["edits"]:
                new = self.new_bytes(e)
                f.seek(e["offset"]); self.check_old(s["file"], f.read(len(new)), e)
                f.seek(e["offset"]); f.write(new)
        if sha256_file(path) != s["dst"]:
            raise PatchError(f"{s['file']}: result hash mismatch")
        return f"{len(s['edits'])} edits"

    def _edit_fs(self, fs, edits, label):
        changes = []
        for e in edits:
            new = self.new_bytes(e)
            pos = fs.node_pos(e["node"], e["offset"])
            old = fs.read(pos, len(new))
            self.check_old(label, old, e)
            fs.write(pos, new)
            changes.append((pos, old, new))
        return changes

    def _crc(self, s, fs, changes):
        c = s["crc"]; cat = self.p(c["catalog"])
        data = open(cat, "rb").read()
        new = replace_catalog_crc(data, c["catalog"], c["old"], crc32_update(c["old"], fs.total, changes))
        if new != data and not self.check_only:
            open(cat, "wb").write(new)

    def step_unityfs(self, s):
        if s["file"] in self.pending:
            return "ok (after extraction)"
        st = self.status([s["file"]], [s["src"]], [s["dst"]])
        if st == "done":
            return "already patched"
        fs = UnityFS(open(self.p(s["file"]), "rb").read())
        changes = self._edit_fs(fs, s["edits"], s["file"])
        if "crc" in s:
            self._crc(s, fs, changes)
        if self.check_only:
            return "ok"
        out = fs.build()
        if sha256_bytes(out) != s["dst"]:
            raise PatchError(f"{s['file']}: result hash mismatch")
        open(self.p(s["file"]), "wb").write(out)
        return f"{len(s['edits'])} edits, {len(fs.dec)} block(s) re-encoded"

    def step_webdata(self, s):
        """Edits inside entries of a split UnityWebData pack; UnityFS entries are addressed by node."""
        if self.status(s["parts"], s["src"], s["dst"]) == "done":
            return "already patched"
        parts = [self.p(x) for x in s["parts"]]
        psize = os.path.getsize(parts[0])
        blob = b"".join(open(x, "rb").read() for x in parts)
        hs, entries = read_webdata(blob)
        changed, blocks = {}, 0
        for name in dict.fromkeys(e["entry"] for e in s["edits"]):
            ent = next((e for e in entries if e[3] == name), None)
            if ent is None:
                raise PatchError(f"{name} not found in the data pack")
            content = blob[ent[1]:ent[1] + ent[2]]
            edits = [e for e in s["edits"] if e["entry"] == name]
            if content.startswith(b"UnityFS\0"):
                fs = UnityFS(content)
                self._edit_fs(fs, edits, name)
                if not self.check_only:
                    changed[name] = fs.build(); blocks += len(fs.dec)
            else:
                data = bytearray(content)
                for e in edits:
                    new, o = self.new_bytes(e), e["offset"]
                    self.check_old(name, bytes(data[o:o + len(new)]), e)
                    data[o:o + len(new)] = new
                changed[name] = bytes(data)
        if self.check_only:
            return "ok"
        out = write_webdata(blob, hs, entries, changed)
        chunks = [out[i:i + psize] for i in range(0, len(out), psize)]
        if len(chunks) != len(parts):
            raise PatchError("patched data no longer fits the original number of parts")
        if [sha256_bytes(c) for c in chunks] != s["dst"]:
            raise PatchError(f"{s['parts'][0]}: result hash mismatch")
        for x, c in zip(parts, chunks):
            open(x, "wb").write(c)
        return f"{len(s['edits'])} edits in {', '.join(changed)}, {blocks} block(s) re-encoded"

    def step_replace(self, s):
        if self.status([s["file"]], [s["src"]], [s["dst"]]) == "done":
            return "already replaced"
        if not self.check_only:
            shutil.copyfile(os.path.join(DATA, *s["with"].split("/")), self.p(s["file"]))
        return "replaced"

    def step_rename(self, s):
        a, b = self.p(s["from"]), self.p(s["to"])
        if not os.path.exists(a) and os.path.exists(b):
            return "already renamed"
        if not os.path.exists(a):
            raise PatchError(f"{s['from']} missing")
        if not self.check_only:
            os.replace(a, b)
        return f"-> {s['to']}"

    def step_delete(self, s):
        present = [f for f in s["files"] if os.path.exists(self.p(f))]
        if not self.check_only:
            for f in present:
                x = self.p(f)
                shutil.rmtree(x) if os.path.isdir(x) else os.remove(x)
            for d in s.get("remove_empty_dirs", []):
                x = self.p(d)
                if os.path.isdir(x) and not os.listdir(x):
                    os.rmdir(x)
        return f"{len(present)} of {len(s['files'])} ({s.get('why', '')})"

    def run(self):
        m = self.m
        print(f"{m['name']}\n  target:  {self.root}\n  version: {m['upstream']['commit'][:12]}")
        if not os.path.isdir(self.root):
            raise PatchError(f"{self.root} is not a directory")
        extracted = {s["dest"] + "/" + n for s in m["steps"] if s["op"] == "extract_zip" for n in s["files"]}
        for s in m["steps"]:  # verify inputs before writing anything
            if s["op"] in ("bytes", "unityfs", "replace") and s["file"] not in extracted:
                self.status([s["file"]], [s["src"]], [s["dst"]])
            if s["op"] == "webdata":
                self.status(s["parts"], s["src"], s["dst"])
        if not self.check_only:
            for f in sorted(self.crlf):  # patch against the original LF content
                data = open(self.p(f), "rb").read()
                open(self.p(f), "wb").write(data.replace(b"\r\n", b"\n"))
                print(f"  (converted {f} from CRLF back to LF line endings)")
        for s in m["steps"]:
            label = s.get("file") or s.get("dest") or s.get("from") or (s["parts"][0].rsplit(".part", 1)[0] if "parts" in s else "")
            print(f"  {s['op']:8} {label + ': ' if label else ''}{getattr(self, 'step_' + s['op'])(s)}")
        print("check passed - nothing was changed." if self.check_only else "done.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("webport", help="folder of the original web port")
    ap.add_argument("--check", action="store_true", help="only verify the files, change nothing")
    a = ap.parse_args()
    try:
        Patcher(os.path.abspath(a.webport), a.check).run()
    except PatchError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
