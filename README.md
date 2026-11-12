# lwrap

A tiny, **generated** sandbox launcher for Linux (x86-64) — `bwrap` rewritten
in Link-theory edge notation.

`lwrap` is not hand-written C. A builder, `sf_builder.py`, takes a *pole
specification* (`spec_lwrap.json`) that declares the interface of a "room"
(an isolated process environment) and the ordered *edges* that glue its
outside to its inside — named syscall steps such as `unshare_user`,
`write_uid_map`, `bind_rootfs` and `chroot_rootfs`. The builder validates
the spec (edge order and dependencies are checked, and invalid specs are
refused by name), generates C (`lwrap_gen.c`), and compiles it into a
static, `-nostdlib` ELF that uses raw syscalls only.

## Size

- Built `lwrap` ELF: **18,680 bytes** (static, no interpreter, no libc)
- For comparison: `bwrap` 72,160 bytes, `proot` 183,464 bytes

## Build

```sh
python3 sf_builder.py spec_lwrap.json --outdir .
# -> lwrap_gen.c (generated, do not edit)
# -> lwrap        (static ELF)
```

Requires Python 3 and gcc. x86-64 Linux only (the generated C is guarded
with `#if !defined(__x86_64__)`).

## Usage

```sh
./lwrap --root <rootfs> [--proc] [--pid] [--net] [--workdir DIR] [--cpus LIST] [--device PATH]... -- CMD...
./lwrap --view DIR        # print a room's composed identity from DIR/.lwrap.lnk
./lwrap -v DIR            # same as --view
```

- The environment is **not** inherited: the `exec_cmd` edge installs a
  minimal generated environment (`PATH`, `HOME`, `TERM`).
- `--proc` and `--pid` enable the optional `mount_proc` / `unshare_pid`
  edges. On hosts where mounting a fresh `/proc` is not permitted, the
  `mount_proc` edge fails by name (see Limitations).
- `--net` gives the room its own network namespace: only loopback
  (brought up) exists inside; the host's interfaces are invisible
  and unreachable. Without it, the network is shared with the host.
- `--cpus LIST` (e.g. `0`, `0-1`, `0,2`) pins the command to those
  CPUs with `sched_setaffinity`. This is pinning, not a quota: no
  cgroup is involved.
- `--device PATH` (repeatable; or the spec's `devices` parameter)
  bind-mounts a host device node into the room's `/dev`. The room
  sees exactly the devices it is given. This is how a GPU is handed
  in — e.g. `--device /dev/nvidia0 --device /dev/nvidiactl
  --device /dev/nvidia-uvm` (the driver libraries are ordinary
  files; ship them in the rootfs). A device that does not exist on
  the host fails the `bind_device` edge by name.
- The default rootfs compiled in from the spec (`./rootfs`) is only a
  default; `--root` overrides it at run time.

## Failure behaviour

A failed edge is reported by name and the process exits with code 111:

```
lwrap: edge bind_rootfs failed (errno=2)
```

`--view` refusals are also named, e.g. `no .lwrap.lnk in room dir`,
with exit code 65.

## How it works

The spec declares one pole:

- **Inputs:** argv (`--root`, `--proc`, `--pid`, `--workdir`, `-- CMD...`),
  fds 0/1/2. Env is generated, not inherited.
- **Outputs:** the command's stdout/stderr, inherited unchanged; the
  command's exit code, propagated (also through the `unshare_pid` fork).
- **Parameters:** rootfs, uid/gid (mapped to 0 inside), workdir, and the
  optional proc/pid edges.
- **States:** host process → user-namespace mapped → private mount tree →
  inside rootfs (chroot) → command process.

Edges, in the only legal order (enforced by the builder):

| # | Edge | What it generates |
|---|------|-------------------|
| 1 | `unshare_user` | `unshare(CLONE_NEWUSER)`, parent/child synchronisation |
| 2 | `deny_setgroups` | write `deny` to `setgroups` (parent side) |
| 3 | `write_uid_map` | map inner uid 0 to the caller's uid |
| 4 | `write_gid_map` | same for gid |
| 5 | `unshare_mount` | `unshare(CLONE_NEWNS)` |
| 6 | `unshare_net` *(optional, `--net`)* | `unshare(CLONE_NEWNET)`; loopback brought up by ioctl |
| 7 | `unshare_pid` *(optional, `--pid`)* | `unshare(CLONE_NEWPID)` + fork; parent propagates the exit code |
| 8 | `mount_private_root` | `mount(none, /, MS_REC\|MS_PRIVATE)` |
| 9 | `bind_rootfs` | bind-mount the rootfs onto itself |
| 10 | `mount_proc` *(optional, `--proc`)* | mount a fresh `/proc` |
| 11 | `bind_device` *(optional, `--device`)* | bind host device nodes into `rootfs/dev` |
| 12 | `chroot_rootfs` | `chroot(rootfs)` |
| 13 | `chdir_work` | `chdir(workdir)` |
| 14 | `pin_cpus` *(optional, `--cpus`)* | `sched_setaffinity` to the named CPUs |
| 15 | `exec_cmd` | `execve` with a `/bin`, `/usr/bin`, … search and the minimal env |

Because this environment forbids a child writing its own `uid_map`, the
`uid_map`/`gid_map` edges are generated as parent-side helpers — the same
division of labour `bwrap` uses.

`--view` reads a room directory's `.lwrap.lnk` declaration (see
[`docs/LNK_FORMAT.md`](docs/LNK_FORMAT.md)) and prints the room's currently
composed identity (soul/memory records and binds) without entering the
room or touching any namespace.

