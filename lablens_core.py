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
DEFAULT_ENV_FILES = (
    Path.home() / ".lablens.env",
    Path.home() / ".lab_ocr.env",
)
DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
)
DEFAULT_TIMEOUT_SEC = 60.0


def _log(message: str) -> None:
    """標準出力(JSONL)を汚さずに進捗・警告を標準エラー出力へ出す。"""
    print(message, file=sys.stderr, flush=True)


def load_env_file(env_path: Path | None = None) -> None:
    """設定ファイルから未設定の環境変数を読み込む（Termux:Widget対策）。"""
    targets = (env_path,) if env_path is not None else DEFAULT_ENV_FILES
    for path in targets:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
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


def _load_image_part(image_path: Path) -> types.Part:
    """Pillow不要で画像ファイルをバイト列Partとして読み込む。"""
    resolved = image_path.expanduser()
    if not resolved.exists():
        parent = resolved.parent
        if not parent.exists():
            hint = (
                f"親ディレクトリが存在しません: {parent}\n"
                "※ ~/storage 配下を使う場合は先に `termux-setup-storage` を実行してください。"
            )
        else:
            candidates = [p.name for p in sorted(parent.iterdir()) if p.is_file()][:10]
            hint = f"フォルダ内のファイル候補: {', '.join(candidates) if candidates else '(なし)'}"
        raise FileNotFoundError(f"画像ファイルが見つかりません: {resolved}\n{hint}")

    mime_type, _ = mimetypes.guess_type(str(resolved))
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"
    data = resolved.read_bytes()
    _log(f"[Image] {resolved.name} ({len(data) / 1024:.1f} KB, {mime_type})")
    return types.Part.from_bytes(data=data, mime_type=mime_type)


def _build_config(
    *,
    temperature: float = 0.0,
    response_mime_type: str | None = None,
    response_schema: dict[str, Any] | None = None,
    low_thinking: bool = False,
) -> types.GenerateContentConfig:
    """GenerateContentConfig を構築する。"""
    kwargs: dict[str, Any] = {"temperature": temperature}
    if response_mime_type is not None:
        kwargs["response_mime_type"] = response_mime_type
    if response_schema is not None:
        kwargs["response_schema"] = response_schema
    if low_thinking:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="LOW")
    return types.GenerateContentConfig(**kwargs)


def _generate_with_fallback(
    client: genai.Client,
    primary_model: str,
    contents: Any,
    *,
    step_label: str,
    response_mime_type: str | None = None,
    response_schema: dict[str, Any] | None = None,
    low_thinking: bool = False,
    max_429_retries: int = 3,
    base_429_wait: float = 5.0,
) -> tuple[Any, str]:
    """503/404時のモデル自動フォールバックと429時の指数バックオフリトライ付きで生成を実行する。"""
    candidates = [primary_model] + [m for m in FALLBACK_MODELS if m != primary_model]
    last_err: Exception | None = None

    for idx, model_name in enumerate(candidates):
        use_low_thinking = low_thinking
        attempt_429 = 0

        while True:
            t0 = time.monotonic()
            _log(f"[{step_label}] リクエスト送信中 (model={model_name})...")
            config = _build_config(
                temperature=0.0,
                response_mime_type=response_mime_type,
                response_schema=response_schema,
                low_thinking=use_low_thinking,
            )

            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                elapsed = time.monotonic() - t0
                _log(f"[{step_label}] 完了 (model={model_name}, {elapsed:.2f}s)")
                return resp, model_name

            except APIError as e:
                last_err = e
                code = e.code

                # thinking_config 非対応モデルで 400 が出た場合は外して即リトライ
                if code == 400 and use_low_thinking and "thinking" in str(e).lower():
                    _log(f"[{step_label}] {model_name} は thinking_config 非対応のため無効化して再試行します。")
                    use_low_thinking = False
                    continue

                # 429: 無料枠レート制限 -> 指数バックオフで待機リトライ
                if code == 429:
                    if attempt_429 < max_429_retries:
                        wait = base_429_wait * (2 ** attempt_429)
                        attempt_429 += 1
                        _log(
                            f"[{step_label}] [429 Rate Limit] {wait:.0f}秒待機後に再試行します "
                            f"({attempt_429}/{max_429_retries}, model={model_name})..."
                        )
                        time.sleep(wait)
                        continue
                    if idx + 1 < len(candidates):
                        _log(
                            f"[{step_label}] [429] {model_name} のリトライ上限に達したため "
                            f"{candidates[idx + 1]} へ切り替えます..."
                        )
                        break
                    raise

                # 503 (高負荷) / 404 (モデル未対応) -> 次モデルへ自動フォールバック
                if code in (503, 404):
                    if idx + 1 < len(candidates):
                        reason = "高負荷(503)" if code == 503 else "未対応(404)"
                        _log(
                            f"[{step_label}] [{reason}] {model_name} から "
                            f"{candidates[idx + 1]} へ自動フォールバックします..."
                        )
                        break
                    raise

                # その他のエラー (403 APIキー無効 等) は即座に送出
                raise

            except Exception as e:
                # タイムアウト等の一時的な通信例外も、別モデルが残っていればフォールバックを試みる
                last_err = e
                if idx + 1 < len(candidates) and "timeout" in str(e).lower():
                    _log(
                        f"[{step_label}] [Timeout] {model_name} がタイムアウトしたため "
                        f"{candidates[idx + 1]} へ切り替えます..."
                    )
                    break
                raise

    if last_err is not None:
        raise last_err
    raise RuntimeError("利用可能なモデル候補がありません。")


