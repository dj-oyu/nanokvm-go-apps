# nanokvm-go-apps

[NanoKVM Go](https://wiki.sipeed.com/hardware/en/kvm/NanoKVM_Go/introduction.html) 用の App カタログです。
NanoKVM Go の **Settings > Apps > Store** に、このリポジトリを App の取得元（App server）として追加すると、ストアからインストールできます。
形式は公式カタログ [sipeed/NanoKVM-Go-Apps](https://github.com/sipeed/NanoKVM-Go-Apps) の `main` ブランチと同じで、既定ブランチのトップレベルにある各ディレクトリが 1 つの App です。

## Apps

| App | 説明 |
| --- | --- |
| [`vin-preview`](vin-preview/) | HDMI 入力を本体の LCD に低遅延で表示する。VIN のフレームをメモリから直接読むので、Web UI の映像配信とは独立に動き、kvm_vin に負荷をかけない。ダブルタップで拡大・縮小、拡大中はスワイプで表示位置を移動できる。スマホのミラーリングなど、中央に映った画像の範囲を自動で検出して、その範囲を基準に表示する。 |

## インストール

1. NanoKVM Go の Web UI で **Settings > Apps > Store** を開き、App の取得元に次の URL を追加する。

   ```
   https://github.com/dj-oyu/nanokvm-go-apps
   ```

2. 追加した取得元のカタログから App を選んでインストールする。
3. 本体の LCD で **Apps** を開き、App をタップして起動する。終了は、画面の左右の端から内側へスワイプして YES。

NanoKVM Go は、取得元のリポジトリの既定ブランチを `https://codeload.github.com/<owner>/<repo>/zip/HEAD` からダウンロードし、トップレベルのディレクトリをカタログとして読み込みます。

## 動作確認した環境

- NanoKVM Go（AX620Q、Debian 13 armv7l、kernel 4.19.125）
- kvm_vin: 2026-09-04 ビルド（git `aeac1d31`）、ax_sys V3.0.0

`vin-preview` は、kvm_vin の非公開の内部構造（AX 共通プールの配置と、各ブロックのメタデータ）に依存しています。ファームウェアの更新で動かなくなる可能性があります。

## 調査メモ

VIN（HDMI 入力）をユーザー App から扱う方法の調査結果は [docs/vin-research.md](docs/vin-research.md) にあります。
kvm_vin の Unix ソケット（JPEG スナップショット / H.264 / 制御）、MCP サーバー、共通プールからの生フレームの読み取り、LCD（fbtft）に描くときの注意点などをまとめています。

## App を追加するときの規則

- ディレクトリ名は小文字のケバブケース（`_` で始まるディレクトリは対象外）。
- 各ディレクトリに `app.json` と `main.py` を置く。
- `app.json` の `app_id` は 3 要素以上の逆ドメイン形式で、最後の要素はディレクトリ名の `-` を `_` にしたもの（例: `vin-preview` → `io.github.dj_oyu.vin_preview`）。
- `app_id` はカタログ内で重複させない。

SDK（`appbase`）と開発ガイドは [sipeed/NanoKVM-Go-Apps の `base` ブランチ](https://github.com/sipeed/NanoKVM-Go-Apps/tree/base)を参照してください。