## Measured results

Experiments were run with predictions fixed *before* measurement
(n=20 per arm, same session, an Alpine rootfs running a recorded
room test):

| Launcher | Median | Output |
|----------|--------|--------|
| chroot (no isolation) | 0.0500 s | `800 / ROOM PASS` |
| bwrap | 0.0622 s | `800 / ROOM PASS` |
| **lwrap** | **0.0483 s** | `800 / ROOM PASS` |

Isolation probes for lwrap: 3/3 (uid 0 inside, host-only marker file
invisible, shell is PID 1 in the new PID namespace). One pre-registered
prediction missed honestly: the lwrap/bwrap median ratio was 0.78,
just below the predicted 0.8–1.5 band — recorded as a miss, not rounded
into a hit.

Full write-ups, including failure modes:

- [`docs/RESULTS-2026-11-10.md`](docs/RESULTS-2026-11-10.md) — design, calibration, failure forms
- [`docs/RESULTS-view-2026-11-10.md`](docs/RESULTS-view-2026-11-10.md) — the `--view` / dynamic room identity extension
- [`docs/RESULTS-netdev-2026-11-12.md`](docs/RESULTS-netdev-2026-11-12.md) — network namespace, CPU pinning and device binds (incl. an honest record of the wrong clone-flag constant that the pre-registered census caught)

## Limitations

- x86-64 only.
- `chroot`, not `pivot_root`.
- `/dev` is the rootfs's as-is, plus exactly the devices bound in
  with `--device`; there is no general device construction.
- Network is binary: shared with the host (default), or `--net`
  loopback-only isolation. There is no veth/NAT, so a `--net` room
  cannot reach the outside at all.
- CPU control is pinning only (`sched_setaffinity`); there are no
  cgroup quotas.
- GPU hand-in is implemented as device binds but is **unverified on
  real hardware** (the development host has no GPU); a missing
  device node is refused by name.
- Mounting a fresh `/proc` (`--proc`) may be refused by the host
  (EPERM); the edge then fails by name and the run stops.
- This is a research artefact from the Link-theory project: a minimal,
  spec-generated launcher, not a drop-in `bwrap` replacement.

## As an MCP server

`mcp_server.py` exposes lwrap over the Model Context Protocol
(stdio transport, newline-delimited JSON-RPC 2.0, protocol version
2024-11-05). It is **Python stdlib only** — no MCP SDK required.
The server lives on the host; every unit of work happens inside a
room, through lwrap. MCP is the plumbing; lwrap is the room.

```sh
python3 mcp_server.py        # serve on stdio (build lwrap first)
```

Client configuration example:

```json
{"mcpServers": {"lwrap": {
    "command": "python3",
    "args": ["/path/to/lwrap/mcp_server.py"]}}}
```

