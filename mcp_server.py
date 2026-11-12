#!/usr/bin/env python3
# mcp_server.py -- lwrap as an MCP (Model Context Protocol) server.
#
# A stdlib-only MCP server over stdio (newline-delimited JSON-RPC 2.0).
# The server lives on the host; every unit of work happens *inside a
# room*, through lwrap:
#
#   lwrap_build  build the lwrap ELF from a pole spec (sf_builder.py)
#   lwrap_rooms  list the room aliases from the registry (rooms.json)
#   lwrap_run    run a command inside a room (lwrap --root ... -- CMD)
#   lwrap_view   print a room's composed identity (lwrap --view DIR)
#   lwrap_put    store a file onto a room's floor (into its rootfs)
#   lwrap_get    load a file from a room's floor
#   lwrap_ls     list a directory inside a room
#   lwrap_read   read a line-carry file by line-number coordinates:
#                each record is "N\ttext" and carries its own number,
#                so the physical order of records does not matter;
#                a range [start, end] is loaded by number, and gaps
#                or duplicates are refused by name
#   lwrap_read8  read an address space whose lines are 8 bytes.
#                Lines are numbered from 1 (line 1 is the first
#                line, as in lwrap_read); the byte offset of line n
#                is 8*(n-1), and line 0 is refused by name.
#                raw8: the file itself is the lines; reading seeks
#                arithmetically, no scan and no index. bag8: records
#                of [tag 8B = line number][data 8B] under a 48-byte
#                header (magic / total length / sha256); physical
#                order is free, and a complete census is always
#                digest-verified
#
# Text vs binary is judged by the zero-check mechanism: data with
# no NUL byte is text, data containing a NUL byte is binary --
# regardless of whether it happens to decode as UTF-8.
#
# Write side (defrag-Link model): lwrap_store8 appends a record
# (a new generation; later wins) and lwrap_drop8 appends a
# tombstone, so writes deliberately fragment a bag and no record
# ever moves. lwrap_defrag folds the chain once into a ledger
# (<path>.fold.json: line -> winning offset, logical digest) --
# the folded ledger is the defragmented entity, the bag's bytes
# are untouched, and reads verify against the ledger until the
# bag grows again. lwrap_rm removes a file and its ledger.
#
# put/get/ls treat the rootfs as the room's floor: files are stored
# and loaded from the host side, under a strict containment rule --
# the resolved real path must stay inside the rootfs. Escapes via
# ".." or via a symlink planted inside the room are refused by name
# (path_escapes_room). File size cap for put/get: 256 KiB.
#
# Rooms can be called by alias instead of by absolute path. The
# registry is a JSON file: rooms.json beside this script, or the path
# in the LWRAP_ROOMS_JSON environment variable:
#
#   {"rooms": {
#       "test":  {"rootfs": "/path/to/rootfs",
#                 "description": "a disposable test room"},
#       "rouka": {"dir": "/path/to/rooms/rouka",
#                 "description": "a room with a .lwrap.lnk, for view"}}}
#
# An entry may carry "rootfs" (for lwrap_run), "dir" (for lwrap_view),
# or both. The registry is re-read on every call, so editing it takes
# effect without restarting the server. Unknown aliases are refused
# by name. See rooms.example.json.
#
# MCP is the plumbing; lwrap is the room. The server adds no state of
# its own beyond the lwrap directory (the folder holding this script,
# sf_builder.py, spec_lwrap.json and the built ./lwrap binary).
#
# Protocol notes:
# - stdout carries ONLY JSON-RPC messages; diagnostics go to stderr.
# - A tool failure is a tools/call result with isError=true and the
#   lwrap refusal named in the text (e.g. "edge bind_rootfs failed"),
#   never a protocol-level error, and it never kills the server.
# - Children are spawned with stdin=DEVNULL so a room command can
#   never consume the MCP stream.
#
# Usage:
#   python3 sf_builder.py spec_lwrap.json --outdir .   # build lwrap
#   python3 mcp_server.py                              # serve on stdio
#
# MCP client config (stdio transport) example:
#   {"mcpServers": {"lwrap": {
#       "command": "python3",
#       "args": ["/path/to/lwrap/mcp_server.py"]}}}

import base64
import hashlib
import json
import os
import subprocess
import sys

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18"]
SERVER_NAME = "lwrap"
SERVER_VERSION = "1.8.0"

HERE = os.path.dirname(os.path.abspath(__file__))
BUILDER = os.path.join(HERE, "sf_builder.py")
DEFAULT_SPEC = os.path.join(HERE, "spec_lwrap.json")
LWRAP = os.path.join(HERE, "lwrap")

MAX_OUTPUT = 64 * 1024          # bytes of stdout/stderr returned per call
DEFAULT_TIMEOUT = 15            # seconds for lwrap_run
MAX_TIMEOUT = 120


def log(msg):
    sys.stderr.write("lwrap-mcp: %s\n" % msg)
    sys.stderr.flush()


# ------------------------------------------------------------------
# Room alias registry (rooms.json). Re-read on every call: the
# registry is data on the floor, not state in the server.
# ------------------------------------------------------------------

REGISTRY_ENV = "LWRAP_ROOMS_JSON"
DEFAULT_REGISTRY = os.path.join(HERE, "rooms.json")


def registry_path():
    return os.environ.get(REGISTRY_ENV) or DEFAULT_REGISTRY


def load_registry():
    """Return (rooms_dict, error_text). No registry file is not an
    error: it simply means no aliases are registered."""
    path = registry_path()
    if not os.path.isfile(path):
        return {}, None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, "rooms_registry_invalid: %s (%s)" % (path, e)
    rooms = data.get("rooms") if isinstance(data, dict) else None
    if not isinstance(rooms, dict):
        return None, ("rooms_registry_invalid: %s "
                      "(expected an object with a 'rooms' object)" % path)
    for name, entry in rooms.items():
        if not isinstance(entry, dict):
            return None, ("rooms_registry_invalid: entry %r "
                          "is not an object" % name)
    return rooms, None


