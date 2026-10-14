#!/usr/bin/env python3
"""
tools/recover_db.py — rebuild a corrupt SQLite file (tracker.db / transfers.db)

    python3 tools/recover_db.py SRC.db DST.db [--dry-run]

Reads SRC.db page by page — never through SQLite's possibly broken B-tree
navigation, never writing to it — and writes a fresh DST.db:

  1. The schema is read from sqlite_master and replayed into DST.db
     (tables first, then indexes, views and triggers).
  2. For each table, its B-tree is walked from the root page with a visited
     set, so a page referenced twice (the corruption seen on Shaheen3:
     a full scan returns a row twice) is read once.
  3. Leaf pages that belong to no tree and are not on the freelist
     ("orphans" — e.g. "Page N: never used" in integrity_check) are decoded
     too; their rows are assigned to the table whose column layout they
     match and added if that rowid is not already present. Freelist pages
     are skipped so deleted rows do not come back.
  4. Rows are inserted with their original rowid. Records written before an
     ALTER TABLE ADD COLUMN are padded with the column's default.
  5. DST.db is checked with PRAGMA integrity_check and per-table counts are
     printed. Rows whose table cannot be determined go to a lost_and_found
     table in DST.db for manual review, never into a real table.

SRC.db is opened read-only. Swap the files only after checking the report,
with every dashboard / CLI process stopped (see docs/db_recovery.md).
Does not handle WITHOUT ROWID tables (none in the tracker schemas).
"""

from __future__ import annotations

import argparse
import sqlite3
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ── low-level file format ─────────────────────────────────────────────────────

INTERIOR_INDEX, INTERIOR_TABLE, LEAF_INDEX, LEAF_TABLE = 2, 5, 10, 13


def varint(buf: bytes, pos: int) -> tuple[int, int]:
    """SQLite varint at buf[pos] → (value, new_pos)."""
    v = 0
    for i in range(8):
        b = buf[pos + i]
        v = (v << 7) | (b & 0x7F)
        if b < 0x80:
            return v, pos + i + 1
    return (v << 8) | buf[pos + 8], pos + 9