Tools:

| Tool | What it does |
|------|--------------|
| `lwrap_build` | Build the lwrap ELF from a pole spec; returns size and sha256 |
| `lwrap_rooms` | List the registered room aliases (see below) |
| `lwrap_run` | Run a command inside a room (`--root`, optional `--pid` / `--proc` / `--net` / `--workdir` / `cpus` / `devices`); returns stdout, stderr, exit code |
| `lwrap_view` | Print a room's composed identity from `DIR/.lwrap.lnk`, without entering it |
| `lwrap_put` | Store a file onto a room's floor (write into its rootfs); returns size and sha256 |
| `lwrap_get` | Load a file from a room's floor (text, or base64 for binary); returns size and sha256 |
| `lwrap_ls` | List a directory inside a room, without entering it |
| `lwrap_read` | Read a line-carry file by line-number coordinates (below) |
| `lwrap_read8` | Read an address space of 8-byte lines (below) |
| `lwrap_store8` | Write one 8-byte line into a bag (append a generation) |
| `lwrap_drop8` | Delete one line from a bag (append a tombstone) |
| `lwrap_defrag` | Fold a bag's record chain into a ledger; the bag is untouched |
| `lwrap_rm` | Remove a file (and its fold ledger) from a room |

With put/get/ls an AI client can do file work end-to-end inside a
room: store data and a script, run the script in isolation with
`lwrap_run`, and load the result back. File paths are room-absolute
and containment is enforced on the resolved real path: a `..` that
climbs above the room's root, and a symlink planted inside the room
pointing outside, are both refused by name (`path_escapes_room`).
Put/get are capped at 256 KiB per file. Whether a file counts as
text or binary is judged by the **zero-check mechanism**: no NUL
byte means text, a NUL byte means binary — even when the bytes
happen to decode as UTF-8 (`"a\0b"` is binary). `lwrap_get` and
`lwrap_read8` report the result as a `kind` field.

### Line-carry reading

A line-carry file (`.lcarry`) stores each line as a record
`N<TAB>text`: every line *carries its own line number*, so the file
is an address space keyed by those numbers, not by byte position.
`lwrap_read` loads a range `[start, end]` by number — the records'
physical order in the file is irrelevant (the bag may be shuffled,
and is reconstructed exactly), a missing number is refused as
`missing_line_number: N`, a duplicate as `duplicate_line_number: N`,
and a file without number headers as `not_line_carry_format`.
Measured on a shuffled 1,000-line file: range and full reads are
byte-identical to the original, the format overhead is 1.097x, and
head vs. tail range reads cost the same (ratio 1.10).

### 8-byte lines

Generalize the unit from a text line to 8 bytes: the address space
is cut into 8-byte lines, and the line number *is* the address.
Lines are numbered from 1 in both readers (line 1 is the first
line; line 0 is refused by name), the byte offset of line *n* is
`8 * (n - 1)`, and ranges are inclusive. `lwrap_read8` reads lines
in two forms:

- **raw8** — the file itself is the lines. Reading seeks
  arithmetically: no scan, no index table. Measured: the same
  16-line read costs the same on a 32 KiB and a 256 KiB file
  (ratio 1.06; a scanning reader would pay ~8x). Overhead: zero.
- **bag8** — a 48-byte header (magic, total byte length, sha256)
  plus records of `[tag 8B][data 8B]` (tags are the 1-based line
  numbers), each line explicitly
  carrying its number, so records may lie in any physical order.
  A complete bag is digest-verified on every read; a dropped
  record refuses as `missing_line_number` only for ranges that
  cover it, a duplicate as `duplicate_line_number`, and any
  flipped data byte as `digest_mismatch`. Cost: exactly 2.001x —
  the symmetric point of carrying an 8-byte tag per 8 bytes of
  data. Placement freedom is bought with the tag; in-place is free
  because the number is arithmetic.

### Writing, deleting, and defrag (the Link fold)

`lwrap_store8` writes a line by *appending* a record — a new
generation; no existing record ever moves, so writes deliberately
fragment the bag, and a later record for the same line wins.
`lwrap_drop8` deletes by appending a tombstone record (the tag's
top bit). Reads resolve generations and tombstones, and report
how many superseded records (`generations`) they folded over.