def resolve_alias(alias, field):
    """Resolve an alias to its 'rootfs' or 'dir' path.
    Return (path, error_text)."""
    rooms, err = load_registry()
    if err:
        return None, err
    entry = rooms.get(alias)
    if entry is None:
        known = ", ".join(sorted(rooms)) or "(none)"
        return None, "unknown_room_alias: %s (known: %s)" % (alias, known)
    path = entry.get(field)
    if not path:
        return None, "room_has_no_%s: %s" % (field, alias)
    return path, None


def tool_rooms(args):
    rooms, err = load_registry()
    if err:
        return (err, None, True)
    listing = []
    for name in sorted(rooms):
        entry = rooms[name]
        item = {"name": name}
        for field in ("rootfs", "dir", "description"):
            if entry.get(field):
                item[field] = entry[field]
        listing.append(item)
    info = {"registry": registry_path(), "rooms": listing}
    return (json.dumps(info, indent=2, ensure_ascii=False), info, False)


# ------------------------------------------------------------------
# Tool implementations. Each returns (text, structured, is_error).
# ------------------------------------------------------------------

def _clip(data):
    if len(data) > MAX_OUTPUT:
        return data[:MAX_OUTPUT].decode("utf-8", "replace") + "\n... [truncated]"
    return data.decode("utf-8", "replace")


