# NanoKVM-Go: Python ユーザーアプリから VIN (HDMI 入力) を扱う

調査日: 2026-09-25 / 対象: `nanokvm-go` (Tailscale, root)
ファーム: Debian 13 armv7l, kernel 4.19.125, Axera SoC, `kvm_vin` build 2026-09-04 (git `aeac1d31`)

> 公式 App SDK (`appbase.py`) には VIN の API はない。以下は `kvm_vin` / `libkvm.so` /
> `nano_ocr_app` のバイナリ解析と実機検証で得た **非公開の内部 API**。ファーム更新で変わる可能性あり。

## 構成

```
HDMI → LT7911 (lt7911_manage.ko, /proc/lt7911_info/*)
     → kvm_vin (/kvmcomm/vin/kvm_vin, kvmcomm.service が起動・監視)
         AX VIN → VENC(H.264/H.265) ─→ /run/kvm/vin_video.sock    → NanoKVM-Server (libkvm.so) → Web
                → JENC(JPEG)      ─→ /run/kvm/vin_snapshot.sock → nano_ocr_app / MCP など
         制御                     ←─ /run/kvm/vin_ctrl.sock      ← NanoKVM-Server (libkvm.so)
設定: /etc/kvm/kvm_vin.toml   ログ: /var/log/kvmcomm/vin.log
```

`/dev/video*` は存在しないため、V4L2 は使えない。ユーザーアプリは上記 Unix ソケットを使うか、次節の方法で VIN の生フレームを直接読む。

## 0. 生フレーム（YUYV422）を VIN の共通プールから直接読む（推奨・JPEG なし）

