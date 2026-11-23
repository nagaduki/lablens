cat << 'EOF' > ~/.shortcuts/lablens_camera.sh
#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

TMP_DIR="$HOME/lablens/output/tmp"
mkdir -p "$TMP_DIR"
PHOTO_PATH="$TMP_DIR/capture_$(date +%Y%m%d_%H%M%S).jpg"

while true; do
  echo "背面カメラ(ID:0)で撮影します..."
  termux-camera-photo -c 0 "$PHOTO_PATH"

  # 撮影した画像をAndroid標準ビューアで開いて目視確認
  termux-open "$PHOTO_PATH"
  sleep 1

  # 確認ダイアログ（Termuxに戻った際に選択）
  ACT_RES="$(termux-dialog radio -t "撮影画像の確認" -v "この画像でOCRを実行,撮り直す,キャンセル")"
  ACT_IDX="$(echo "$ACT_RES" | jq -r '.index // 2')"

  if [[ "$ACT_IDX" -eq 0 ]]; then
    ~/lablens/run_interactive.sh "$PHOTO_PATH"
    break
  elif [[ "$ACT_IDX" -eq 1 ]]; then
    rm -f "$PHOTO_PATH"
    continue
  else
    rm -f "$PHOTO_PATH"
    echo "中止しました。"
    break
  fi
done

echo ""
read -r -p "Enterキーで終了します..."
EOF
chmod +x ~/.shortcuts/lablens_camera.sh
