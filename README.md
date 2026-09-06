# sil 回帰テスト

プロンプト／知識ベースを変更したときに、意図しない挙動変化が起きていないかを検査する。

## GitHub の画面だけで実行する場合（推奨）

`.github/workflows/eval.yml` を配置し、リポジトリの
Settings → Secrets and variables → Actions に `ANTHROPIC_API_KEY` を登録する。

1. Actions タブ → 左の「sil 回帰テスト」を選ぶ
2. 右の **Run workflow** を押す
   - `filter`：一部だけ実行したいとき（空欄なら全件）
   - `save_baseline`：チェックすると結果をベースラインとして
     `tests/results/baseline.json` にコミットする
3. 実行後、**Summary 画面に結果の表と FAIL の実際の回答が出る**
4. `latest.json` は Artifacts からダウンロードできる

ローカルに Python 環境は不要。

## 準備（ローカルで動かす場合）

```bash
pip install anthropic pyyaml
export ANTHROPIC_API_KEY=sk-ant-...
```

`tests/` をリポジトリ直下に置くこと。`eval.py` は親ディレクトリを
リポジトリルートとみなし、`system_prompt_v3.txt` と
`itoda_gomi_knowledge_v2.md` を main.py と同じ手順で連結する。

## 実行

```bash
# 全件（33件）実行。ベースラインがあれば差分も表示
python tests/eval.py

# 今回の結果をベースラインとして保存
python tests/eval.py --save-baseline

# 一部だけ（ID / カテゴリ / 質問文への部分一致）
python tests/eval.py --filter 粗大

# 分離後の新ファイル構成で実行（Phase 2 以降）
python tests/eval.py --system-prompt system_prompt.txt --knowledge knowledge.md

# ケース一覧だけ表示（API を呼ばない）
python tests/eval.py --list
```

## 判定の仕組み

| 項目 | 意味 |
|---|---|
| `expect_all` | すべて含まれること |
| `expect_any` | いずれか1つ以上含まれること |
| `forbid` | 1つも含まれないこと |
| `globals.forbid` | 全ケース共通の禁止語（絵文字・マークダウン・法律名・確認調） |
| `globals.phone_check` | `0947-26-0917` 以外の電話番号が出ていないかを正規表現で検査 |

共通ルールは全33ケースに適用されるため、絵文字や法律名の混入は
どのケースで起きても検出できる。

## フェーズ別の合格条件

| Phase | 合格条件 |
|---|---|
| 1 ベースライン取得 | 期待値を確定し `--save-baseline` を実行 |
| 2 構造分離 | **回答文が変化: 0件**（挙動を1文字も変えない） |
| 3 修正 | 意図したケースだけが変化。退行 0件 |

## 運用

bad評価が付いた質問は、そのまま `cases.yaml` に1件追加する。
temperature=0 のため同じ質問には同じ回答が返り、再現テストとして機能する。

## 注意

- `MODEL` / `MAX_TOKENS` / `TEMPERATURE` は main.py と一致させること。
  片方だけ変えるとテストが本番を測れなくなる。
- `build_system_prompt()` は main.py の `SYSTEM_PROMPT` 組み立てと同一。
  main.py 側を変えたらここも変える。
- 期待値は「町の実運用として正しいか」で決める。判断がつかないものは
  `note` に「要確認」と書き、糸田清掃に確認してから確定する。
