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
      == ["lwrap_build", "lwrap_defrag", "lwrap_drop8", "lwrap_get",
          "lwrap_ls", "lwrap_put", "lwrap_read", "lwrap_read8",
          "lwrap_rm", "lwrap_rooms", "lwrap_run", "lwrap_store8",
          "lwrap_view"],
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

# Line-carry read: a shuffled bag of numbered records reads back in
# number order; a plain file refuses by name.
r = call("lwrap_put", {"room": "scratch", "path": "/nums.lcarry",
                       "content": "3\tthree\n1\tone\n2\ttwo\n"})
r = call("lwrap_read", {"room": "scratch", "path": "/nums.lcarry",
                        "start": 1, "end": 2})
info = json.loads(r["content"][0]["text"]) if not r.get("isError") else {}
check("lwrap_read shuffled bag", info.get("text") == "one\ntwo\n",
      json.dumps(info)[:80])
r = call("lwrap_read", {"room": "scratch", "path": "/work/data.txt"})
check("lwrap_read plain refused", r.get("isError") is True
      and "not_line_carry_format" in r["content"][0]["text"],
      r["content"][0]["text"][:80])

# 8-byte lines: line n is at byte offset 8*n; a shuffled bag of
# [tag 8B][data 8B] records under a 48-byte header reads back whole.
import base64 as _b64
import hashlib as _hl
import struct as _st
blob = bytes(range(256)) * 2  # 512 bytes = 64 lines
r = call("lwrap_put", {"room": "scratch", "path": "/raw.bin",
                       "content_base64": _b64.b64encode(blob).decode()})
r = call("lwrap_read8", {"room": "scratch", "path": "/raw.bin",
                         "start": 11, "end": 12})
info = json.loads(r["content"][0]["text"]) if not r.get("isError") else {}
check("lwrap_read8 raw8 seek (lines from 1)", _b64.b64decode(
    info.get("content_base64", "")) == blob[80:96], json.dumps(info)[:80])
r = call("lwrap_read8", {"room": "scratch", "path": "/raw.bin",
                         "start": 0, "end": 1})
check("lwrap_read8 line 0 refused", r.get("isError") is True
      and "line_out_of_range" in r["content"][0]["text"],
      r["content"][0]["text"][:80])
recs = [_st.pack("<Q", n) + blob[8 * (n - 1):8 * n] for n in range(1, 65)]
recs.reverse()
bag = b"LCARRY8\x00" + _st.pack("<Q", len(blob)) \
    + _hl.sha256(blob).digest() + b"".join(recs)
r = call("lwrap_put", {"room": "scratch", "path": "/bag.bag8",
                       "content_base64": _b64.b64encode(bag).decode()})
r = call("lwrap_read8", {"room": "scratch", "path": "/bag.bag8"})
info = json.loads(r["content"][0]["text"]) if not r.get("isError") else {}
check("lwrap_read8 bag8 shuffled", _b64.b64decode(
    info.get("content_base64", "")) == blob
    and info.get("digest_verified") is True, json.dumps(info)[:80])

# Write side: store8 appends generations, drop8 tombstones,
# defrag folds without touching the bag, rm removes file + fold.
import hashlib as _h2
for _n, _d in [(2, b"line-two"), (1, b"line-one"), (3, b"line-3!!")]:
    r = call("lwrap_store8", {"room": "scratch", "path": "/w.bag8",
                              "line": _n,
                              "data_base64": _b64.b64encode(_d).decode()})
call("lwrap_store8", {"room": "scratch", "path": "/w.bag8", "line": 2,
                      "data_base64": _b64.b64encode(b"GEN2!!!!").decode()})
r = call("lwrap_read8", {"room": "scratch", "path": "/w.bag8",
                         "start": 1, "end": 3})
info = json.loads(r["content"][0]["text"]) if not r.get("isError") else {}
check("store8 generations resolve", _b64.b64decode(
    info.get("content_base64", ""))
    == b"line-oneGEN2!!!!line-3!!", json.dumps(info)[:90])
_bag_host = os.path.join(_scratch, "w.bag8")
_sha0 = _h2.sha256(open(_bag_host, "rb").read()).hexdigest()
r = call("lwrap_defrag", {"room": "scratch", "path": "/w.bag8"})
_sha1 = _h2.sha256(open(_bag_host, "rb").read()).hexdigest()
r = call("lwrap_read8", {"room": "scratch", "path": "/w.bag8",
                         "start": 1, "end": 3})
info = json.loads(r["content"][0]["text"]) if not r.get("isError") else {}
check("defrag folds, bag untouched, verified", _sha0 == _sha1
      and info.get("digest_verified") is True
      and info.get("digest_via") == "fold", json.dumps(info)[:90])
call("lwrap_drop8", {"room": "scratch", "path": "/w.bag8", "line": 2})
r = call("lwrap_read8", {"room": "scratch", "path": "/w.bag8",
                         "start": 1, "end": 3})
check("drop8 tombstone refuses by name", r.get("isError") is True
      and "missing_line_number: 2" in r["content"][0]["text"],
      r["content"][0]["text"][:80])
r = call("lwrap_rm", {"room": "scratch", "path": "/w.bag8"})
check("rm removes file and fold", not os.path.exists(_bag_host)
      and not os.path.exists(_bag_host + ".fold.json"), "")

# Zero-check mechanism: "a\x00b" decodes as UTF-8 but is binary.
r = call("lwrap_put", {"room": "scratch", "path": "/nul.bin",
                       "content_base64": _b64.b64encode(b"a\x00b").decode()})
r = call("lwrap_get", {"room": "scratch", "path": "/nul.bin"})
info = json.loads(r["content"][0]["text"]) if not r.get("isError") else {}
check("zero-check: NUL means binary", info.get("kind") == "binary"
      and info.get("encoding") == "base64", json.dumps(info)[:90])
r = call("lwrap_get", {"room": "scratch", "path": "/work/data.txt"})
info = json.loads(r["content"][0]["text"]) if not r.get("isError") else {}
check("zero-check: no NUL means text", info.get("kind") == "text",
      json.dumps(info)[:90])
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
