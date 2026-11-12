# 動的アイデンティティ構築 RESULTS（2026-11-10）

タヌさんの設計（2026-11-10）: 各部屋のアイデンティティ
ファイル（spec・メモリー・ソウル等）は静的ファイルでなく
lwrap＋Link理論で動的にする。部屋ディレクトリに隠し
ファイル `.lwrap.lnk`（Link宣言）と隠し実行ファイル
`.lwrap.elf` を置き、lwrap に `--view`（`-v`）モードを
付けて現在の合成を見られるようにする。

実行バイナリのサイズ（全数）:
- lwrap（--view 追加後）: 14,384 バイト
  （元の lwrap は 13,968 バイト、バックアップ
  `experiments/lwrap/lwrap.orig` に保存）
- 部屋 ELF `.lwrap.elf`: 全6室とも 14,008 バイト
  （rouka / play-lab / proof-lab / house / library /
  society。生成 C は gen/<room>_room.c）
- `.lwrap.lnk`: rouka 4,571 / play-lab 1,773 /
  proof-lab 2,037 / house 2,088 / library 1,989 /
  society 1,478 バイト

## 形式と生成の決定

1. LNK 形式（LNK_FORMAT.md）: 行指向テキスト＋バイト数
   つきペイロード。魔数 `LWRAP-LNK 1`、room/title/slot/
   role、bind 行（host => inside ro|rw）、soul（SOUL.md
   のバイト列）、memory レコード列（record: 日付 バイト数）。
   レコード連結は MEMORY.md とバイト完全一致（種まき時の
   分割は行頭 `- ` 境界、序文は日付 `head`）。Python
   （lnk.py）と C（lwrap --view）の両方が同じ形式を読む。
2. bind: 全室共通で 自室 => /data rw、冷蔵庫 items =>
   /fridge ro、fridge.md => /fridge.md ro。ろうか のみ
   全室 => /rooms/<name> ro を追加。
3. --view は生成フロー側で実装: sf_builder.py の
   C_HEADER に読み手（step_view）を追加し、cmain の
   argv 判定で `--view`/`-v` を先に拾う。名前空間操作は
   一切なし。生成し直した lwrap_gen.c が正本で、手パッチ
   はしていない。
4. 部屋 ELF は gen_room_elf.py が各室の .lwrap.lnk を
   読んで bind を C に焼き込み、lwrap_gen.c と同じ作法
   （親が map を書く userns 対、mount ns、pid ns は
   run_exp.py の lwrap 腕に合わせて常時 on）で生成。
   bind のホストパスが開けなければ、名前空間に触る前に
   名前つきで拒否（exit 66）。

## 検証（全項目）

- a. 較正（check_view.py）: 全6室で `lwrap --view` が
  描く soul / memory が SOUL.md / MEMORY.md と一致。
  末尾空白の正規化すら不要なバイト完全一致だった
  （soul: 1375/744/863/736/730/614、memory: 2257/575/
  701/964/867/472 バイト、室順は rouka/play-lab/
  proof-lab/house/library/society）。PASS
- b. 動的化（append_record.py、proof-lab）: テスト
  レコード1件（2026-11-10、99バイト）を追加すると
  view は records 4→5、memory 701→800 バイト、
  record-dates 末尾に 2026-11-10 が増え、ペイロードは
  その1行だけが増えた（diff で確認）。SOUL.md /
  MEMORY.md の md5 は不変。検証後に .lnk は元の
  バイト列へ復元し、較正の再実行も PASS。PASS
- c. 隔離（check_isolation.py、全6室の .lwrap.elf）:
  /data/SOUL.md 読み ✓、/fridge.md 読み ✓、/fridge
  items 一覧 ✓、/data への書き込みはホスト側に見えて
  後始末済み ✓、/fridge への書き込み拒否（EROFS）✓、
  /fridge.md への追記拒否 ✓、他室 MEMORY.md は到達
  不能 ✓（ろうかだけは設計通り /rooms/house/MEMORY.md
  が読める ✓）。全室 PASS
- 負コン（名前つき拒否）: 存在しない bind 先で生成した
  試験 ELF は "roomelf testroom: bind /data refused:
  host path ... missing" で exit 66。`lwrap --view`
  は .lnk の無い dir で "no .lwrap.lnk in room dir"
  exit 65、壊れた .lnk で "invalid: bad magic" exit 65。
- 回帰: 拡張した lwrap で exp/run_exp.py を全実行。
  判定は P1（全腕の出力一致）/ P3（サイズ ≤ 20,000）/
  P4（中央値が bwrap の 0.8–1.5 倍域内）/ P5（隔離
  プローブ 3/3）すべて true。room_test の出力は新旧
  とも "800\nROOM PASS" で一致。退行なし。

## 当たったバグと直し

1. ro 再マウントの EPERM（本構築の一番の山）: bind を
   read-only に remount する syscall が errno=1 で落ちた。
   切り分けの末、原因は「現在の per-mount フラグを
   再指定しない remount は、このセルでは落ちる」こと
   だった。コピーされたマウント木がフラグ（noatime 等）を
   ロックして持ち運んでおり、外す指定は変更とみなされて
   EPERM になる。util-linux mount が通るのは
   /proc/self/mountinfo から現在のフラグを読み戻して
   全て再指定するから（strace で確認）。直し: 部屋 ELF
   も mountinfo を読んで現在フラグ＋MS_RDONLY で
   remount する。読みは pid ns を分ける前に行う
   （分けた後では /proc/self が引けない）。bind と
   remount を pid fork の前へ移したのもこのため。
2. 較正スクリプトの添字バグ1件（"\nmemory:" の先頭 LF
   を数え忘れ）。測定器側の誤りとして直した。

## 正直な境界

- --view は「読む」だけ。部屋へ入る（実行する）のは
  .lwrap.elf の仕事で、view は合成の表示に徹する。
  view の出力は検証用の書式であり、人間向けの整形は
  最小限（レコードの日付一覧つき）。
- アイデンティティの「実体」は .lnk へ移ったが、
  SOUL.md / MEMORY.md は種として残っており、今の
  ところ両方が正本候補として併存する。どちらを正に
  するかは未決定（今回の種まきは SOUL/MEMORY を読み
  取り専用の種として扱い、一切変更していない）。
- bind の解決は ELF 生成時に焼き込む。室の .lnk の
  bind を変えたら gen_room_elf.py の再実行が要る
  （view は .lnk を毎回読むので即座に追随する）。
- /rooms/<他室> の空のマウント点ディレクトリは、
  他室からも名前だけは見える（中身は到達不能で、
  MEMORY.md は読めない）。骨格が見えること自体を
  漏れとみなすかは設計判断として残す。
- spec の自動生成（特徴量→極）とは未接続。今回の
  .lnk は LLM の手書き spec と同じく「宣言され、
  検証を通った」側の成果物。

## 成果物

- experiments/lwrap-rooms/: LNK_FORMAT.md、lnk.py、
  seed_lnk.py、gen_room_elf.py、append_record.py、
  check_view.py、check_isolation.py、gen/*.c
- experiments/lwrap/: sf_builder.py（--view 追加）、
  lwrap_gen.c（再生成）、lwrap 14,384 バイト、
  lwrap.orig（元の 13,968 バイト）
- 各室: .lwrap.lnk、.lwrap.elf
- rootfs にマウント点を追加: /data、/fridge、
  /fridge.md（空ファイル）、/rooms/<全室>
