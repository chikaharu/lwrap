#!/usr/bin/env python3
# test_mcp.py -- smoke test for mcp_server.py (stdlib only).
#
# Spawns the server on stdio and checks: handshake, tools/list,
# a build, a named refusal for a missing rootfs, and server survival.
# If LWRAP_TEST_ROOTFS points at a rootfs with /bin/sh, it also runs
# `id` inside the room and checks for uid=0.
#
#   python3 test_mcp.py
#   LWRAP_TEST_ROOTFS=/path/to/rootfs python3 test_mcp.py
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_id = [0]
failures = []

# A throwaway alias registry, so the alias tests do not depend on a
# local rooms.json: "demo" resolves to paths that do not exist, which
# is enough to prove resolution happened (the refusal names the edge).
# "scratch" is a real empty directory used as a room floor for the
# file-tool tests.
import tempfile
_scratch = tempfile.mkdtemp(prefix="lwrap-scratch-")
_reg = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"rooms": {"demo": {"rootfs": "/nonexistent-rootfs",
                              "dir": "/nonexistent-room",
                              "description": "demo alias"},
                     "scratch": {"rootfs": _scratch,
                                 "description": "scratch floor"}}}, _reg)
_reg.close()
os.environ["LWRAP_ROOMS_JSON"] = _reg.name


def check(name, ok, detail=""):
    print("%s %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        failures.append(name)


server = subprocess.Popen([sys.executable, os.path.join(HERE, "mcp_server.py")],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True)


def request(method, params=None):
    _id[0] += 1
    server.stdin.write(json.dumps(
        {"jsonrpc": "2.0", "id": _id[0], "method": method,
         "params": params or {}}) + "\n")
    server.stdin.flush()
    return json.loads(server.stdout.readline())


def call(name, arguments):
    return request("tools/call",
                   {"name": name, "arguments": arguments}).get("result") or {}


init = request("initialize", {"protocolVersion": "2024-11-05",
                              "capabilities": {},
                              "clientInfo": {"name": "test_mcp",
                                             "version": "0"}})
res = init.get("result") or {}
check("handshake", res.get("serverInfo", {}).get("name") == "lwrap",
      json.dumps(res.get("serverInfo")))
server.stdin.write(json.dumps(
    {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
server.stdin.flush()

tools = request("tools/list").get("result", {}).get("tools", [])
check("tools/list", sorted(t["name"] for t in tools)
      == ["lwrap_build", "lwrap_get", "lwrap_ls", "lwrap_put",
          "lwrap_rooms", "lwrap_run", "lwrap_view"],
      ",".join(t["name"] for t in tools))

r = call("lwrap_rooms", {})
info = json.loads(r["content"][0]["text"])
check("lwrap_rooms", [x["name"] for x in info["rooms"]]
      == ["demo", "scratch"], json.dumps(info["rooms"]))

# File tools on the scratch floor: put -> ls -> get round-trip,
# then the two escape refusals.
r = call("lwrap_put", {"room": "scratch", "path": "/work/data.txt",
                       "content": "hello room\n"})
info = json.loads(r["content"][0]["text"]) if not r.get("isError") else {}
check("lwrap_put", r.get("isError") is False
      and info.get("size_bytes") == 11, json.dumps(info))
r = call("lwrap_ls", {"room": "scratch", "path": "/work"})
info = json.loads(r["content"][0]["text"])
check("lwrap_ls", [e["name"] for e in info["entries"]] == ["data.txt"]
      and info["entries"][0]["size"] == 11, json.dumps(info["entries"]))
r = call("lwrap_get", {"room": "scratch", "path": "/work/data.txt"})
info = json.loads(r["content"][0]["text"])
check("lwrap_get", info.get("content") == "hello room\n",
      json.dumps(info)[:80])
r = call("lwrap_put", {"room": "scratch", "path": "/../evil.txt",
                       "content": "escape"})
check("put escape refused", r.get("isError") is True
      and "path_escapes_room" in r["content"][0]["text"]
      and not os.path.exists(os.path.join(
          os.path.dirname(_scratch), "evil.txt")),
      r["content"][0]["text"])
os.symlink("/etc/hostname", os.path.join(_scratch, "peek"))
r = call("lwrap_get", {"room": "scratch", "path": "/peek"})
check("get symlink escape refused", r.get("isError") is True
      and "path_escapes_room" in r["content"][0]["text"],
      r["content"][0]["text"])
r = call("lwrap_run", {"room": "demo", "command": ["/bin/true"]})
check("alias resolves (run)", r.get("isError") is True
      and "edge bind_rootfs failed" in r["content"][0]["text"],
      "demo -> /nonexistent-rootfs")
r = call("lwrap_run", {"room": "bogus", "command": ["/bin/true"]})
check("unknown alias refused", r.get("isError") is True
      and "unknown_room_alias: bogus" in r["content"][0]["text"],
      r["content"][0]["text"])
r = call("lwrap_run", {"room": "demo", "rootfs": "/x",
                       "command": ["/bin/true"]})
check("room+rootfs conflict refused", r.get("isError") is True
      and "conflicting_arguments" in r["content"][0]["text"],
      r["content"][0]["text"])
r = call("lwrap_view", {"room": "demo"})
check("alias resolves (view)", r.get("isError") is True
      and "no .lwrap.lnk" in r["content"][0]["text"],
      r["content"][0]["text"].strip()[:80])

r = call("lwrap_build", {})
info = {}
try:
    info = json.loads(r["content"][0]["text"])
except ValueError:
    pass
check("lwrap_build", r.get("isError") is False
      and info.get("size_bytes") == 14384, json.dumps(info))

r = call("lwrap_run", {"rootfs": "/nonexistent-rootfs",
                       "command": ["/bin/true"]})
check("named refusal", r.get("isError") is True
      and "edge bind_rootfs failed" in r["content"][0]["text"],
      r["content"][0]["text"].strip().splitlines()[-1][:80])
check("server alive", request("ping").get("result") == {})

rootfs = os.environ.get("LWRAP_TEST_ROOTFS")
if rootfs:
    r = call("lwrap_run", {"rootfs": rootfs, "command": ["/bin/sh", "-c", "id"]})
    out = json.loads(r["content"][0]["text"])
    check("room run", out.get("exit_code") == 0
          and "uid=0(root)" in out.get("stdout", ""), out.get("stdout", "").strip())
else:
    print("SKIP room run (set LWRAP_TEST_ROOTFS to enable)")

server.stdin.close()
server.wait(timeout=10)
print("OVERALL", "PASS" if not failures else "FAIL %s" % failures)
sys.exit(0 if not failures else 1)
