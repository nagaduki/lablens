cat << 'EOF' > ~/lablens/run_interactive.sh
#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

IMAGE_PATH="${1:-}"
BASE_DIR="$HOME/lablens"
CORE_SCRIPT="$BASE_DIR/lablens_core.py"
VIEWER_SCRIPT="$BASE_DIR/viewer_tui.py"
OUT_DIR="$BASE_DIR/output"

if [[ -z "$IMAGE_PATH" \vert{}\vert{} ! -f "$IMAGE_PATH" ]]; then
  echo "エラー: 有効な画像パスが指定されていません: $IMAGE_PATH" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# 1. 利用可能なフォーマット一覧をコアから取得
FORMATS_JSON="$(python "$CORE_SCRIPT" --list-formats)"
COUNT="$(echo "$FORMATS_JSON" | jq 'length')"
if [[ "$COUNT" -eq 0 ]]; then
  echo "エラー: $BASE_DIR/prompts/ にフォーマット定義JSONがありません。" >&2
  exit 1
fi

# 2. termux-dialog radio でフォーマットを選択
LABELS_CSV="$(echo "$FORMATS_JSON" | jq -r '[.[].label | gsub(","; "・")] | join(",")')"
RADIO_RES="$(termux-dialog radio -t "検査データフォーマットを選択" -v "$LABELS_CSV")"
SEL_INDEX="$(echo "$RADIO_RES" | jq -r '.index // -1')"

if [[ "$SEL_INDEX" -lt 0 ]]; then
  echo "キャンセルされました。"
  exit 0
fi

FORMAT_ID="$(echo "$FORMATS_JSON" \vert{} jq -r ".[$SEL_INDEX].id")"
FORMAT_LABEL="$(echo "$FORMATS_JSON" \vert{} jq -r ".[$SEL_INDEX].label")"

# 3. 実行モード選択
MODE_RES="$(termux-dialog radio -t "実行モードを選択" -v "2段階処理（OCR→整形：高精度）,1パス処理（無料枠RPM節約）")"
MODE_IDX="$(echo "$MODE_RES" | jq -r '.index // -1')"
if [[ "$MODE_IDX" -lt 0 ]]; then
  echo "キャンセルされました。"
  exit 0
fi
MODE="two-step"
[[ "$MODE_IDX" -eq 1 ]] && MODE="single"

# 4. 出力ファイルパスの決定と実行
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_JSONL="$OUT_DIR/${STAMP}_${FORMAT_ID}.jsonl"
OUT_OCR="$OUT_DIR/${STAMP}_${FORMAT_ID}.ocr.txt"

echo "========================================"
echo "フォーマット : $FORMAT_LABEL ($FORMAT_ID)"
echo "モード       : $MODE"
echo "入力画像     : $IMAGE_PATH"
echo "========================================"

python "$CORE_SCRIPT" \
  --image "$IMAGE_PATH" \
  --format "$FORMAT_ID" \
  --mode "$MODE" \
  --output "$OUT_JSONL" \
  --save-ocr "$OUT_OCR"

if [[ ! -s "$OUT_JSONL" ]]; then
  echo "警告: 抽出された検査項目が 0 件でした。OCRログを確認してください: $OUT_OCR"
