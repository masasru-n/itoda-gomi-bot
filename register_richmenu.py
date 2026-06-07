"""
リッチメニューを LINE Messaging API で登録するスクリプト（1回だけ実行）。

前提:
  - 環境変数 LINE_CHANNEL_ACCESS_TOKEN が設定されていること
  - 同じディレクトリに richmenu_v6.png（確定画像・2500x843）があること
  - 実行前に LINE 公式アカウント管理画面の既存リッチメニューを「オフ」にしておくこと
    （管理画面の設定とAPI設定が競合するため）

実行:
  pip install requests
  LINE_CHANNEL_ACCESS_TOKEN=xxxx python register_richmenu.py

注意:
  API登録後は、画像・文言・リンクの変更は管理画面ではなくこのスクリプトの
  再実行で行うことになる。
"""
import os
import sys
import requests

TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
if not TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN が未設定です")

IMAGE_PATH = "richmenu_v6.png"  # 確定画像
LP_URL = "https://itodaseisou.jp/lp"

H = {"Authorization": f"Bearer {TOKEN}"}

richmenu = {
    "size": {"width": 2500, "height": 843},
    "selected": True,
    "name": "sil_main_v1",
    "chatBarText": "メニュー",
    "areas": [
        # 左：糸田町のごみ案内AI → 使い方ガイド（テキストは飛ばさない）
        {"bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
         "action": {"type": "postback", "data": "action=usage_guide"}},
        # 中央：自宅の片付けサービス → 片付け案内（テキストは飛ばさない）
        {"bounds": {"x": 833, "y": 0, "width": 834, "height": 843},
         "action": {"type": "postback", "data": "action=cleanup_menu"}},
        # 右：糸田清掃 公式サイト → LP を開く
        {"bounds": {"x": 1667, "y": 0, "width": 833, "height": 843},
         "action": {"type": "uri", "uri": LP_URL}},
    ],
}


def main():
    # 1) 定義を登録
    r = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers={**H, "Content-Type": "application/json"},
        json=richmenu,
    )
    r.raise_for_status()
    rid = r.json()["richMenuId"]
    print("richMenuId:", rid)

    # 2) 画像をアップロード
    with open(IMAGE_PATH, "rb") as f:
        r = requests.post(
            f"https://api-data.line.me/v2/bot/richmenu/{rid}/content",
            headers={**H, "Content-Type": "image/png"},
            data=f.read(),
        )
    r.raise_for_status()
    print("image uploaded")

    # 3) 全ユーザーのデフォルトに設定
    r = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rid}",
        headers=H,
    )
    r.raise_for_status()
    print("set as default. done.")


if __name__ == "__main__":
    main()