def process_image_to_jsonl(
    image_path: str | Path,
    format_id: str,
    *,
    mode: str = "two-step",
    model: str | None = None,
    prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    step_interval: float = 1.5,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """画像をOCRし、指定フォーマットで整形したJSONL文字列と詳細を返す。"""
    load_env_file()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY が設定されていません（~/.lablens.env または ~/.lab_ocr.env を確認してください）"
        )

    primary_model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    fmt = load_format_config(format_id, prompts_dir)
    image_part = _load_image_part(Path(image_path))

    # HttpOptions.timeout はミリ秒指定
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=int(timeout_sec * 1000)),
    )

    ocr_prompt = fmt["ocr_prompt"]
    format_prompt = fmt["format_prompt"]
    response_schema = fmt["response_schema"]

    if mode == "two-step":
        # 【第1段階】画像 → プレーンOCRテキスト（思考を抑制して高速化）
        ocr_resp, ocr_model = _generate_with_fallback(
            client,
            primary_model,
            [image_part, ocr_prompt],
            step_label="Step 1/2: OCR",
            low_thinking=True,
        )
        ocr_text = (ocr_resp.text or "").strip()
        _log(f"[Step 1/2: OCR] 抽出文字数: {len(ocr_text)} 文字")

        if step_interval > 0:
            time.sleep(step_interval)

        # 【第2段階】OCRテキスト → 選択プロンプトによる構造化JSON
        # 第1段階でフォールバックが起きた場合は、通ったモデルを優先して第2段階に使う
        fmt_resp, format_model = _generate_with_fallback(
            client,
            ocr_model,
            f"{format_prompt}\n\n--- OCRテキスト ---\n{ocr_text}",
            step_label="Step 2/2: JSON整形",
            response_mime_type="application/json",
            response_schema=response_schema,
            low_thinking=True,
        )
        parsed = json.loads(fmt_resp.text)
        records = parsed.get("records", [])
        used_model = format_model

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
        resp, used_model = _generate_with_fallback(
            client,
            primary_model,
            [image_part, combined_prompt],
            step_label="Single Pass: OCR+整形",
            response_mime_type="application/json",
            response_schema=combined_schema,
            low_thinking=True,
        )
        parsed = json.loads(resp.text)
        ocr_text = parsed.get("ocr_text", "")
        records = parsed.get("records", [])
        ocr_model = used_model
        format_model = used_model
    else:
        raise ValueError(f"不正なmode指定です: {mode} ('two-step' または 'single')")

    jsonl_lines = [json.dumps(rec, ensure_ascii=False) for rec in records]
    jsonl_str = "\n".join(jsonl_lines)

    return {
        "format_id": format_id,
        "model": used_model,
        "ocr_model": ocr_model,
        "format_model": format_model,
        "ocr_text": ocr_text,
        "records": records,
        "jsonl": jsonl_str,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="検査結果画像 OCR & JSONL変換コアCLI (lablens)")
    parser.add_argument("--image", help="入力画像ファイルパス")
    parser.add_argument("--format", dest="format_id", help="使用するプロンプトフォーマットID")
    parser.add_argument("--list-formats", action="store_true", help="利用可能なフォーマット一覧をJSONで表示")
    parser.add_argument("--mode", choices=["two-step", "single"], default="two-step", help="実行モード")
    parser.add_argument("--model", default=None, help=f"優先Geminiモデル名 (既定: 環境変数 or {DEFAULT_MODEL})")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"1リクエストあたりのタイムアウト秒数 (既定: {DEFAULT_TIMEOUT_SEC:.0f})",
    )
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
        timeout_sec=args.timeout,
    )

    if args.save_ocr:
        ocr_path = Path(args.save_ocr).expanduser()
        ocr_path.parent.mkdir(parents=True, exist_ok=True)
        ocr_path.write_text(result["ocr_text"] + "\n", encoding="utf-8")

    if args.output:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_mode = "a" if args.append else "w"
        with out_path.open(write_mode, encoding="utf-8") as f:
            if result["jsonl"]:
                f.write(result["jsonl"] + "\n")
        _log(f"[OK] {len(result['records'])}件を {out_path} へ保存しました (model={result['model']})")
    else:
        print(result["jsonl"])

    return 0


if __name__ == "__main__":
    sys.exit(main())



