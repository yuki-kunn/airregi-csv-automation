"""
Cookie取得ツール（初回・手動）

AirREGIはRecruit ID/OAuthログインでCAPTCHA/2段階認証が出る可能性があるため、
自動ログインではなく「人がブラウザで一度ログイン」してCookieを保存する方式を採る。

使い方:
    HEADLESS=false python src/cookie_tool.py

ブラウザが立ち上がるので手動でログイン → 売上ページが表示されたらターミナルで
Enterを押す。Cookieが airregi_cookies.json に保存される。
その中身を GitHub Secrets `AIRREGI_COOKIES` に1行で貼り付ける。
（base64化したい場合は出力されるコマンドを利用）
"""

import json
import logging
import os

import config
from browser import create_driver

logger = logging.getLogger(__name__)

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "airregi_cookies.json",
)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if config.HEADLESS:
        print(
            "⚠ HEADLESS=true ではログイン操作ができません。\n"
            "  HEADLESS=false python src/cookie_tool.py で再実行してください。"
        )
        return

    driver = create_driver()
    try:
        driver.get(config.AIRREGI_SALES_URL)
        print("\n========================================")
        print("ブラウザでAirREGIにログインしてください。")
        print(f"  ID:   {config.AIRREGI_ID}")
        print(f"  PASS: {config.AIRREGI_PASS}")
        print("売上ページが表示されたら、ここで Enter を押してください。")
        print("========================================\n")
        input("ログイン完了後 Enter > ")

        # airregi.jp / connect.airregi.jp 両方のCookieを集める
        all_cookies = []
        for origin in ("https://airregi.jp/", "https://connect.airregi.jp/"):
            try:
                driver.get(origin)
                all_cookies.extend(driver.get_cookies())
            except Exception as e:  # noqa: BLE001
                logger.warning("Cookie取得失敗(%s): %s", origin, e)

        # name重複を除去
        seen = set()
        deduped = []
        for c in all_cookies:
            key = (c.get("domain"), c.get("name"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)

        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(deduped, f, ensure_ascii=False)

        print(f"\n✅ {len(deduped)}個のCookieを保存しました: {OUTPUT_PATH}")
        print("\n--- GitHub Secrets `AIRREGI_COOKIES` にこの1行を貼り付け ---")
        print(json.dumps(deduped, ensure_ascii=False))
        print("\n（ローカル .env に入れる場合は AIRREGI_COOKIES='...' として上記を設定）")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