`lwrap_defrag` defragments the Link way: it folds the record
chain once into a ledger file `<path>.fold.json` — an offsets
table (line → winning record), the logical digest, and the gap
list — **without modifying the bag's bytes at all**. The folded
ledger is the defragmented entity. Reads verify against the
ledger until the bag grows again (an append makes the fold
stale, and reads honestly report `digest_verified: false`).

Measured honestly: on a 2,000-line × 5-generation bag, folding
(11.2 ms) was *slower* than materialized compaction (6.6 ms) —
both pay the same full scan, and this bag's reads were never
chain walks, so the 2026 fold advantage does not apply here.
The fold's value in this form is not speed but that the bag
never changes: offsets stay stable and the digest settles in
place. `lwrap_rm` removes a file together with its ledger.

### Reading as fragments, always

`lwrap_read8` reads every bag *as a chain of fragments* and
resolves before loading — whether or not the bag is actually
fragmented. An ordered bag is the special case whose ledger is
a formula: if the bag is clean-shaped and each requested
record's tag matches its arithmetic offset `48 + 16*(n-1)`, the
read is served with no scan (`via: "arithmetic_fold"`; measured
0.27 ms at 20,000 lines vs 21.1 ms for the scan path, and flat
against a 10x size change). If a probe misses — the physical
order is not the logical order — the read falls back to full
generation resolution (`via: "scan"`), so a physically swapped
bag still reads correctly where a blind seek would silently
serve the wrong line. Verification moves to settlement: the
arithmetic path declares `digest_verified: false`, a fresh fold
ledger verifies in place, and `lwrap_defrag` itself refuses to
settle a clean bag whose content contradicts its header
(`digest_mismatch`). Defragging an ordered bag materializes
exactly the formula — every offset equals `48 + 16*(n-1)`.

### Room aliases

Rooms can be called by name instead of by absolute path. Create a
`rooms.json` beside the server (or point the `LWRAP_ROOMS_JSON`
environment variable at one) — see `rooms.example.json`:

```json
{"rooms": {
    "test":  {"rootfs": "/path/to/rootfs", "description": "a test room"},
    "rouka": {"dir": "/path/to/rooms/rouka", "description": "for view"}}}
```

An entry may carry `rootfs` (used by `lwrap_run`), `dir` (used by
`lwrap_view`), or both. Then:

- `lwrap_run` accepts `{"room": "test", "command": [...]}` instead of `rootfs`
- `lwrap_view` accepts `{"room": "rouka"}` instead of `dir`

The registry is re-read on every call, so edits take effect without
restarting the server: the registry is data on the floor, not state
in the server. Unknown aliases are refused by name, with the known
aliases listed (`unknown_room_alias: nope (known: rouka, test)`);
giving both `room` and a path is a `conflicting_arguments` refusal.
`rooms.json` itself is machine-specific and git-ignored.

A failed edge is a tool result with `isError: true` and the refusal
named in the text (e.g. `lwrap: edge bind_rootfs failed (errno=2)`) —
never a protocol error, and it never kills the server. Room commands
are spawned with stdin detached, so they cannot consume the MCP stream.

Measured with a stdlib test client (predictions fixed before the run):
handshake, tool listing and room execution all pass, the MCP-run
output is byte-identical to a direct lwrap run (`uid=0`, PID 1, host
marker invisible), a build through MCP returns the same sha256 as a
direct build, and the warm round-trip overhead was +0.2 ms (median,
n=10) over direct execution. Interoperability with the official MCP
Python SDK client was also verified (initialize / tools/list /
lwrap_run all pass). A smoke test is included as `test_mcp.py`.

## Layout

```
sf_builder.py      the builder (spec validation + C generation + build)
spec_lwrap.json    the pole spec (the source of truth for lwrap)
mcp_server.py      stdlib-only MCP server exposing lwrap as 13 tools
rooms.example.json example room-alias registry (copy to rooms.json)
test_mcp.py        stdlib smoke test for the MCP server
docs/              format spec and measured results
lwrap_gen.c        generated by the build (git-ignored)
lwrap              built ELF (git-ignored)
```