def tool_build(args):
    spec = args.get("spec") or DEFAULT_SPEC
    outdir = args.get("outdir") or HERE
    if not os.path.isfile(spec):
        return ("spec_not_found: %s" % spec, None, True)
    if not os.path.isdir(outdir):
        return ("outdir_not_found: %s" % outdir, None, True)
    proc = subprocess.run(
        [sys.executable, BUILDER, spec, "--outdir", outdir],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
    out = _clip(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        return ("build_failed (rc=%d):\n%s" % (proc.returncode, out), None, True)
    binary = os.path.join(outdir, "lwrap")
    if not os.path.isfile(binary):
        return ("build_failed: builder reported success but %s is missing"
                % binary, None, True)
    blob = open(binary, "rb").read()
    info = {"path": binary, "size_bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}
    return (json.dumps(info, indent=2), info, False)


def _require_binary():
    if not os.path.isfile(LWRAP):
        return "lwrap_not_built: %s is missing; call lwrap_build first" % LWRAP
    return None


def tool_run(args):
    missing = _require_binary()
    if missing:
        return (missing, None, True)
    rootfs = args.get("rootfs")
    if args.get("room") and rootfs:
        return ("conflicting_arguments: give room or rootfs, not both",
                None, True)
    if args.get("room"):
        rootfs, err = resolve_alias(str(args["room"]), "rootfs")
        if err:
            return (err, None, True)
    if not rootfs:
        return ("missing_argument: rootfs (or room, an alias)", None, True)
    command = args.get("command")
    if not command or not isinstance(command, list):
        return ("missing_argument: command (a non-empty array)", None, True)
    timeout = args.get("timeout_sec") or DEFAULT_TIMEOUT
    try:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        return ("bad_argument: timeout_sec must be an integer", None, True)
    devices = args.get("devices")
    if devices is not None and not isinstance(devices, list):
        return ("bad_argument: devices must be a list of paths",
                None, True)
    argv = [LWRAP, "--root", rootfs]
    if args.get("pid", True):
        argv.append("--pid")
    if args.get("proc", False):
        argv.append("--proc")
    if args.get("net", False):
        argv.append("--net")
    if args.get("cpus"):
        argv += ["--cpus", str(args["cpus"])]
    for d in devices or []:
        argv += ["--device", str(d)]
    if args.get("workdir"):
        argv += ["--workdir", str(args["workdir"])]
    argv += ["--"] + [str(c) for c in command]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout)
        result = {"stdout": _clip(proc.stdout), "stderr": _clip(proc.stderr),
                  "exit_code": proc.returncode, "timed_out": False}
        is_error = proc.returncode != 0
    except subprocess.TimeoutExpired as e:
        result = {"stdout": _clip(e.stdout or b""),
                  "stderr": _clip(e.stderr or b""),
                  "exit_code": None, "timed_out": True}
        is_error = True
    return (json.dumps(result, indent=2), result, is_error)


def tool_view(args):
    missing = _require_binary()
    if missing:
        return (missing, None, True)
    room = args.get("dir")
    if args.get("room") and room:
        return ("conflicting_arguments: give room or dir, not both",
                None, True)
    if args.get("room"):
        room, err = resolve_alias(str(args["room"]), "dir")
        if err:
            return (err, None, True)
    if not room:
        return ("missing_argument: dir (or room, an alias)", None, True)
    proc = subprocess.run([LWRAP, "--view", str(room)],
                          stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=30)
    text = _clip(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        return ("view_refused (rc=%d): %s" % (proc.returncode, text.strip()),
                None, True)
    return (text, {"view": text}, False)


# ------------------------------------------------------------------
# File tools: Store (put) / Load (get) / list (ls) on a room's floor.
# Containment: the resolved real path must stay inside the rootfs.
# ------------------------------------------------------------------

MAX_FILE = 256 * 1024


def zero_check(data):
    """The text/binary judgment: True (text) iff no NUL byte."""
    return b"\x00" not in data


def data_kind(data):
    return "text" if zero_check(data) else "binary"


def resolve_rootfs(args):
    """Shared room/rootfs argument handling for run/put/get/ls.
    Return (rootfs, error_text)."""
    rootfs = args.get("rootfs")
    if args.get("room") and rootfs:
        return None, ("conflicting_arguments: give room or rootfs, "
                      "not both")
    if args.get("room"):
        rootfs, err = resolve_alias(str(args["room"]), "rootfs")
        if err:
            return None, err
    if not rootfs:
        return None, "missing_argument: rootfs (or room, an alias)"
    if not os.path.isdir(rootfs):
        return None, "rootfs_not_found: %s" % rootfs
    return rootfs, None


def _within(root_real, path):
    return (path == root_real
            or path.startswith(root_real.rstrip(os.sep) + os.sep))


def _room_target(rootfs, inside):
    """Map a room-absolute path (e.g. /tmp/x) into the rootfs and
    verify containment of the resolved path. Return (host_path,
    normalised_inside, error_text). Symlinks on the final component
    are followed here; callers that write check for links first."""
    if inside is None or str(inside) == "":
        return None, None, "missing_argument: path"
    root_real = os.path.realpath(rootfs)
    # Paths are room-absolute. A ".." that would climb above the
    # room's root is an escape attempt, refused rather than clamped.
    rel = os.path.normpath(str(inside).lstrip("/"))
    if rel == ".." or rel.startswith(".." + os.sep):
        return None, None, ("path_escapes_room: %s climbs above the "
                            "room's root" % inside)
    norm = "/" if rel == "." else "/" + rel
    host = root_real + norm
    resolved = os.path.realpath(host)
    if not _within(root_real, resolved):
        return None, norm, ("path_escapes_room: %s resolves outside "
                            "the room" % inside)
    return host, norm, None


def tool_put(args):
    rootfs, err = resolve_rootfs(args)
    if err:
        return (err, None, True)
    host, norm, err = _room_target(rootfs, args.get("path"))
    if err:
        return (err, None, True)
    if args.get("content") is not None and args.get("content_base64"):
        return ("conflicting_arguments: give content or "
                "content_base64, not both", None, True)
    if args.get("content") is not None:
        data = str(args["content"]).encode("utf-8")
    elif args.get("content_base64"):
        try:
            data = base64.b64decode(str(args["content_base64"]),
                                    validate=True)
        except ValueError:
            return ("bad_argument: content_base64 is not valid base64",
                    None, True)
    else:
        return ("missing_argument: content (or content_base64)",
                None, True)
    if len(data) > MAX_FILE:
        return ("file_too_large: %d bytes (cap %d)"
                % (len(data), MAX_FILE), None, True)
    if os.path.islink(host):
        return ("path_is_symlink: %s" % norm, None, True)
    if os.path.isdir(host):
        return ("not_a_file: %s is a directory" % norm, None, True)
    # Create parent directories, but never through a planted symlink:
    # verify the deepest existing ancestor resolves inside the room.
    root_real = os.path.realpath(rootfs)
    parent = os.path.dirname(host)
    ancestor = parent
    while not os.path.exists(ancestor):
        ancestor = os.path.dirname(ancestor)
    if not _within(root_real, os.path.realpath(ancestor)):
        return ("path_escapes_room: parent of %s resolves outside "
                "the room" % norm, None, True)
    os.makedirs(parent, exist_ok=True)
    with open(host, "wb") as f:
        f.write(data)
    info = {"path": norm, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}
    return (json.dumps(info, indent=2), info, False)


def tool_get(args):
    rootfs, err = resolve_rootfs(args)
    if err:
        return (err, None, True)
    host, norm, err = _room_target(rootfs, args.get("path"))
    if err:
        return (err, None, True)
    if not os.path.exists(host):
        return ("file_not_found: %s" % norm, None, True)
    if os.path.isdir(host):
        return ("not_a_file: %s is a directory (use lwrap_ls)"
                % norm, None, True)
    size = os.path.getsize(host)
    if size > MAX_FILE:
        return ("file_too_large: %s is %d bytes (cap %d)"
                % (norm, size, MAX_FILE), None, True)
    with open(host, "rb") as f:
        data = f.read()
    info = {"path": norm, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "kind": data_kind(data)}
    # Text/binary is judged by the zero-check mechanism: no NUL
    # byte means text. UTF-8 validity alone does not make text --
    # "a\x00b" decodes fine but is binary by the zero-check.
    if info["kind"] == "text":
        try:
            info["encoding"] = "utf-8"
            info["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            info["kind"] = "binary"
            info["encoding"] = "base64"
            info["content"] = base64.b64encode(data).decode("ascii")
    else:
        info["encoding"] = "base64"
        info["content"] = base64.b64encode(data).decode("ascii")
    return (json.dumps(info, indent=2, ensure_ascii=False), info, False)


def tool_ls(args):
    rootfs, err = resolve_rootfs(args)
    if err:
        return (err, None, True)
    host, norm, err = _room_target(rootfs, args.get("path") or "/")
    if err:
        return (err, None, True)
    if not os.path.exists(host):
        return ("file_not_found: %s" % norm, None, True)
    if not os.path.isdir(host):
        return ("not_a_directory: %s" % norm, None, True)
    entries = []
    for name in sorted(os.listdir(host)):
        st = os.lstat(os.path.join(host, name))
        if os.path.islink(os.path.join(host, name)):
            kind = "link"
        elif os.path.isdir(os.path.join(host, name)):
            kind = "dir"
        elif os.path.isfile(os.path.join(host, name)):
            kind = "file"
        else:
            kind = "other"
        entries.append({"name": name, "type": kind, "size": st.st_size})
    info = {"path": norm, "entries": entries}
    return (json.dumps(info, indent=2, ensure_ascii=False), info, False)


def tool_read(args):
    rootfs, err = resolve_rootfs(args)
    if err:
        return (err, None, True)
    host, norm, err = _room_target(rootfs, args.get("path"))
    if err:
        return (err, None, True)
    if not os.path.exists(host):
        return ("file_not_found: %s" % norm, None, True)
    if os.path.isdir(host):
        return ("not_a_file: %s is a directory" % norm, None, True)
    size = os.path.getsize(host)
    if size > MAX_FILE:
        return ("file_too_large: %s is %d bytes (cap %d)"
                % (norm, size, MAX_FILE), None, True)
    with open(host, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ("not_line_carry_format: %s is not UTF-8 text" % norm,
                None, True)
    physical = text.split("\n")
    if physical and physical[-1] == "":
        physical.pop()
    records = {}
    for i, line in enumerate(physical, 1):
        head, sep, body = line.partition("\t")
        if not sep or not head.isdigit():
            return ("not_line_carry_format: physical line %d of %s "
                    "has no line-number header" % (i, norm), None, True)
        n = int(head)
        if n in records:
            return ("duplicate_line_number: %d (in %s)" % (n, norm),
                    None, True)
        records[n] = body
    if not records:
        return ("not_line_carry_format: %s has no records" % norm,
                None, True)
    numbers = sorted(records)
    gaps = [n for n in range(numbers[0], numbers[-1] + 1)
            if n not in records]
    try:
        start = int(args.get("start") or 1)
        end = int(args.get("end") or numbers[-1])
    except (TypeError, ValueError):
        return ("bad_argument: start/end must be integers", None, True)
    if start < 1 or end < start:
        return ("bad_argument: range [%s, %s]" % (start, end),
                None, True)
    for n in range(start, end + 1):
        if n not in records:
            return ("missing_line_number: %d (in %s)" % (n, norm),
                    None, True)
    lines = [{"n": n, "text": records[n]} for n in range(start, end + 1)]
    info = {"path": norm, "start": start, "end": end,
            "lines": lines,
            "text": "".join(records[n] + "\n"
                            for n in range(start, end + 1)),
            "records": len(records),
            "number_min": numbers[0], "number_max": numbers[-1],
            "gap_count": len(gaps), "gaps": gaps[:50]}
    return (json.dumps(info, indent=2, ensure_ascii=False), info, False)


BAG8_MAGIC = b"LCARRY8\x00"
READ8_CAP = 4 * 1024 * 1024
TOMBSTONE = 1 << 63


def _bag_parse(blob):
    """Parse a bag8 blob -> (total_len, want_sha, nlines, entries)
    or (None, named_error). Entries are (line, data8, offset,
    tombstone) in physical order; lines are numbered from 1."""
    if len(blob) < 48 or (len(blob) - 48) % 16 != 0:
        return None, ("not_line8_bag_format: bad length %d"
                      % len(blob))
    total_len = int.from_bytes(blob[8:16], "little")
    want_sha = blob[16:48]
    nlines = (total_len + 7) // 8
    entries = []
    body = blob[48:]
    for i in range(0, len(body), 16):
        raw = int.from_bytes(body[i:i + 8], "little")
        line = raw & (TOMBSTONE - 1)
        if line < 1 or line > nlines:
            return None, ("not_line8_bag_format: tag %d is not a "
                          "line of 1..%d" % (line, nlines))
        entries.append((line, body[i + 8:i + 16], 48 + i,
                        bool(raw & TOMBSTONE)))
    return (total_len, want_sha, nlines, entries), None


def _bag_resolve(entries):
    """Resolve generations: a later record wins, a tombstone
    deletes. Returns (state, deleted, generations, winner_offset)
    where state maps line -> data8 for present lines, and
    generations counts superseded records (the fragmentation)."""
    state = {}
    deleted = set()
    winner_off = {}
    generations = 0
    for line, data, off, tomb in entries:
        if line in state or line in deleted:
            generations += 1
        if tomb:
            state.pop(line, None)
            winner_off.pop(line, None)
            deleted.add(line)
        else:
            state[line] = data
            winner_off[line] = off
            deleted.discard(line)
    return state, deleted, generations, winner_off


def _fold_path(host):
    return host + ".fold.json"


def _load_fold(host, bag_size):
    """The folded ledger for a bag, or None if absent or stale
    (the bag grew after the fold, so its offsets/digest no longer
    describe the present bag)."""
    try:
        with open(_fold_path(host), "r", encoding="utf-8") as f:
            fold = json.load(f)
    except (OSError, ValueError):
        return None
    if fold.get("format") != "lwrap-fold8-1":
        return None
    if fold.get("bag_size") != bag_size:
        return None
    return fold


def tool_read8(args):
    rootfs, err = resolve_rootfs(args)
    if err:
        return (err, None, True)
    host, norm, err = _room_target(rootfs, args.get("path"))
    if err:
        return (err, None, True)
    if not os.path.exists(host):
        return ("file_not_found: %s" % norm, None, True)
    if os.path.isdir(host):
        return ("not_a_file: %s is a directory" % norm, None, True)
    size = os.path.getsize(host)
    if size > READ8_CAP:
        return ("file_too_large: %s is %d bytes (cap %d)"
                % (norm, size, READ8_CAP), None, True)
    try:
        _s = args.get("start")
        start = 1 if _s is None else int(_s)
        _e = args.get("end")
        end = None if _e is None else int(_e)
    except (TypeError, ValueError):
        return ("bad_argument: start/end must be integers", None, True)

    with open(host, "rb") as f:
        head = f.read(len(BAG8_MAGIC))
    is_bag = (head == BAG8_MAGIC)

    def range_err(nlines):
        return ("line_out_of_range: [%d, %d] of %d lines, numbered "
                "from 1 (in %s)" % (start, end, nlines, norm),
                None, True)

    if not is_bag:
        # raw8: the file itself is the lines; seek arithmetically.
        # Line n lives at byte offset 8*(n-1); line 1 is first.
        nlines = (size + 7) // 8
        if end is None:
            end = nlines
        if start < 1 or end < start or end > nlines:
            return range_err(nlines)
        with open(host, "rb") as f:
            f.seek(8 * (start - 1))
            data = f.read(min(8 * (end - start + 1),
                              size - 8 * (start - 1)))
        info = {"path": norm, "format": "raw8", "start": start,
                "end": end, "lines_total": nlines, "via": "arithmetic",
                "kind": data_kind(data),
                "byte_length": len(data),
                "content_base64": base64.b64encode(data).decode("ascii")}
        return (json.dumps(info, indent=2), info, False)

    # bag8: header 48B + records of [tag u64 LE][data 8B]; tags are
    # line numbers from 1. Records are generations: a later record
    # wins, a tombstone (tag's top bit) deletes. Nothing is ever
    # rewritten in place, so a written bag is fragmented by design
    # until lwrap_defrag folds it.
    with open(host, "rb") as f:
        blob = f.read()
    if len(blob) < 48 or (len(blob) - 48) % 16 != 0:
        return ("not_line8_bag_format: %s (bad length %d)"
                % (norm, len(blob)), None, True)
    total_len = int.from_bytes(blob[8:16], "little")
    want_sha = blob[16:48]
    nlines = (total_len + 7) // 8
    if end is None:
        end = nlines
    if start < 1 or end < start or end > nlines:
        return range_err(nlines)
    # Read as fragments, always. If the bag is clean-shaped and
    # the requested records sit exactly where the arithmetic fold
    # puts them (tag == line at offset 48+16*(n-1)), the ledger is
    # the formula and no scan happens. Verification is deferred
    # on this path -- settlement is defrag's job -- unless a
    # fresh fold ledger settles it here.
    if len(blob) == 48 + 16 * nlines:
        probed = {}
        ok = True
        for n in range(start, end + 1):
            off = 48 + 16 * (n - 1)
            if int.from_bytes(blob[off:off + 8], "little") != n:
                ok = False
                break
            probed[n] = blob[off + 8:off + 16]
        if ok:
            data = b"".join(probed[n] for n in range(start, end + 1))
            data = data[:max(0, min(len(data),
                                    total_len - 8 * (start - 1)))]
            verified, via_d = False, None
            fold = _load_fold(host, len(blob))
            if fold is not None and fold.get("sha256") \
                    and not fold.get("gaps"):
                offs = fold.get("offsets") or []
                if len(offs) == nlines and None not in offs:
                    full = b"".join(blob[o + 8:o + 16]
                                    for o in offs)[:total_len]
                    if hashlib.sha256(full).hexdigest() \
                            != fold["sha256"]:
                        return ("digest_mismatch: %s (bag data "
                                "does not match its fold)" % norm,
                                None, True)
                    verified, via_d = True, "fold"
            info = {"path": norm, "format": "bag8", "start": start,
                    "end": end, "lines_total": nlines,
                    "records": nlines, "generations": 0,
                    "via": "arithmetic_fold",
                    "digest_verified": verified, "digest_via": via_d,
                    "kind": data_kind(data),
                    "byte_length": len(data),
                    "content_base64":
                    base64.b64encode(data).decode("ascii")}
            return (json.dumps(info, indent=2), info, False)
    # The arithmetic fold did not hold: fall back to resolving
    # the whole chain (generations, tombstones) by scan.
    parsed, perr = _bag_parse(blob)
    if perr:
        return ("%s (in %s)" % (perr, norm), None, True)
    _tl, _ws, _nl, entries = parsed
    state, deleted, generations, _wo = _bag_resolve(entries)
    missing = [n for n in range(start, end + 1) if n not in state]
    if missing:
        return ("missing_line_number: %d (in %s)" % (missing[0], norm),
                None, True)
    complete = len(state) == nlines
    digest_verified = False
    digest_via = None
    if complete:
        full = b"".join(state[n]
                        for n in range(1, nlines + 1))[:total_len]
        got = hashlib.sha256(full).digest()
        clean_shape = (len(blob) == 48 + 16 * nlines
                       and generations == 0)
        fold = None if clean_shape else _load_fold(host, len(blob))
        if clean_shape and want_sha != b"\x00" * 32:
            if got != want_sha:
                return ("digest_mismatch: %s (bag data does not "
                        "match its header)" % norm, None, True)
            digest_verified, digest_via = True, "header"
        elif fold is not None and fold.get("sha256"):
            if got.hex() != fold["sha256"]:
                return ("digest_mismatch: %s (bag data does not "
                        "match its fold)" % norm, None, True)
            digest_verified, digest_via = True, "fold"
    data = b"".join(state[n] for n in range(start, end + 1))
    data = data[:max(0, min(len(data), total_len - 8 * (start - 1)))]
    info = {"path": norm, "format": "bag8", "start": start, "end": end,
            "lines_total": nlines, "records": len(entries),
            "generations": generations, "via": "scan",
            "digest_verified": digest_verified,
            "digest_via": digest_via,
            "kind": data_kind(data),
            "byte_length": len(data),
            "content_base64": base64.b64encode(data).decode("ascii")}
    return (json.dumps(info, indent=2), info, False)


def _ensure_parent(rootfs, host, norm):
    """Create the target's parent directories without ever
    following a planted symlink out of the room (same rule as
    lwrap_put). Returns a named error string or None."""
    root_real = os.path.realpath(rootfs)
    parent = os.path.dirname(host)
    ancestor = parent
    while not os.path.exists(ancestor):
        ancestor = os.path.dirname(ancestor)
    if not _within(root_real, os.path.realpath(ancestor)):
        return ("path_escapes_room: parent of %s resolves outside "
                "the room" % norm)
    os.makedirs(parent, exist_ok=True)
    return None


def _open_bag(args, create_line=None, create_len=0):
    """Shared opening for the bag writers: resolves the target and
    returns (host, norm, blob, parsed) or an error triple."""
    rootfs, err = resolve_rootfs(args)
    if err:
        return (None, None, None, None, (err, None, True))
    host, norm, err = _room_target(rootfs, args.get("path"))
    if err:
        return (None, None, None, None, (err, None, True))
    if os.path.isdir(host):
        return (None, None, None, None,
                ("not_a_file: %s is a directory" % norm, None, True))
    if os.path.exists(host):
        if os.path.getsize(host) > READ8_CAP:
            return (None, None, None, None,
                    ("file_too_large: %s (cap %d)" % (norm, READ8_CAP),
                     None, True))
        with open(host, "rb") as f:
            blob = f.read()
        if not blob.startswith(BAG8_MAGIC):
            return (None, None, None, None,
                    ("not_line8_bag_format: %s exists and is not a "
                     "bag8" % norm, None, True))
        parsed, perr = _bag_parse(blob)
        if perr:
            return (None, None, None, None,
                    ("%s (in %s)" % (perr, norm), None, True))
        return (host, norm, blob, parsed, None)
    if create_line is None:
        return (None, None, None, None,
                ("file_not_found: %s" % norm, None, True))
    perr2 = _ensure_parent(rootfs, host, norm)
    if perr2:
        return (None, None, None, None, (perr2, None, True))
    total_len = 8 * (create_line - 1) + create_len
    blob = (BAG8_MAGIC + total_len.to_bytes(8, "little")
            + b"\x00" * 32)
    parsed, perr = _bag_parse(blob)
    assert perr is None
    return (host, norm, blob, parsed, None)


def tool_store8(args):
    try:
        line = int(args.get("line"))
    except (TypeError, ValueError):
        return ("bad_argument: line must be an integer", None, True)
    if line < 1:
        return ("line_out_of_range: line %d; lines are numbered "
                "from 1" % line, None, True)
    b64 = args.get("data_base64")
    if not isinstance(b64, str):
        return ("bad_argument: data_base64 is required", None, True)
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError:
        return ("bad_argument: data_base64 is not valid base64",
                None, True)
    if not 1 <= len(data) <= 8:
        return ("bad_argument: a line is 1..8 bytes, got %d"
                % len(data), None, True)
    host, norm, blob, parsed, errt = _open_bag(
        args, create_line=line, create_len=len(data))
    if errt:
        return errt
    total_len, _sha, _nlines, entries = parsed
    new_total = max(total_len, 8 * (line - 1) + len(data))
    record = line.to_bytes(8, "little") + data.ljust(8, b"\x00")
    # Patch only the header's total_len field, then append the
    # record. No existing record moves.
    existed = os.path.exists(host)
    with open(host, "r+b" if existed else "wb") as f:
        if not existed:
            f.write(blob)
        f.seek(8)
        f.write(new_total.to_bytes(8, "little"))
        f.seek(0, os.SEEK_END)
        f.write(record)
    info = {"path": norm, "line": line, "total_len": new_total,
            "records": len(entries) + 1,
            "bag_size": 48 + 16 * (len(entries) + 1),
            "note": "appended as a new generation; no record moved"}
    return (json.dumps(info, indent=2), info, False)


def tool_drop8(args):
    try:
        line = int(args.get("line"))
    except (TypeError, ValueError):
        return ("bad_argument: line must be an integer", None, True)
    if line < 1:
        return ("line_out_of_range: line %d; lines are numbered "
                "from 1" % line, None, True)
    host, norm, blob, parsed, errt = _open_bag(args)
    if errt:
        return errt
    _tl, _sha, _nlines, entries = parsed
    state, _d, _g, _w = _bag_resolve(entries)
    if line not in state:
        return ("missing_line_number: %d (in %s)" % (line, norm),
                None, True)
    record = (TOMBSTONE | line).to_bytes(8, "little") + b"\x00" * 8
    with open(host, "ab") as f:
        f.write(record)
    info = {"path": norm, "line": line,
            "records": len(entries) + 1,
            "note": "tombstone appended; no record moved"}
    return (json.dumps(info, indent=2), info, False)


def tool_defrag(args):
    host, norm, blob, parsed, errt = _open_bag(args)
    if errt:
        return errt
    total_len, want_sha, nlines, entries = parsed
    state, _deleted, generations, winner_off = _bag_resolve(entries)
    gaps = [n for n in range(1, nlines + 1) if n not in state]
    digest = None
    if not gaps:
        full = b"".join(state[n]
                        for n in range(1, nlines + 1))[:total_len]
        digest = hashlib.sha256(full).hexdigest()
    # Settlement checks the header where the header is
    # authoritative: a clean-shaped, generation-free bag whose
    # header carries a real digest must match it, or the fold
    # refuses to settle tampered content as truth.
    if digest is not None and generations == 0 \
            and len(blob) == 48 + 16 * nlines \
            and want_sha != b"\x00" * 32 \
            and digest != want_sha.hex():
        return ("digest_mismatch: %s (bag data does not match its "
                "header; defrag refuses to settle it)" % norm,
                None, True)
    fold = {"format": "lwrap-fold8-1", "bag_size": len(blob),
            "total_len": total_len, "sha256": digest,
            "offsets": [winner_off.get(n) for n in
                        range(1, nlines + 1)],
            "gaps": gaps, "generations_folded": generations}
    fold_host = _fold_path(host)
    tmp = fold_host + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fold, f, indent=1)
    os.replace(tmp, fold_host)
    info = {"path": norm, "fold_path": norm + ".fold.json",
            "bag_size": len(blob),
            "bag_sha256": hashlib.sha256(blob).hexdigest(),
            "lines_present": len(state), "gaps": gaps,
            "generations_folded": generations,
            "digest_settled": digest is not None,
            "note": "the bag itself was not modified; the folded "
                    "ledger is the defragmented entity"}
    return (json.dumps(info, indent=2), info, False)


def tool_rm(args):
    rootfs, err = resolve_rootfs(args)
    if err:
        return (err, None, True)
    host, norm, err = _room_target(rootfs, args.get("path"))
    if err:
        return (err, None, True)
    if not os.path.exists(host):
        return ("file_not_found: %s" % norm, None, True)
    if os.path.isdir(host):
        return ("not_a_file: %s is a directory" % norm, None, True)
    size = os.path.getsize(host)
    os.unlink(host)
    fold_removed = False
    if os.path.exists(_fold_path(host)):
        os.unlink(_fold_path(host))
        fold_removed = True
    info = {"path": norm, "removed_bytes": size,
            "fold_removed": fold_removed}
    return (json.dumps(info, indent=2), info, False)


TOOLS = [
    {
        "name": "lwrap_build",
        "description": "Build the lwrap ELF from a pole spec with "
                       "sf_builder.py. Returns the binary size and sha256.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string",
                         "description": "Path to the pole spec JSON "
                                        "(default: spec_lwrap.json beside "
                                        "the server)."},
                "outdir": {"type": "string",
                           "description": "Where to write lwrap_gen.c and "
                                          "lwrap (default: beside the "
                                          "server)."},
            },
        },
        "handler": tool_build,
    },
    {
        "name": "lwrap_rooms",
        "description": "List the registered room aliases (rooms.json): "
                       "the names lwrap_run and lwrap_view accept as "
                       "their 'room' argument, with the paths they "
                       "resolve to.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_rooms,
    },
    {
        "name": "lwrap_run",
        "description": "Run a command inside an lwrap room "
                       "(user/mount namespaces + chroot into rootfs). "
                       "Returns stdout, stderr and the exit code. A failed "
                       "edge is reported by name (isError).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms); resolves to "
                                        "that room's rootfs."},
                "command": {"type": "array", "items": {"type": "string"},
                            "description": "Command and arguments to run "
                                           "inside the room."},
                "pid": {"type": "boolean",
                        "description": "New PID namespace (default true)."},
                "proc": {"type": "boolean",
                         "description": "Mount a fresh /proc (default "
                                        "false; some hosts refuse it)."},
                "net": {"type": "boolean",
                        "description": "New network namespace: the "
                                       "room has only loopback (up) "
                                       "and cannot see the host's "
                                       "interfaces (default false)."},
                "cpus": {"type": "string",
                         "description": "Pin the command to these "
                                        "CPUs, e.g. \"0\", \"0-1\", "
                                        "\"0,2\" (sched_setaffinity)."},
                "devices": {"type": "array",
                            "items": {"type": "string"},
                            "description": "Host device nodes to "
                                           "bind into the room's "
                                           "/dev, e.g. [\"/dev/null\"] "
                                           "or a GPU set "
                                           "([\"/dev/nvidia0\", "
                                           "\"/dev/nvidiactl\", "
                                           "\"/dev/nvidia-uvm\"]). A "
                                           "device missing on the "
                                           "host is refused by name."},
                "workdir": {"type": "string",
                            "description": "Working directory inside the "
                                           "room."},
                "timeout_sec": {"type": "integer",
                                "description": "Timeout in seconds "
                                               "(default 15, max 120)."},
            },
            "required": ["command"],
        },
        "handler": tool_run,
    },
    {
        "name": "lwrap_view",
        "description": "Print a room's currently composed identity from "
                       "DIR/.lwrap.lnk (lwrap --view), without entering "
                       "the room.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dir": {"type": "string",
                        "description": "Room directory holding .lwrap.lnk. "
                                       "Give either dir or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms); resolves to "
                                        "that room's dir."},
            },
        },
        "handler": tool_view,
    },
    {
        "name": "lwrap_put",
        "description": "Store a file onto a room's floor (write into "
                       "its rootfs at a room-absolute path, e.g. "
                       "/work/data.csv). Parent directories are "
                       "created. The resolved path must stay inside "
                       "the room: escapes via '..' or symlinks are "
                       "refused by name. Cap 256 KiB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute destination, "
                                        "e.g. /work/data.csv."},
                "content": {"type": "string",
                            "description": "File content as text "
                                           "(UTF-8)."},
                "content_base64": {"type": "string",
                                   "description": "File content as "
                                                  "base64 (for binary)."},
            },
            "required": ["path"],
        },
        "handler": tool_put,
    },
    {
        "name": "lwrap_get",
        "description": "Load a file from a room's floor. Returns the "
                       "content (UTF-8 text, or base64 for binary), "
                       "its size and sha256, and a kind. Text vs "
                       "binary is judged by the zero-check mechanism: "
                       "no NUL byte means text, a NUL byte means "
                       "binary. The resolved path must stay inside "
                       "the room: a symlink pointing outside is "
                       "refused by name. Cap 256 KiB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute path to read."},
            },
            "required": ["path"],
        },
        "handler": tool_get,
    },
    {
        "name": "lwrap_ls",
        "description": "List a directory inside a room (names, types, "
                       "sizes), without entering the room.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute directory "
                                        "(default /)."},
            },
        },
        "handler": tool_ls,
    },
    {
        "name": "lwrap_read",
        "description": "Read a line-carry file by line-number "
                       "coordinates. The file's records are "
                       "'N\\ttext' lines, each carrying its own "
                       "number, so their physical order in the file "
                       "does not matter (the bag may be shuffled). "
                       "Returns the requested range in number order, "
                       "plus a census (record count, number range, "
                       "gaps). Missing or duplicate numbers are "
                       "refused by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute path of the "
                                        "line-carry file."},
                "start": {"type": "integer",
                          "description": "First line number "
                                         "(default 1)."},
                "end": {"type": "integer",
                        "description": "Last line number, inclusive "
                                       "(default: the largest number)."},
            },
            "required": ["path"],
        },
        "handler": tool_read,
    },
    {
        "name": "lwrap_read8",
        "description": "Read an address space whose lines are 8 "
                       "bytes. Lines are numbered from 1 (line 1 is "
                       "the first line, as in lwrap_read); the byte "
                       "offset of line n is 8*(n-1), and line 0 is "
                       "refused by name. Range is [start, end], both "
                       "inclusive. Two forms: raw8 -- the file itself "
                       "is the lines and reading seeks arithmetically "
                       "(no scan, no index); bag8 -- a header (magic, "
                       "total length, sha256) plus records of "
                       "[tag 8B][data 8B] whose physical order is "
                       "free; a complete bag is digest-verified on "
                       "every read when settled (clean bag: header; "
                       "fragmented bag: its defrag fold). Records "
                       "are generations -- a later record wins, a "
                       "tombstone deletes. Returns base64 content "
                       "plus a kind judged by the zero-check (no "
                       "NUL byte = text). Missing and out-of-range "
                       "lines are refused by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute path of the "
                                        "raw8 or bag8 file."},
                "start": {"type": "integer",
                          "description": "First line number, from 1 "
                                         "(default 1)."},
                "end": {"type": "integer",
                        "description": "Last line number, inclusive "
                                       "(default: the last line)."},
            },
            "required": ["path"],
        },
        "handler": tool_read8,
    },
    {
        "name": "lwrap_store8",
        "description": "Write one 8-byte line into a bag8 file by "
                       "appending a new record (a new generation). "
                       "Existing records never move, so writes "
                       "deliberately fragment the bag; a later "
                       "record for the same line wins. Creates the "
                       "bag (header included) if absent. Fold the "
                       "bag with lwrap_defrag to settle its digest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute path of the "
                                        "bag8 file."},
                "line": {"type": "integer",
                         "description": "Line number, from 1."},
                "data_base64": {"type": "string",
                                "description": "The line's bytes "
                                               "(1..8), base64."},
            },
            "required": ["path", "line", "data_base64"],
        },
        "handler": tool_store8,
    },
    {
        "name": "lwrap_drop8",
        "description": "Delete one line from a bag8 file by "
                       "appending a tombstone record. No record "
                       "moves; the line reads as missing afterwards. "
                       "Refuses by name if the line is not present.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute path of the "
                                        "bag8 file."},
                "line": {"type": "integer",
                         "description": "Line number, from 1."},
            },
            "required": ["path", "line"],
        },
        "handler": tool_drop8,
    },
    {
        "name": "lwrap_defrag",
        "description": "Defragment a bag8 file the Link way: fold "
                       "its record chain once into a ledger (a "
                       ".fold.json beside the bag: line -> winning "
                       "record offset, logical digest, gaps). The "
                       "bag's bytes are not modified at all -- the "
                       "folded ledger is the defragmented entity, "
                       "and lwrap_read8 verifies against it until "
                       "the bag grows again.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute path of the "
                                        "bag8 file."},
            },
            "required": ["path"],
        },
        "handler": tool_defrag,
    },
    {
        "name": "lwrap_rm",
        "description": "Remove a file from a room's floor, together "
                       "with its .fold.json ledger if one exists. "
                       "Files only; directories are refused by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs. "
                                          "Give either rootfs or room."},
                "room": {"type": "string",
                         "description": "Alias from the rooms registry "
                                        "(see lwrap_rooms)."},
                "path": {"type": "string",
                         "description": "Room-absolute path of the "
                                        "file to remove."},
            },
            "required": ["path"],
        },
        "handler": tool_rm,
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


