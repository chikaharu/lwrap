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

- Built `lwrap` ELF: **14,384 bytes** (static, no interpreter, no libc)
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
./lwrap --root <rootfs> [--proc] [--pid] [--workdir DIR] -- CMD...
./lwrap --view DIR        # print a room's composed identity from DIR/.lwrap.lnk
./lwrap -v DIR            # same as --view
```

- The environment is **not** inherited: the `exec_cmd` edge installs a
  minimal generated environment (`PATH`, `HOME`, `TERM`).
- `--proc` and `--pid` enable the optional `mount_proc` / `unshare_pid`
  edges. On hosts where mounting a fresh `/proc` is not permitted, the
  `mount_proc` edge fails by name (see Limitations).
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
| 6 | `unshare_pid` *(optional, `--pid`)* | `unshare(CLONE_NEWPID)` + fork; parent propagates the exit code |
| 7 | `mount_private_root` | `mount(none, /, MS_REC\|MS_PRIVATE)` |
| 8 | `bind_rootfs` | bind-mount the rootfs onto itself |
| 9 | `mount_proc` *(optional, `--proc`)* | mount a fresh `/proc` |
| 10 | `chroot_rootfs` | `chroot(rootfs)` |
| 11 | `chdir_work` | `chdir(workdir)` |
| 12 | `exec_cmd` | `execve` with a `/bin`, `/usr/bin`, … search and the minimal env |

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

## Limitations

- x86-64 only.
- `chroot`, not `pivot_root`.
- No `/dev` construction: the room gets the rootfs's `/dev` as-is.
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
| `lwrap_run` | Run a command inside a room (`--root`, optional `--pid` / `--proc` / `--workdir`); returns stdout, stderr, exit code |
| `lwrap_view` | Print a room's composed identity from `DIR/.lwrap.lnk`, without entering it |
| `lwrap_put` | Store a file onto a room's floor (write into its rootfs); returns size and sha256 |
| `lwrap_get` | Load a file from a room's floor (text, or base64 for binary); returns size and sha256 |
| `lwrap_ls` | List a directory inside a room, without entering it |
| `lwrap_read` | Read a line-carry file by line-number coordinates (below) |
| `lwrap_read8` | Read an address space of 8-byte lines (below) |

With put/get/ls an AI client can do file work end-to-end inside a
room: store data and a script, run the script in isolation with
`lwrap_run`, and load the result back. File paths are room-absolute
and containment is enforced on the resolved real path: a `..` that
climbs above the room's root, and a symlink planted inside the room
pointing outside, are both refused by name (`path_escapes_room`).
Put/get are capped at 256 KiB per file.

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
is cut into 8-byte lines, the line number *is* the address, and the
byte offset is `8 * n`. `lwrap_read8` reads a range `[start, end)`
of line numbers in two forms:

- **raw8** — the file itself is the lines. Reading seeks
  arithmetically: no scan, no index table. Measured: the same
  16-line read costs the same on a 32 KiB and a 256 KiB file
  (ratio 1.06; a scanning reader would pay ~8x). Overhead: zero.
- **bag8** — a 48-byte header (magic, total byte length, sha256)
  plus records of `[tag 8B][data 8B]`, each line explicitly
  carrying its number, so records may lie in any physical order.
  A complete bag is digest-verified on every read; a dropped
  record refuses as `missing_line_number` only for ranges that
  cover it, a duplicate as `duplicate_line_number`, and any
  flipped data byte as `digest_mismatch`. Cost: exactly 2.001x —
  the symmetric point of carrying an 8-byte tag per 8 bytes of
  data. Placement freedom is bought with the tag; in-place is free
  because the number is arithmetic.

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
mcp_server.py      stdlib-only MCP server exposing lwrap as 9 tools
rooms.example.json example room-alias registry (copy to rooms.json)
test_mcp.py        stdlib smoke test for the MCP server
docs/              format spec and measured results
lwrap_gen.c        generated by the build (git-ignored)
lwrap              built ELF (git-ignored)
```