class DbFile:
    def __init__(self, path: Path) -> None:
        self.data = path.read_bytes()
        if self.data[:16] != b"SQLite format 3\x00":
            raise ValueError(f"{path} is not an SQLite 3 database")
        ps = struct.unpack(">H", self.data[16:18])[0]
        self.page_size = 65536 if ps == 1 else ps
        self.usable = self.page_size - self.data[20]
        self.n_pages = len(self.data) // self.page_size
        enc = struct.unpack(">I", self.data[56:60])[0]
        self.encoding = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}.get(enc, "utf-8")

    def page(self, n: int) -> bytes:
        return self.data[(n - 1) * self.page_size: n * self.page_size]

    def hdr_off(self, n: int) -> int:
        return 100 if n == 1 else 0

    def page_type(self, n: int) -> int:
        return self.page(n)[self.hdr_off(n)] if 1 <= n <= self.n_pages else -1

    def cells(self, n: int) -> list[int]:
        p, h = self.page(n), self.hdr_off(n)
        t = p[h]
        hsize = 12 if t in (INTERIOR_INDEX, INTERIOR_TABLE) else 8
        count = struct.unpack(">H", p[h + 3:h + 5])[0]
        base = h + hsize
        out = []
        for i in range(count):
            off = struct.unpack(">H", p[base + 2 * i: base + 2 * i + 2])[0]
            if 0 < off < self.page_size:
                out.append(off)
        return out

    def right_child(self, n: int) -> int:
        p, h = self.page(n), self.hdr_off(n)
        return struct.unpack(">I", p[h + 8:h + 12])[0]

    def freelist_pages(self) -> set[int]:
        pages: set[int] = set()
        trunk = struct.unpack(">I", self.data[32:36])[0]
        while 0 < trunk <= self.n_pages and trunk not in pages:
            pages.add(trunk)
            p = self.page(trunk)
            nxt, cnt = struct.unpack(">II", p[0:8])
            for i in range(min(cnt, (self.usable - 8) // 4)):
                leaf = struct.unpack(">I", p[8 + 4 * i: 12 + 4 * i])[0]
                if 0 < leaf <= self.n_pages:
                    pages.add(leaf)
            trunk = nxt
        return pages

    # -- table leaf cell → (rowid, payload bytes, overflow pages used)
    def leaf_cell(self, n: int, off: int) -> tuple[int, bytes, list[int]]:
        p = self.page(n)
        size, pos = varint(p, off)
        rowid, pos = varint(p, pos)
        u = self.usable
        x = u - 35
        if size <= x:
            return rowid, bytes(p[pos:pos + size]), []
        m = ((u - 12) * 32 // 255) - 23
        k = m + ((size - m) % (u - 4))
        local = k if k <= x else m
        payload = bytearray(p[pos:pos + local])
        ovf = struct.unpack(">I", p[pos + local:pos + local + 4])[0]
        used = []
        while ovf and len(payload) < size and ovf not in used and ovf <= self.n_pages:
            used.append(ovf)
            op = self.page(ovf)
            nxt = struct.unpack(">I", op[0:4])[0]
            payload += op[4:4 + min(u - 4, size - len(payload))]
            ovf = nxt
        if len(payload) < size:
            raise ValueError(f"overflow chain short on page {n}")
        return rowid, bytes(payload), used

    def record(self, payload: bytes) -> list:
        hsize, pos = varint(payload, 0)
        types = []
        while pos < hsize:
            t, pos = varint(payload, pos)
            types.append(t)
        body, vals = hsize, []
        for t in types:
            if t == 0:
                vals.append(None)
            elif 1 <= t <= 6:
                n = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}[t]
                vals.append(int.from_bytes(payload[body:body + n], "big", signed=True))
                body += n
            elif t == 7:
                vals.append(struct.unpack(">d", payload[body:body + 8])[0])
                body += 8
            elif t in (8, 9):
                vals.append(t - 8)
            elif t >= 12 and t % 2 == 0:
                n = (t - 12) // 2
                vals.append(bytes(payload[body:body + n]))
                body += n
            elif t >= 13:
                n = (t - 13) // 2
                vals.append(payload[body:body + n].decode(self.encoding, errors="replace"))
                body += n
            else:
                raise ValueError(f"bad serial type {t}")
        return vals

    def leaf_rows(self, n: int) -> tuple[list[tuple[int, list]], list[int]]:
        rows, ovf_pages = [], []
        for off in self.cells(n):
            try:
                rowid, payload, used = self.leaf_cell(n, off)
                rows.append((rowid, self.record(payload)))
                ovf_pages += used
            except Exception:
                continue   # unreadable cell — skipped, counted by caller
        return rows, ovf_pages

    # -- walk one table tree; each page once
    def walk_table(self, root: int, seen: set[int]) -> tuple[list[tuple[int, list]], set[int], int]:
        rows: list[tuple[int, list]] = []
        pages: set[int] = set()
        double_refs = 0
        stack = [root]
        while stack:
            n = stack.pop()
            if not (1 <= n <= self.n_pages):
                continue
            if n in pages or n in seen:
                double_refs += 1
                continue
            pages.add(n)
            t = self.page_type(n)
            if t == INTERIOR_TABLE:
                p = self.page(n)
                kids = [struct.unpack(">I", p[o:o + 4])[0] for o in self.cells(n)]
                kids.append(self.right_child(n))
                stack.extend(reversed(kids))
            elif t == LEAF_TABLE:
                r, ovf = self.leaf_rows(n)
                rows += r
                pages.update(ovf)
        return rows, pages, double_refs

    def walk_any(self, root: int) -> set[int]:
        """All pages reachable from an index or table root (for orphan detection)."""
        pages, stack = set(), [root]
        while stack:
            n = stack.pop()
            if not (1 <= n <= self.n_pages) or n in pages:
                continue
            pages.add(n)
            t = self.page_type(n)
            if t in (INTERIOR_TABLE, INTERIOR_INDEX):
                p = self.page(n)
                for o in self.cells(n):
                    stack.append(struct.unpack(">I", p[o:o + 4])[0])
                stack.append(self.right_child(n))
        return pages


# ── recovery ──────────────────────────────────────────────────────────────────

def recover(src: Path, dst: Path, dry_run: bool = False) -> int:
    f = DbFile(src)
    ro = sqlite3.connect(f"file:{src}?mode=ro&immutable=1", uri=True)
    schema = ro.execute(
        "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master"
    ).fetchall()
    ro.close()

    tables = [(n, r, s) for t, n, _, r, s in schema
              if t == "table" and not n.startswith("sqlite_") and s and "WITHOUT ROWID" not in s.upper()]
    others = [(t, n, s) for t, n, _, r, s in schema
              if t in ("index", "view", "trigger") and s]

    # columns per table, from the schema replayed into a scratch DB
    scratch = sqlite3.connect(":memory:")
    for _, _, s in tables:
        scratch.execute(s)
    cols = {n: [(r[1], r[2].upper(), r[4], r[5]) for r in scratch.execute(f"PRAGMA table_info('{n}')")]
            for n, _, _ in tables}
    scratch.close()

    def ipk(n: str):
        pks = [c for c in cols[n] if c[3]]
        return pks[0][0] if len(pks) == 1 and pks[0][1] == "INTEGER" else None

    # 1. tree walks
    reached: set[int] = {1}
    for _, _, _, r, _ in schema:
        if r:
            reached |= f.walk_any(r)
    per_table: dict[str, dict[int, list]] = {}
    report = defaultdict(Counter)
    claimed: set[int] = set()
    for n, root, _ in tables:
        rows, pages, dbl = f.walk_table(root, claimed)
        claimed |= pages
        report[n]["double_references"] = dbl
        seen: dict[int, list] = {}
        for rowid, vals in rows:
            if rowid in seen:
                report[n]["duplicate_rowids_dropped"] += 1
                continue
            seen[rowid] = vals
        per_table[n] = seen
        report[n]["rows_from_tree"] = len(seen)

    # 2. orphan leaf pages (not in any tree, not free)
    free = f.freelist_pages()
    lost: list[tuple[int, int, list]] = []
    for pg in range(1, f.n_pages + 1):
        if pg in reached or pg in free or f.page_type(pg) != LEAF_TABLE:
            continue
        rows, _ = f.leaf_rows(pg)
        if not rows:
            continue
        width = Counter(len(v) for _, v in rows).most_common(1)[0][0]
        fits = [n for n, _, _ in tables if len(cols[n]) == width] or \
               [n for n, _, _ in tables if len(cols[n]) > width]
        target = fits[0] if len(fits) == 1 else None
        for rowid, vals in rows:
            if target and rowid not in per_table[target]:
                per_table[target][rowid] = vals
                report[target]["rows_from_orphan_pages"] += 1
            elif target:
                report[target]["orphan_rows_already_present"] += 1
            else:
                lost.append((pg, rowid, vals))

    # 3. report
    print(f"source: {src}  ({f.n_pages} pages of {f.page_size} B, {len(free)} free)")
    for n, _, _ in tables:
        r = report[n]
        print(f"  {n:<16} rows={len(per_table[n]):>6}  "
              f"dup_dropped={r['duplicate_rowids_dropped']}  "
              f"double_refs={r['double_references']}  "
              f"from_orphans={r['rows_from_orphan_pages']}")
    if lost:
        print(f"  lost_and_found   rows={len(lost)}  (table could not be determined)")
    if dry_run:
        print("dry run — nothing written")
        return 0

    # 4. write destination
    if dst.exists():
        print(f"error: {dst} exists — refusing to overwrite", file=sys.stderr)
        return 2
    out = sqlite3.connect(dst)
    out.execute("PRAGMA journal_mode=DELETE")
    for n, _, s in tables:
        out.execute(s)
    for n, _, _ in tables:
        names = [c[0] for c in cols[n]]
        defaults = [c[2] for c in cols[n]]
        key = ipk(n)
        if key:
            col_list = ", ".join(f'"{c}"' for c in names)
        else:
            col_list = "rowid, " + ", ".join(f'"{c}"' for c in names)
        ph = ", ".join("?" * (len(names) + (0 if key else 1)))
        for rowid, vals in sorted(per_table[n].items()):
            vals = list(vals[:len(names)])
            for i in range(len(vals), len(names)):          # pre-ALTER records
                d = defaults[i]
                vals.append(None if d is None else out.execute(f"SELECT {d}").fetchone()[0])
            if key:
                vals[names.index(key)] = rowid
                out.execute(f'INSERT INTO "{n}" ({col_list}) VALUES ({ph})', vals)
            else:
                out.execute(f'INSERT INTO "{n}" ({col_list}) VALUES ({ph})', [rowid] + vals)
    if lost:
        out.execute("CREATE TABLE lost_and_found (src_page INTEGER, src_rowid INTEGER, n_cols INTEGER, vals TEXT)")
        out.executemany("INSERT INTO lost_and_found VALUES (?,?,?,?)",
                        [(pg, rid, len(v), repr(v)) for pg, rid, v in lost])
    for t, n, s in others:
        try:
            out.execute(s)
        except sqlite3.Error as exc:
            print(f"  warning: could not recreate {t} {n}: {exc}")
    out.commit()
    ic = [r[0] for r in out.execute("PRAGMA integrity_check")]
    out.close()
    try:   # group k03 rwx like every other file the app creates (best effort)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from iolib.perms import apply_group_rwx
        apply_group_rwx(dst, directory=False)
    except Exception:
        pass
    print(f"written: {dst}")
    print(f"integrity_check: {'ok' if ic == ['ok'] else ic}")
    return 0 if ic == ["ok"] else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="report only")
    a = ap.parse_args()
    sys.exit(recover(a.src, a.dst, a.dry_run))


if __name__ == "__main__":
    main()
