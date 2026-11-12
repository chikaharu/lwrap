# RESULTS — ネットワークとデバイス (CPU, CUDA)（2026-11-12）

予測は PREDICTION.md（測定前に固定）。対象 ELF は新しい
lwrap 本体で、サイズは **18,680 バイト**（従来 14,384、
エッジ3件と引数解析で +4,296）。

## 較正

- C1 ホストの ip link = lo＋host0、Cpus_allowed_list 0-3 を確認
- C2 室内の確認は ip link と静的プローブ affprobe
  （785,664B、テスト rootfs の /bin に配置）で行った
- C3 不在デバイスは名前つきで読めた（N5）

## 結果（全 PASS）

- N1 既定: 室内の ip link はホストと同じ lo＋host0（回帰なし）
- N2 --net: 室内は lo のみで UP。ホストで立てた
  127.0.0.1:18777 のリスナーは、共有時は reachable、
  --net 時は unreachable。名前空間は実際に分かれた
- N3 --cpus: affprobe の報告が 既定 0,1,2,3 → `--cpus 0` で 0、
  `--cpus 1-2` で 1,2。sched_setaffinity が室内で効いた
- N4 --device /dev/null: 室内でキャラクタデバイスとして
  存在し書き込み可。指定しない /dev/zero は不在のまま
  ——部屋は与えられたデバイスだけを見る
- N5 CUDA: このホストに GPU は無い。`--device /dev/nvidia0` は
  `edge bind_device failed (errno=2)`、終了コード 111 で
  名前つき拒否。CUDA の配線（デバイス一覧を bind する形）は
  実装済みだが、実機の GPU パススルーは未検証として残る
- N6 回帰: 既定 run（uid=0・PID=1）と MCP スモーク
  （test_mcp.py、lwrap_build の期待サイズを 18,680 に更新）
  が OVERALL PASS

## 自前ミス（測定器でなく生成器の側、記録として残す）

初回の --net は分離していなかった。原因は生成 C の定数ミス:
CLONE_NEWNET を 0x00040000 と書いた（正しくは 0x40000000。
0x00040000 は CLONE_NEWUTS）。unshare は成功を返し、部屋は
ホストのネット名前空間に残り続けた。切り分けの過程で
「ioctl で lo を上げると EPERM」という二次症状に長く引っ張
られたが、あれは部屋がホスト側ネット名前空間に居たため
（他人の名前空間の lo は操作できない）で、本体は定数1つの
取り違えだった。N2 の「インターフェースを数える」較正が
無ければ、成功を返す見かけに騙されるところだった。
修正後は上記の通り全 PASS。

## 残る境界

- --net は loopback のみの隔離。veth や NAT は無いので、
  --net の部屋は外と一切通信できない（共有か、完全隔離か）
- CPU は固定（affinity）のみで、cgroup の quota は無い
  （このホストでは cgroup 委譲が未確認のため、確実に効く側
  を選んだ、という PREDICTION 時点の判断のまま）
- GPU 実機は未検証。ドライバのライブラリ類（libcuda 等）は
  デバイスでなく通常ファイルなので、要れば rootfs 側に置く