チップは AX620Q（`/proc/ax_proc/chip_type`、MSP V3.0.0）。構造体の定義は
[sipeed/maix_ax620e_sdk_msp](https://github.com/sipeed/maix_ax620e_sdk_msp) の `out/arm_glibc/include` にあるヘッダを使った。

- VIN pipe0/chn0 は **YUYV422 2560x1440**（7,372,800 B）を共通プール（`/proc/ax_proc/pool`、PoolId 0、16.5MB × 5 ブロック、使用中は 3 ブロック）へ出力する。
  カーネルのリンク表（`/proc/ax_proc/link_table`）では VIN(0,0) → VENC ch0（H.264）と JENC ch1（JPEG）につながっている。IVPS は未使用。
- プールの物理配置: `PhysAddr` から 4KB のメタ領域が BlkCnt 個並び、その後ろに BlkSize ずつデータブロックが並ぶ。
- **各ブロックのメタ領域には `AX_VIDEO_FRAME_T` が入っていて、リアルタイムで更新される**。`fmt=0xD`（YUYV）かつ `pts≠0` なら書き込み済みのフレーム、`fmt=0x84` なら書き込み予定・書き込み中。
  `/proc/ax_proc/vin/statistics` の `OutBlkId` / `OutSeqNum` とも一致する。
- 別プロセスから `libax_sys.so` の `AX_SYS_Init` → `AX_SYS_Mmap`（メタ）/ `AX_SYS_MmapCache`（データ）→ `AX_SYS_MinvalidateCache` → 読み取り、で取れる。
  **VIN・リンク・プール参照カウントの API は一切呼ばない**ので、kvm_vin は影響を受けない（検証中に PID は変わらなかった）。
- 別プロセスから `AX_VIN_*` を呼ぶと `0x80110180`（未初期化）で失敗する。VIN の状態はプロセスごとに持つため、`AX_VIN_GetYuvFrame` で横取りはできない。
  VIN の出力チャネルは MAIN の 1 つだけなので、「VIN から別解像度の raw ストリームを出す」こともできない（やるなら IVPS へのリンク追加が必要で、kvm_vin のパイプラインを変えることになる）。
- **注意（上書き）**: ブロックは参照カウントで保護されていない。VIN はフレームの完成から約 1 フレーム（60Hz で約 17ms）後にそのブロックへの上書きを始め、そのときメタの seq が変わる。
  そこで「次に完成するフレームを待ち、すぐ 8 行おきにコピー（約 5ms）し、コピー後に seq が変わっていないか確認する」。これで破損率は約 6.5%（CPU を取れず遅れた場合）になった。破損したフレームは捨てて読み直す。
  フル解像度のコピーは約 47ms かかり、ほぼ確実に破損する。
- 読み取り中も kvm_vin の CPU 使用率は 1% のまま（平常時と同じ）。

#### コピーを最小にしたパイプライン（`RawScaler`）

1 フレームあたりのコピーは「プール → 入力バッファ（間引きを兼ねる、行ごとに 1 回）」「swscale」「回転しながら fb の back buffer へ（列ごとに 1 回）」「flush」だけ。

- 行の間引き: 必要な行だけを読む。横の間引き: YUYV のマクロピクセル（2 画素 = 32bit）単位のストライド付き memoryview 代入で、同じコピーの中で行う。
- **SwsContext と入出力バッファを使い回す**。PyAV の `VideoFrame.reformat()` はフレームごとに SwsContext を作り直すので、毎回新しいフレームを作ると swscale が約 4 倍遅くなる（17.8ms → 4.4ms）。
- **PyAV は import しない。libswscale を ctypes で直接呼ぶ**（`_Swscale`）。理由は下の「起動が遅い」を参照。
- swscale の出力プレーン（行末パディングあり）から、`bytes()` や `join` を通さず、行ストライドを指定して直接 blit する。
- 完成から 8ms 以内のフレームは待たずに使う（`AX_SYS_GetCurPTS` と meta の pts を比べる）。古ければ次のフレームを待つ。

計測（実機、画面なし、2560x1440 入力、ソースは 640x180 に間引き）:

| 版 | 出力 | コピー | swscale | blit | 合計（待ち時間を除く） | fps | 破損 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 旧（join / tobytes / 毎回新規フレーム） | 270x152 | 9.8 | 17.8 | 6.5 | 34.3 ms | 17–23 | 6–9% |
| 新 `RawScaler` | 270x152 | 5.6 | 4.4 | 4.7 | 14.9 ms | **57.7** | 1.4% |
| 新 `RawScaler` | 270x240 | 5.6 | 4.7 | 6.2 | 16.7 ms | 52.6 | 1.3% |
| 新、ソース 1280x360 | 270x152 | 18.2 | 13.9 | 4.4 | 36.7 ms | — | 100% |

- **表示領域を広げても、コストはほとんど増えない**（出力サイズに比例するのは blit だけ）。効くのはソースから読む量。
- ソースを細かくするとコピーが約 17ms の猶予を超え、すべてのフレームが破損する。高精細にしたいなら切り出し範囲を狭める（例: row_step=4 で画面の半分だけ読む）。

実装: `nanokvm_vin.RawFrameReader` / `RawScaler`。

#### LCD に出すときの落とし穴（実機で計測）

画面なしで約 58fps 出ていても、LCD では最初 18–20fps しか出なかった。原因は 3 つ。

1. **カーネルに高分解能タイマーがない（HZ=100）。`time.sleep()` は `sleep(0)` も含めて約 10ms 眠る**（`os.sched_yield()` は 0.003ms）。
   フレーム待ちは「次のフレームまで 12ms 以上あるときだけ sleep、それ以外は `sched_yield`」にし、`ctx.run(fps=0)` で appbase の速度調整もやめた。
2. **appbase の `draw_text` は 1 画素ずつ描くので、1 行約 3.7ms かかる**。ステータス表示は変化したときだけ描き直し、FPS の数値は 0.5 秒ごとに更新する。毎フレームの `fb.clear` もやめた。
3. **LCD は fbtft（`fb_st7789p3`、SPI 80MHz）で、書き込みは deferred I/O（遅延転送）**。1 画面（136KB）の転送に約 22ms かかり、その間に mmap 経由で書くとページロックで待たされる（appbase の `flush()` で中央値 19.5ms、最大 93ms）。
   **`os.pwrite(fb._fd, fb._buf, 0)` で書けばブロックしない**（0.15ms）。パネルの更新レートの上限は約 45fps（SPI 転送量 6.2MB/s）。

改善後の実機の結果（270x152、RAW）: App のループは 36–60fps、**パネルの実際の更新は 44.7fps（上限に張り付き）**、kvm_vin の CPU 使用率は 1%。
App は 1 コアを使い切る（約 104%）。大半は処理そのもの（1 フレーム約 15ms / 周期 16.7ms）で、空回りによる分は小さい。

**パネルの上限（45fps）を超えて毎フレーム書くのは、レイテンシのため**。fbtft の遅延転送は、ワーカーが起きた瞬間のバッファの中身を送るので、書く頻度が高いほど送られる絵が新しい。
画面なしのシミュレーションで、各書き込みの時刻とフレームの pts を記録し、パネルがランダムな時刻に読み出したときの絵の古さ（キャプチャからの経過時間）を比べた。

| 方式 | 書き込み | パネルが読み出す絵の古さ 中央値 / p90 |
| --- | --- | --- |
| 毎フレーム（60Hz） | 58–60 回/秒 | 24 / 32 ms |
| 1 フレームおき（30Hz） | 30 回/秒 | 32 / 45 ms |

これに SPI 転送（画面の上から下まで約 22ms）が加わる。CPU を節約したい場合は 1 フレームおきにして間を sleep で眠れば CPU 使用率は約半分になる見込み（未計測）だが、その分だけ絵が古くなる。

## 1. vin_snapshot.sock — JPEG スナップショット

- リクエスト: JSON 1 行（**改行必須**、1 接続につき 1 リクエスト）
- レスポンス: JSON 1 行 + `size` バイトの JPEG

```jsonc
// → 送信
{"version":1,"cmd":"capture_fresh","quality":30,"timeout_ms":1000}
// ← 受信 (続けて JPEG 本体 169818 バイト)
{"capture_id":"18d8…-0:1","captured_at":"2026-09-24T17:45:32.285938432Z","height":1440,
 "message":"ok","ok":true,"ret_code":0,"session_id":"18d8…-0","size":169818,
 "source":"mcp","version":1,"width":2560}
```

| cmd | パラメータ | 備考 |
| --- | --- | --- |
| `capture_fresh` | `quality` 1..99, `timeout_ms` 1..30000 (既定 1000), `x` `y` `w` `h`（切り出し） | 縮小オプションはない。切り出しはハードウェア (IVPS) で行う |
| `get_latest` | `wait_ms` (long-poll), `session_id`, `after_sequence` | periodic snapshot が有効な時のみ新フレームあり。既定設定では常に `not_modified` |

エラー例: `missing or invalid version` (-4), `unsupported cmd` (-2), `snapshot timeout` (-13, HDMI 無信号時),
`IVPS config failed` (-15, 切り出し範囲が不正)。

切り出しの実測（入力 2560x1440）。`{"x":0,"y":0,"w":640,"h":360}` のように**トップレベルのキー**で渡す（`crop:{…}` の形は無視される）。

- 成功した例: (0,0,1280,720), (640,360,640,360), (1280,720,640,360), (2,2,64,64), (0,0,34,34)
- 失敗した例: 奇数の値 (1,1,64,64)、小さすぎる値 (0,0,63,63)・(0,0,32,8)、右端・下端に接する範囲 (1920,1080,640,360)・(1280,720,1280,720)、
  大きめの範囲 (100,100,2016,1008)
- 正確な条件は未確定。偶数座標で、中央寄りの 1280x720 以下の範囲なら安定して成功した。

**Web UI の配信と共存できる**ので、ユーザーアプリにはこちらを推奨。

## 2. vin_video.sock — H.264/H.265 ストリーム

接続すると即座にフレームが流れ始める（最初は SPS/PPS 付き IDR）。フレームごとに 64 バイトのヘッダ + Annex-B ペイロード。

| offset | 型 | 内容 | 実測値 |
| ---: | --- | --- | --- |
| 0 | char[4] | magic | `KVVF` |
| 4 | u16 | version | 1 |
| 6 | u16 | header size | 64 |
| 8 | u32 | payload size | |
| 12 | u32 | width | 2560 |
| 16 | u32 | height | 1440 |
| 20 | char[4] | codec | `H264` (`[device] mode` で H265) |
| 24 | u32 | flags (bit0 = keyframe) | GOP 50 で 50 枚に 1 回 |
| 28 | u64 | pts (µs) | 16667 刻み |
| 36 | u64 | sequence | +1 ずつ |
| 44 | u32 | fps | 59 |
| 48 | 16B | reserved | 0 |

**注意: 同時接続は 1 クライアントのみで、後から接続した側が勝つ。** アプリが接続すると
NanoKVM-Server（Web で映像を見ている時だけ接続している）の配信が切れる。逆に Web を開くとアプリ側が EOF になる。

## 3. vin_ctrl.sock — エンコーダ制御

JSON オブジェクトを送信すると、JSON で応答する（改行は不要）。

```jsonc
{"cmd":"request_idr","instant":true}          → {"message":"request_idr ok","ok":true,"ret_code":0}
{"cmd":"set_fps","src_fps":60.0,"dst_fps":30.0}
{"cmd":"set_gop","gop":50}
{"cmd":"set_bitrate","bitrate":8000}
{"cmd":"set_rc_mode","mode":0}
```

Web UI と共有のエンコーダ設定なので、`request_idr` 以外は Web の画質に影響する。

## 3.5 MCP サーバー (`https://nanokvm-go/api/mcp`)

NanoKVM-Server が提供している（Streamable HTTP、`Authorization: Bearer <api key>`、`Mcp-Session-Id` あり）。
serverInfo は `nanokvm-remote-control v1.0.0`。resources / prompts は空。

| tool | VIN との関係 |
| --- | --- |
| `screenshot` (quality, timeoutMs, x, y, w, h) | **中身は vin_snapshot.sock の `capture_fresh` そのもの**。session_id と capture_id の連番が共通で、`source:"mcp"` になる。HTTPS 経由で 300–650 ms かかる |
| `media_session_create` (videoReceive / speakerReceive / micSend, ttl ≤ 600s) → `wss://…/api/media/webrtc?media_session=<token>` | WebRTC で H.264 映像と Opus 音声を受信できる。Server が配信を分配するので **Web UI と共存できる**。ただしデバイス上の Python には aiortc などが入っていないので、外部クライアント向け |
| `speaker_read` | HDMI 音声を 1–30 秒録音し、Whisper で文字起こしして返す |
| `click_mouse` / `move_mouse` / `scroll_mouse` / `press_keys` / `type_text` | HID 入力（VIN とは無関係） |

- デバイス内で動くアプリなら、MCP を経由せずソケットを直接使うほうが速い。API キーも不要。
- 外部 PC から映像を扱うなら、MCP の `screenshot` か WebRTC を使う。

## 4. デバイス上でのデコード（実測, 2560x1440 入力）

システムの Python 3.13 には **PyAV 17.1 (`import av`) が入っている**。numpy / PIL / cv2 は入っていない。

| 処理 | 時間 |
| --- | --- |
| `capture_fresh` quality=30 (約 110KB) | 55–60 ms（初回は 200–340 ms） |
| quality=既定 / 90 | 約 340 ms / 630 ms |
| MJPEG デコード `lowres=3`（1/8 → 320x180） | 約 20 ms |
| MJPEG デコード（等倍 2560x1440） | 約 60 ms |
| `reformat(240x135, rgb565le)` | 約 12 ms |
| **合計（スナップショット → LCD 用 RGB565）** | **約 90 ms ≒ 10 fps** |
| H.264 ソフトウェアデコード 2560x1440 | 約 5.8 fps（2 コア使用、CPU を占有） |

- RAM が 119MB（空きは約 40MB）しかない。1440p の H.264 デコードや MJPEG エンコードは、メモリ不足（`ENOMEM`）や swap による大幅な低速化が起きた。常用するなら JPEG + `lowres` を使う。
- HDMI の状態は `/proc/lt7911_info/{status,width,height,fps}` で読める（`stable` / `disappear`）。

## サンプル

- [`vin-preview/nanokvm_vin.py`](../vin-preview/nanokvm_vin.py) — 生フレームの読み取りと、上記 3 ソケットのクライアント（ctypes と標準ライブラリのみ。縮小・色変換は PyAV の wheel に同梱の libswscale を ctypes で呼ぶ。JPEG のデコードなどソケット経路の補助関数だけ PyAV を使う）
- [`vin-preview/main.py`](../vin-preview/main.py) — appbase App。HDMI 入力を LCD にプレビューする（RAW のみ、テキスト表示なし）。
  - **長辺・短辺はフレーム全体ではなく、中身が映っている範囲（画像領域）を基準にする**。スマホのミラーリングでは、縦長のときは 16:9 のフレームの中央に置かれ左右が黒帯になり、横長のときはフレームいっぱいで余白がない。
    画像領域は `RawFrameReader.content_rect()`（0.5 秒ごとに `ContentScanner` が呼ぶ）+ `ContentTracker` で検出する。
    左右対称を前提に、**左半分の列を二分探索**して左の黒帯の幅を求める（列 x に、8 行おきの画素で明るいものが 1 つでもあれば「明るい列」とする。
    列 1 本の判定は、行の長さをステップにしたスライスで 180 画素を取り出して `translate` / `find` にかけるだけ）。境界が ±8px 以内に絞れたら打ち切り、黒と分かっている側に丸める（中身を削らない）。
    右の黒帯は同じ幅とみなし、上下の余白は 0。約 8 回の判定で 1 回約 2ms（行ごとに線形に走査していたときは 7–9ms）。
    中央付近の列まで黒なら暗転とみなして更新しない。広がる方向（縦→横の回転など）には次の検出ですぐ追従する（最大 0.5 秒）。
    縮む方向のうち、幅か高さが 25% 以上狭まる大きな変化（横→縦の回転など）は、同じ結果が 2 回続けば反映する（約 1 秒）。
    それより小さな縮みは、端が暗くなっただけの可能性が高いので 2 秒安定してから反映する（端の暗い画面で範囲が揺れないように）。
    実測: 縦長のスマホで (944, 0, 672, 1440)（実際の境界は 948）。
  - 通常: 画像領域の長辺を画面いっぱいに表示する（fit。縦長のスマホなら高さいっぱいの 110x240、16:9 の画面なら 270x150）。
  - ダブルタップ: 画像領域の短辺が画面いっぱいになるまで拡大する（cover。縦長のスマホなら幅いっぱいで上下にはみ出す。664x590 を切り出して 270x240 に）。拡大するときは、タップした位置に近い範囲を表示する。もう一度ダブルタップで戻る。
  - 拡大中のスワイプ: はみ出している方向に 3 段階（縦長のスマホなら上・中央・下）で表示範囲を切り替える。
    切り出しはプールからのコピーの段階で行う（`RawFrameReader.read(crop=...)`、`RawScaler.set_view()`）。
  - HDMI の解像度の変化に追従する: VIN が使うブロックの切り替え（プールの全ブロックを map し、メタ領域で毎回判定）、連番の振り直し（最新の判定は pts で行う）、行のストライド（`pic_stride`）、取得の途中で解像度が変わった場合（`SourceChanged`）に対応。
    kvm_vin が再起動してプールが動いた場合は、フレームが 2 秒来なければ `refresh()` で map し直す。無信号のまま起動しても落ちない。
    ※ 実際に解像度を変えたり、kvm_vin を再起動したりしての確認はまだしていない。
  - 起動から最初のフレームまで約 1.6 秒（PyAV を使っていた 0.2.0 までは 12–25 秒。下の「起動が遅い」を参照）。
  - 画面の端から始めた横スワイプは、ホストの終了ジェスチャー（左右の端 40px）と重なるので、中央寄りから行う。
  - タップした位置は光る円で示す（0.4 秒）。
  - 画面なしの計測: fit 58fps、cover 48fps。同じフレームを 2 回処理しないよう、前回より新しい連番のフレームだけを使う。

#### 縮小・色変換・描画の詰め（実測）

- **swscale は FAST_BILINEAR にする**。既定の BILINEAR より約 1.5 倍速い（fit 4.24→3.09ms、cover 5.36→3.00ms）。NEON は検出されている（`av_get_cpu_flags` = NEON）。
  POINT（最近傍）も、同じサイズでの色変換だけでも速くならない（cover で 1:1 の色変換は 6.97ms）。YUYV→RGB565 には swscale の専用の高速経路がなく、倍率に関係なく汎用の処理を通るため。
  YUV→RGB は係数の掛け算が必要なので、ビットシフトだけにはできない（ビットシフトで済むのは RGB565 への詰め込みだけ）。
  参考: 色を捨てたグレースケール（Y を `bytes.translate` の表引き 2 回で RGB565 に）でも 2.9–4.7ms で、FAST_BILINEAR のカラー変換より速くない。
- **「縮小せず、ソースからのサンプル位置の調整だけで最近傍に縮小する」案は効果が小さい**。色変換が結局 swscale の汎用処理を通り、最近傍のサンプリングは 1 行あたりのスライスが 2 回になってコピーが重くなるため（cover で合計 14.9ms、今の方式は約 12.6ms）。
- **回転（試して、やめた）**: libavfilter の `transpose` で LCD の物理的な並びに回転してから書くと、描画は物理行ごとの連続コピー（cover では 1 回のコピー）になり、
  縮小・色変換・回転・書き込みの合計は fit 9.06→4.88ms、cover 12.32→6.11ms まで下がった（出力は従来と 1 画素も違わない）。
  ただし libavfilter は PyAV 経由でしか使えず、PyAV の読み込みが起動を 10 秒以上遅らせるので、Python の列ごとの blit に戻した（cover で 48fps。パネルの上限 45fps は超えている）。
  - Python のループ展開（4 列ずつ）は効かない（4.33→4.22ms）。重いのはループの制御ではなく、1 反復ごとのスライスオブジェクトの生成なので、ループそのものを C に移すほうが効く。
  - `pad` は RGB565 に対応しておらず rgb24 に変換されてしまう。ソース側（YUYV）で黒帯を足すとかえって遅い（6.01ms）。

#### 起動が遅い → PyAV をやめて libswscale を ctypes で呼ぶ

ストアからインストールした直後の初回起動が非常に遅かった。計測すると、`import av` だけで 10–22 秒かかっていた。

- 端末の PyAV は pip の wheel で、FFmpeg 一式を同梱している。`import av` で **83 個の共有ライブラリ（ディスク上 58MB）** を map する（libavcodec 12MB、SVT-AV1 2.8MB、gnutls、libvpx など）。
- RAM は 119MB で、swap（59MB）もほぼ満杯（tailscaled、NanoKVM-Server、kvm_ui がそれぞれ 10–17MB を swap に出している）。そこに 58MB のライブラリを読み込むのでスラッシングが起き、ページキャッシュが温まっていても 10 秒以上かかる。キャッシュを捨てた状態では 22 秒だった。
- 使っているのは swscale だけ。wheel に同梱の `libswscale-*.so`（797KB）と `libavutil-*.so`（664KB）は libc/libm/libpthread にしか依存しないので、ctypes でこれだけを読み込み、`sws_getContext` / `sws_scale` を直接呼ぶ。
  システムの `libavutil.so.59` は X11 や VA-API、OpenCL などに依存しているので使わない。
- 結果: 起動から最初のフレームまで約 1.6 秒。プロセスの RSS は 11.7MB（`import av` するだけで約 16MB 使っていた）。出力は PyAV の `reformat`（FAST_BILINEAR）と 64800 画素すべて一致した。

`blit_rgb565()` は appbase にビットマップ描画 API がないため、`FrameBuffer._buf`（非公開属性）へ直接書き込む。

**実機の画面寸法は appbase README と異なる**: `/dev/fb0` は物理 240x284（stride 480）で、
host が渡す rotate=90 では論理 **284x240（横長）** になる。LVGL UI から判断して、表示されないのは論理座標の左 14 列と思われる。
そのため、プレビューは x=14 から 270x152（16:9）で描画している。

実機での動作（2026-09-25）: JPEG モード約 8–10 fps、RAW モード約 10 fps。python3 の RSS は約 13–18MB。
ただし、この計測中は後述の kvm_vin の空回り（1 コアを占有）が起きていた。kvm_vin 単体で見ると、JPEG スナップショットを 16 枚/秒取り続けても kvm_vin の CPU 使用率は約 5%。

### 既知の問題: kvm_vin が空回りすることがある

調査の途中で、kvm_vin の 1 スレッドがユーザー空間で空回りし、1 コアを使い切る状態になった（システムコールは呼んでおらず、状態は常に R）。
App を止めても変わらず、`kill -TERM $(pidof kvm_vin)`（kvmcomm.sh が約 7 秒で起動し直す）で直った。
再起動後は、RAW 読み取りを続けても JPEG スナップショットを連続で取っても再発しなかった。
引き金は特定できていない。疑わしいのは、この調査で行った `vin_video.sock` への強制接続・切断や、切り出しに失敗するリクエスト（`IVPS config failed` / `jenc_set_crop failed`）。
負荷は `top` か `/proc/$(pidof kvm_vin)/task/*/stat` で確認できる（平常時は約 1%）。
`captured_at` は UTC 表記。

配置: 正規の方法は、このリポジトリを NanoKVM Go の App の取得元として登録し、ストアからインストールすること（手順はリポジトリ直下の README）。
開発中は、直接コピーしても 10 秒以内に Apps 一覧に出る:

```bash
scp -r vin-preview nanokvm-go:/kvmcomm/apps/
```

簡易利用例:

```python
import nanokvm_vin as vin
hdr, jpeg = vin.capture_fresh(quality=50)
open("screen.jpg", "wb").write(jpeg)

for f in vin.video_frames():          # ※ Web 配信を奪う
    print(f.seq, f.keyframe, len(f.payload))
```
