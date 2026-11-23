#!/usr/bin/env python3
"""検査結果画像のOCR・整形・JSONL出力を行う独立コアモジュール。

CLIとして単体実行できるほか、MCPサーバーや他スクリプトからimportして利用できます。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from google.genai.errors import APIError

DEFAULT_BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPTS_DIR = DEFAULT_BASE_DIR / "prompts"
DEFAULT_ENV_FILE = Path.home() / ".lab_ocr.env"
DEFAULT_MODEL = "gemini-3.8-flash"


def load_env_file(env_path: Path = DEFAULT_ENV_FILE) -> None:
    """~/.lab_ocr.env から未設定の環境変数を読み込む（Termux:Widget対策）。"""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def list_formats(prompts_dir: Path = DEFAULT_PROMPTS_DIR) -> list[dict[str, Any]]:
    """promptsディレクトリ内のフォーマット定義一覧を返す。"""
    formats: list[dict[str, Any]] = []
    if not prompts_dir.exists():
        return formats
    for path in sorted(prompts_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        formats.append({
            "id": path.stem,
            "label": data.get("label", path.stem),
            "path": path,
            "config": data,
        })
    return formats


def load_format_config(format_id: str, prompts_dir: Path = DEFAULT_PROMPTS_DIR) -> dict[str, Any]:
    """指定されたIDのプロンプト定義JSONを読み込む。"""
    target = prompts_dir / f"{format_id}.json"
    if not target.exists():
        available = [f["id"] for f in list_formats(prompts_dir)]
        raise FileNotFoundError(
            f"フォーマット '{format_id}' が見つかりません。利用可能: {', '.join(available)}"
        )
    return json.loads(target.read_text(encoding="utf-8"))


def _call_with_retry(fn, max_retries: int = 4, base_wait: float = 6.0):
    """無料版APIキーのレート制限(429)に対する指数バックオフリトライ。"""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except APIError as e:
            if e.code == 429 and attempt < max_retries:
                wait = base_wait * (2 ** attempt)
                print(
                    f"[429 Rate Limit] {wait:.0f}秒待機後にリトライします ({attempt + 1}/{max_retries})...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                raise


def _load_image_part(image_path: Path) -> types.Part:
    """Pillow不要で画像ファイルをバイト列Partとして読み込む。"""
    if not image_path.exists():
        raise FileNotFoundError(f"画像ファイルが見つかりません: {image_path}")
    mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"
    return types.Part.from_bytes(data=image_path.read_bytes(), mime_type=mime_type)


def process_image_to_jsonl(
    image_path: str | Path,
    format_id: str,
    *,
    mode: str = "two-step",
    model: str | None = None,
    prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    step_interval: float = 1.5,
) -> dict[str, Any]:
    """画像をOCRし、指定フォーマットで整形したJSONL文字列と詳細を返す。

    Returns:
        {
            "format_id": str,
            "model": str,
            "ocr_text": str,
            "records": list[dict],
            "jsonl": str,
        }
    """
    load_env_file()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が設定されていません（~/.lab_ocr.env を確認してください）")

    model_name = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    fmt = load_format_config(format_id, prompts_dir)
    image_part = _load_image_part(Path(image_path))
    client = genai.Client(api_key=api_key)

    ocr_prompt = fmt["ocr_prompt"]
    format_prompt = fmt["format_prompt"]
    response_schema = fmt["response_schema"]

    if mode == "two-step":
        # 【第1段階】画像 → プレーンOCRテキスト（行・列の対応を維持）
        ocr_resp = _call_with_retry(
            lambda: client.models.generate_content(
                model=model_name,
                contents=[image_part, ocr_prompt],
                config=types.GenerateContentConfig(temperature=0.0),
            )
        )
        ocr_text = (ocr_resp.text or "").strip()

        # 無料枠のバースト回避のため短いインターバルを挟む
        if step_interval > 0:
            time.sleep(step_interval)

        # 【第2段階】OCRテキスト → 選択プロンプトによる構造化JSON
        fmt_resp = _call_with_retry(
            lambda: client.models.generate_content(
                model=model_name,
                contents=f"{format_prompt}\n\n--- OCRテキスト ---\n{ocr_text}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema,
                    temperature=0.0,
                ),
            )
        )
        parsed = json.loads(fmt_resp.text)
        records = parsed.get("records", [])

    elif mode == "single":
        # 【省クォータ1パス】1回のAPI呼び出しでOCRテキストと構造化を同時取得
        combined_schema = {
            "type": "OBJECT",
            "properties": {
                "ocr_text": {"type": "STRING", "description": "画像から読み取ったOCR全文"},
                "records": response_schema["properties"]["records"],
            },
            "required": ["ocr_text", "records"],
        }
        combined_prompt = (
            f"以下の手順を1回の出力で実行してください。\n"
            f"1) {ocr_prompt}（結果を ocr_text に格納）\n"
            f"2) {format_prompt}（結果を records に格納）"
        )
        resp = _call_with_retry(
            lambda: client.models.generate_content(
                model=model_name,
                contents=[image_part, combined_prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=combined_schema,
                    temperature=0.0,
                ),
            )
        )
        parsed = json.loads(resp.text)
        ocr_text = parsed.get("ocr_text", "")
        records = parsed.get("records", [])
    else:
        raise ValueError(f"不正なmode指定です: {mode} ('two-step' または 'single')")

    jsonl_lines = [json.dumps(rec, ensure_ascii=False) for rec in records]
    jsonl_str = "\n".join(jsonl_lines)

    return {
        "format_id": format_id,
        "model": model_name,
        "ocr_text": ocr_text,
        "records": records,
        "jsonl": jsonl_str,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="検査結果画像 OCR & JSONL変換コアCLI")
    parser.add_argument("--image", help="入力画像ファイルパス")
    parser.add_argument("--format", dest="format_id", help="使用するプロンプトフォーマットID")
    parser.add_argument("--list-formats", action="store_true", help="利用可能なフォーマット一覧をJSONで表示")
    parser.add_argument("--mode", choices=["two-step", "single"], default="two-step", help="実行モード")
    parser.add_argument("--model", default=None, help=f"Geminiモデル名 (既定: 環境変数 or {DEFAULT_MODEL})")
    parser.add_argument("--output", help="JSONLの出力ファイルパス（省略時は標準出力）")
    parser.add_argument("--append", action="store_true", help="出力ファイルへ追記する")
    parser.add_argument("--save-ocr", help="第1段階のOCRテキストを保存するパス")
    parser.add_argument("--prompts-dir", default=str(DEFAULT_PROMPTS_DIR), help="プロンプト定義ディレクトリ")
    args = parser.parse_args()

    prompts_dir = Path(args.prompts_dir)

    if args.list_formats:
        items = [{"id": f["id"], "label": f["label"]} for f in list_formats(prompts_dir)]
        print(json.dumps(items, ensure_ascii=False))
        return 0

    if not args.image or not args.format_id:
        parser.error("--image と --format の両方を指定してください（一覧確認は --list-formats）")

    result = process_image_to_jsonl(
        image_path=args.image,
        format_id=args.format_id,
        mode=args.mode,
        model=args.model,
        prompts_dir=prompts_dir,
    )

    if args.save_ocr:
        Path(args.save_ocr).write_text(result["ocr_text"] + "\n", encoding="utf-8")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_mode = "a" if args.append else "w"
        with out_path.open(write_mode, encoding="utf-8") as f:
            if result["jsonl"]:
                f.write(result["jsonl"] + "\n")
        print(
            f"[OK] {len(result['records'])}件を {out_path} へ保存しました (model={result['model']})",
            file=sys.stderr,
        )
    else:
        print(result["jsonl"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
