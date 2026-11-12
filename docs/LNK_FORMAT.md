# LNK 形式（.lwrap.lnk）— 部屋の Link 宣言

部屋ディレクトリに置く隠しファイル `.lwrap.lnk` の形式。
目的は「Python の道具と、小さい C の読み手（lwrap --view）の
両方がライブラリなしで読める」ことで、行指向テキスト＋
バイト数つきペイロードで構成する。

## 構造

```
LWRAP-LNK 1
room: <dir name>
title: <表示名>
slot: <0-5 の整数>
role: <ROOMS.md の役割>
bind: <ホスト絶対パス> => <内側パス> <ro|rw>
...（bind は複数行）
soul: <バイト数>
<SOUL.md のバイト列そのまま>
<LF 1個（区切り、バイト数には数えない）>
memory: <レコード数>
record: <日付> <バイト数>
<レコードのバイト列>
<LF 1個（区切り）>
...（record の繰り返し）
end
```

- 1行目は固定の魔数 `LWRAP-LNK 1`。
- ヘッダ行は `key: value` 形式。C の読み手は行頭のキーで判別する。
- ペイロード（soul と各 record）はバイト数で区切るので、
  本文にどんな行が含まれていても壊れない。ペイロードの直後には
  区切りの LF が1個だけ入り、これはバイト数に数えない。
- memory のレコード連結（record のペイロードを順に繋いだもの）が
  MEMORY.md のバイト列と一致する（種まき時の分割は行頭の
  `- ` を境界にし、序文は日付 `head` のレコードに入れる）。
  つまり .lnk から MEMORY.md は無損失に復元できる。
- record の日付は `YYYY-MM-DD`、序文は `head`、日付のない
  箇条書きは `undated`。

## 読み手の契約

- `lwrap --view DIR` は DIR/.lwrap.lnk を読み、合成した
  view（room/slot/role/binds/soul/memory とレコード数）を
  標準出力に書く。名前空間操作は一切しない。
- ファイルが無い・魔数が違う・バイト数が合わない場合は
  名前つきで拒否し、非ゼロで終了する。
- append_record.py は record を末尾（`end` の直前）に足し、
  `memory:` のレコード数を書き換える。SOUL.md / MEMORY.md
  には触らない。
