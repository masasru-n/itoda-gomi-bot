#!/usr/bin/env python3
"""
sil 回帰テスト用の評価スクリプト

main.py と同じ手順でシステムプロンプトを組み立て、temperature=0 で
Claude を呼び、tests/cases.yaml の期待値と照合する。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("pyyaml が必要です:  pip install pyyaml")

try:
    from anthropic import Anthropic
except ImportError:
    sys.exit("anthropic が必要です:  pip install anthropic")


# ── main.py と揃える。ここを変えたら main.py も変える ──────────────
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1000
TEMPERATURE = 0

DEFAULT_SYSTEM_PROMPT = "system_prompt_v3.txt"
DEFAULT_KNOWLEDGE = "itoda_gomi_knowledge_v2.md"

# 出力してよい電話番号はこれ1つだけ（システムプロンプトの絶対ルール）
ALLOWED_PHONE = "0947-26-0917"
PHONE_RE = re.compile(r"0\d{1,3}[-‐−ー－ｰ]\d{2,4}[-‐−ー－ｰ]\d{3,4}")

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"
BASELINE = RESULTS_DIR / "baseline.json"
LATEST = RESULTS_DIR / "latest.json"


def build_system_prompt(sp_path: Path, kb_path: Path | None) -> str:
    """main.py の SYSTEM_PROMPT 組み立てと同一にすること"""
    base = sp_path.read_text(encoding="utf-8")
    if kb_path is None:
        return base
    kb = kb_path.read_text(encoding="utf-8")
    return f"{base}\n# 知識ベース（糸田町のごみ分別ルール）\n{kb}\n"


def judge(answer: str, case: dict, globals_: dict) -> list[str]:
    """違反の一覧を返す。空リストなら PASS"""
    fails: list[str] = []

    for s in case.get("expect_all", []):
        if s not in answer:
            fails.append(f"expect_all 未検出: {s!r}")

    any_list = case.get("expect_any", [])
    if any_list and not any(s in answer for s in any_list):
        fails.append(f"expect_any がどれも未検出: {any_list}")

    for s in case.get("forbid", []):
        if s in answer:
            fails.append(f"forbid 検出: {s!r}")

    for s in globals_.get("forbid", []):
        if s in answer:
            fails.append(f"[共通] forbid 検出: {s!r}")

    if globals_.get("phone_check", True):
        for m in set(PHONE_RE.findall(answer)):
            if m != ALLOWED_PHONE:
                fails.append(f"[共通] 許可外の電話番号: {m}")

    return fails


def ask(client: Anthropic, system_prompt: str, question: str, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=system_prompt,
                messages=[{"role": "user", "content": question}],
            )
            return msg.content[0].text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    return
