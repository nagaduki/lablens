# 1. プロンプト一覧の読み込み確認
python ~/lablens/lablens_core.py --list-formats

# 2. コア単体での実行テスト（標準出力にJSONLを表示）
python ~/lablens/lablens_core.py \
  --image ~/storage/downloads/test_lab.jpg \
  --format 01_general_lab \
  --mode two-step