# ------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ------------------------------------------------------------------

def make_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def handle(request):
    """Return a response dict, or None for notifications."""
    if not isinstance(request, dict):
        return make_error(None, -32600, "invalid_request: not an object")
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    is_notification = "id" not in request

    if method == "initialize":
        client_version = params.get("protocolVersion")
        version = (client_version if client_version in SUPPORTED_VERSIONS
                   else PROTOCOL_VERSION)
        return make_result(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if is_notification:
        # notifications/initialized and friends: accept silently.
        return None
    if method == "ping":
        return make_result(req_id, {})
    if method == "tools/list":
        listing = [{k: t[k] for k in ("name", "description", "inputSchema")}
                   for t in TOOLS]
        return make_result(req_id, {"tools": listing})
    if method == "tools/call":
        name = params.get("name")
        tool = TOOL_BY_NAME.get(name)
        if tool is None:
            return make_result(req_id, {
                "content": [{"type": "text",
                             "text": "unknown_tool: %s" % name}],
                "isError": True})
        try:
            text, structured, is_error = tool["handler"](
                params.get("arguments") or {})
        except Exception as e:  # a tool bug must not kill the server
            text, structured, is_error = (
                "tool_exception: %s: %s" % (type(e).__name__, e), None, True)
        result = {"content": [{"type": "text", "text": text}],
                  "isError": is_error}
        if structured is not None:
            result["structuredContent"] = structured
        return make_result(req_id, result)
    if method == "resources/list":
        return make_result(req_id, {"resources": []})
    if method == "prompts/list":
        return make_result(req_id, {"prompts": []})
    return make_error(req_id, -32601, "method_not_found: %s" % method)


def serve(instream, outstream):
    for line in instream:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            response = make_error(None, -32700, "parse_error")
            outstream.write(json.dumps(response) + "\n")
            outstream.flush()
            continue
        if isinstance(payload, list):  # batch: answer each, skip notifications
            responses = [r for r in (handle(item) for item in payload)
                         if r is not None]
            if responses:
                outstream.write(json.dumps(responses) + "\n")
                outstream.flush()
            continue
        response = handle(payload)
        if response is not None:
            outstream.write(json.dumps(response) + "\n")
            outstream.flush()


def main():
    log("serving on stdio (dir=%s)" % HERE)
    try:
        serve(sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
