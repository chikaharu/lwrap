#!/usr/bin/env python3
# mcp_server.py -- lwrap as an MCP (Model Context Protocol) server.
#
# A stdlib-only MCP server over stdio (newline-delimited JSON-RPC 2.0).
# The server lives on the host; every unit of work happens *inside a
# room*, through lwrap:
#
#   lwrap_build  build the lwrap ELF from a pole spec (sf_builder.py)
#   lwrap_run    run a command inside a room (lwrap --root ... -- CMD)
#   lwrap_view   print a room's composed identity (lwrap --view DIR)
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

import hashlib
import json
import os
import subprocess
import sys

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_VERSIONS = ["2024-11-05", "2025-03-26", "2025-06-18"]
SERVER_NAME = "lwrap"
SERVER_VERSION = "1.0.0"

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
    if not rootfs:
        return ("missing_argument: rootfs", None, True)
    command = args.get("command")
    if not command or not isinstance(command, list):
        return ("missing_argument: command (a non-empty array)", None, True)
    timeout = args.get("timeout_sec") or DEFAULT_TIMEOUT
    try:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        return ("bad_argument: timeout_sec must be an integer", None, True)
    argv = [LWRAP, "--root", rootfs]
    if args.get("pid", True):
        argv.append("--pid")
    if args.get("proc", False):
        argv.append("--proc")
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
    if not room:
        return ("missing_argument: dir", None, True)
    proc = subprocess.run([LWRAP, "--view", str(room)],
                          stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=30)
    text = _clip(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        return ("view_refused (rc=%d): %s" % (proc.returncode, text.strip()),
                None, True)
    return (text, {"view": text}, False)


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
        "name": "lwrap_run",
        "description": "Run a command inside an lwrap room "
                       "(user/mount namespaces + chroot into rootfs). "
                       "Returns stdout, stderr and the exit code. A failed "
                       "edge is reported by name (isError).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rootfs": {"type": "string",
                           "description": "Path to the room's rootfs."},
                "command": {"type": "array", "items": {"type": "string"},
                            "description": "Command and arguments to run "
                                           "inside the room."},
                "pid": {"type": "boolean",
                        "description": "New PID namespace (default true)."},
                "proc": {"type": "boolean",
                         "description": "Mount a fresh /proc (default "
                                        "false; some hosts refuse it)."},
                "workdir": {"type": "string",
                            "description": "Working directory inside the "
                                           "room."},
                "timeout_sec": {"type": "integer",
                                "description": "Timeout in seconds "
                                               "(default 15, max 120)."},
            },
            "required": ["rootfs", "command"],
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
                        "description": "Room directory holding .lwrap.lnk."},
            },
            "required": ["dir"],
        },
        "handler": tool_view,
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
