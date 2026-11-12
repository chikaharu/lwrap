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
| `lwrap_run` | Run a command inside a room (`--root`, optional `--pid` / `--proc` / `--workdir`); returns stdout, stderr, exit code |
| `lwrap_view` | Print a room's composed identity from `DIR/.lwrap.lnk`, without entering it |

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
mcp_server.py      stdlib-only MCP server exposing lwrap as 3 tools
test_mcp.py        stdlib smoke test for the MCP server
docs/              format spec and measured results
lwrap_gen.c        generated by the build (git-ignored)
lwrap              built ELF (git-ignored)
```
