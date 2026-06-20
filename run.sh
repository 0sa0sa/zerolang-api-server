#!/bin/sh
# Todo BE 全体を起動: Zero 読み取りバックエンド + 外部ライターサイドカー。
#
#   client ──> writer.py (:8080, 書き込みと永続化を担当)
#                 │  GET /health,/todos
#                 └─> zero run  (:3000+, std.fs.readBytes で data/store.json を読む)
#
# 注意: `zero` は必ずフルパスで起動すること — std.http.listen が argv[0] を
# 再exec するため、PATH 解決の素の `zero run` は
# "zero listen: No such file or directory" で失敗する。
set -eu   # -e: 最初のエラーで停止, -u: 未定義変数でエラー

# どこから実行してもよいようプロジェクトディレクトリを解決。
DIR="$(cd "$(dirname "$0")" && pwd)"
ZBIN="${ZERO_BIN:-$HOME/.zero/bin/zero}"      # zero バイナリのフルパス(上の注意参照)
STORE="${ZT_STORE:-$DIR/data/store.json}"     # 共有ストアファイル(Zeroが読み、writerが書く)
PORT="${ZT_PORT:-8080}"                       # writer.py が公開するポート
cd "$DIR"

# 何かが読む前にストアの存在を保証。
mkdir -p "$DIR/data"
[ -f "$STORE" ] || printf '[]' > "$STORE"

# Zero 読み取りバックエンドをバックグラウンド起動し、出力を捕捉する
# (パースに必要な "listening on http://127.0.0.1:PORT" 行を含む)。
ZLOG="$DIR/data/zero.log"
: > "$ZLOG"                                    # ログを空にする
"$ZBIN" run > "$ZLOG" 2>&1 &
ZPID=$!
# スクリプト終了時(Ctrl-C・エラー等)に Zero バックエンドを確実に止める。
trap 'kill $ZPID 2>/dev/null || true' EXIT INT TERM

# Zero リスナが実ポート(自動繰り上げの可能性あり)を表示するまで最大~10秒
# 待ち、host:port として writer に渡すために取得する。
BACKEND=""
i=0
while [ $i -lt 40 ]; do
  BACKEND=$(grep -oE '127\.0\.0\.1:[0-9]+' "$ZLOG" | head -1 || true)
  [ -n "$BACKEND" ] && break
  # 起動せずコンパイル/実行時エラーをログした場合は即失敗。
  if grep -qiE 'BLD|PAR|ERR|error' "$ZLOG"; then echo "zero backend failed:"; cat "$ZLOG"; exit 1; fi
  i=$((i+1)); sleep 0.25
done
[ -n "$BACKEND" ] || { echo "zero backend did not start:"; cat "$ZLOG"; exit 1; }
echo "zero read-backend: http://$BACKEND"

# ストアパスと検出したバックエンドアドレスを環境で渡し、writer サイドカーへ
# 処理を引き継ぐ(exec でこのシェルを置き換える)。
ZT_STORE="$STORE" ZERO_BACKEND="$BACKEND" ZT_PORT="$PORT" exec python3 "$DIR/writer.py"
