#!/usr/bin/env python3
"""
sil 回帰テスト用の評価スクリプト

main.py と同じ手順でシステムプロンプトを組み立て、temperature=0 で
Claude を呼び、tests/cases.yaml の期待値と照合する。

使い方:
    export ANTHROPIC_API_KEY=sk-ant-...
    pip install anthropic pyyaml

    # 全件実行（前回結果 baseline.json と比較）
    python tests/eval.py

    # 今回の結果をベースラインとして保存（Phase 1 の最後にこれを実行）
    python tests/eval.py --save-baseline

    # 一部だけ実行（ID / カテゴリ / 質問文の部分一致）
    python tests/eval.py --filter 粗大

    # 分離後の新ファイルで実行（Phase 2 以降）
    python tests/eval.py --system-prompt system_prompt.txt --knowledge knowledge.md

    # ケース一覧を見るだけ（API を呼ばない）
    python tests/eval.py --list
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
    return f"__ERROR__ {last}"


def run(cases: list[dict], system_prompt: str, globals_: dict, workers: int) -> list[dict]:
    client = Anthropic()

    def one(case: dict) -> dict:
        answer = ask(client, system_prompt, case["q"])
        fails = ["API エラー"] if answer.startswith("__ERROR__") else judge(answer, case, globals_)
        return {
            "id": case["id"],
            "category": case.get("category", ""),
            "q": case["q"],
            "answer": answer,
            "pass": not fails,
            "fails": fails,
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, cases))


def report(results: list[dict], baseline: dict | None) -> int:
    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    print("\n" + "=" * 72)
    print(f"  結果: {passed}/{total} PASS")
    print("=" * 72 + "\n")

    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"[{mark}] {r['id']:<14} {r['category']:<12} {r['q']}")
        for f in r["fails"]:
            print(f"         └ {f}")
        if not r["pass"]:
            print("         ── 実際の回答 ──")
            for line in r["answer"].splitlines():
                print(f"         | {line}")
            print()

    if not baseline:
        print("\n（ベースラインなし。--save-baseline で今回の結果を基準として保存できます）")
        return 0 if passed == total else 1

    prev = {r["id"]: r for r in baseline["results"]}
    regressions, fixes, changed = [], [], []
    for r in results:
        p = prev.get(r["id"])
        if not p:
            continue
        if p["pass"] and not r["pass"]:
            regressions.append(r["id"])
        elif not p["pass"] and r["pass"]:
            fixes.append(r["id"])
        if p["answer"] != r["answer"]:
            changed.append(r["id"])

    print("\n" + "-" * 72)
    print(f"  ベースライン比較（{baseline.get('timestamp', '?')}）")
    print("-" * 72)
    print(f"  退行 (PASS→FAIL) : {regressions or 'なし'}")
    print(f"  改善 (FAIL→PASS) : {fixes or 'なし'}")
    print(f"  回答文が変化      : {len(changed)}件 {changed if changed else ''}")
    print()
    print("  ※ Phase 2（構造分離）では『回答文が変化: 0件』が合格条件です")
    print("  ※ Phase 3（修正）では、意図したケースだけが変化していることを確認してください")
    print()

    return 1 if regressions else 0


def write_github_summary(results: list[dict], baseline: dict | None) -> None:
    """GitHub Actions のサマリー欄に結果を表で出す（ブラウザで読める）"""
    import os
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    out = [f"## sil 回帰テスト: {passed}/{total} PASS", ""]

    if baseline:
        prev = {r["id"]: r for r in baseline["results"]}
        reg = [r["id"] for r in results if prev.get(r["id"], {}).get("pass") and not r["pass"]]
        fix = [r["id"] for r in results if r["id"] in prev and not prev[r["id"]]["pass"] and r["pass"]]
        chg = [r["id"] for r in results if r["id"] in prev and prev[r["id"]]["answer"] != r["answer"]]
        out += [
            f"- 退行 (PASS→FAIL): **{', '.join(reg) if reg else 'なし'}**",
            f"- 改善 (FAIL→PASS): {', '.join(fix) if fix else 'なし'}",
            f"- 回答文が変化: **{len(chg)}件** {', '.join(chg)}",
            "",
            "> Phase 2（構造分離）は「回答文が変化: 0件」が合格条件",
            "",
        ]

    out += ["| | ID | カテゴリ | 質問 | 違反 |", "|---|---|---|---|---|"]
    for r in results:
        mark = "PASS" if r["pass"] else "**FAIL**"
        fails = "<br>".join(f.replace("|", "\\|") for f in r["fails"]) or "-"
        q = r["q"].replace("|", "\\|")
        out.append(f"| {mark} | `{r['id']}` | {r['category']} | {q} | {fails} |")

    fails_only = [r for r in results if not r["pass"]]
    if fails_only:
        out += ["", "## FAIL の実際の回答", ""]
        for r in fails_only:
            out += [f"### `{r['id']}` {r['q']}", "", "```", r["answer"], "```", ""]

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    ap.add_argument("--knowledge", default=DEFAULT_KNOWLEDGE,
                    help="空文字を渡すと知識ベースを連結しない（1ファイル構成にした場合）")
    ap.add_argument("--cases", default=str(Path(__file__).resolve().parent / "cases.yaml"))
    ap.add_argument("--filter", default="", help="ID / カテゴリ / 質問文への部分一致")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    data = yaml.safe_load(Path(args.cases).read_text(encoding="utf-8"))
    globals_ = data.get("globals", {})
    cases = data["cases"]

    if args.filter:
        f = args.filter
        cases = [c for c in cases
                 if f in c["id"] or f in c.get("category", "") or f in c["q"]]

    if args.list:
        for c in cases:
            print(f"{c['id']:<14} {c.get('category',''):<12} {c['q']}")
        print(f"\n{len(cases)}件")
        return 0

    if not cases:
        print("該当ケースがありません")
        return 1

    sp_path = (ROOT / args.system_prompt) if not Path(args.system_prompt).is_absolute() else Path(args.system_prompt)
    kb_path = None
    if args.knowledge:
        kb_path = (ROOT / args.knowledge) if not Path(args.knowledge).is_absolute() else Path(args.knowledge)

    for p in [sp_path] + ([kb_path] if kb_path else []):
        if not p.exists():
            sys.exit(f"ファイルが見つかりません: {p}")

    system_prompt = build_system_prompt(sp_path, kb_path)
    print(f"system prompt : {sp_path}")
    print(f"knowledge     : {kb_path or '(連結なし)'}")
    print(f"合計文字数    : {len(system_prompt):,}")
    print(f"model         : {MODEL} / temperature={TEMPERATURE}")
    print(f"ケース数      : {len(cases)}")

    results = run(cases, system_prompt, globals_, args.workers)

    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    code = report(results, baseline)
    write_github_summary(results, baseline)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "system_prompt": str(sp_path),
        "knowledge": str(kb_path) if kb_path else None,
        "model": MODEL,
        "temperature": TEMPERATURE,
        "results": results,
    }
    LATEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"結果を保存: {LATEST}")

    if args.save_baseline:
        BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"ベースラインを更新: {BASELINE}")

    return code


if __name__ == "__main__":
    raise SystemExit(main())
