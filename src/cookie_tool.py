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
import sys

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
        if config.AIRREGI_ID:
            print(f"  ID:   {config.AIRREGI_ID}")
        if config.AIRREGI_PASS:
            print(f"  PASS: {config.AIRREGI_PASS}")
        print("----------------------------------------")
        print("売上ページに到達すると自動でCookieを保存します。")
        print("（手動でEnterを押しても確定できます）")
        print("中断する場合は Ctrl+C。")
        print("========================================\n")

        # 売上ページ到達を自動検知（Enter入力に依存しない）。
        # ターミナルが選択モード(COPYMODE)でも確実に進む。
        import select
        import time

        deadline = time.time() + 300  # 最大5分待つ
        detected = False
        while time.time() < deadline:
            url = driver.current_url
            on_login = (
                config.AIRREGI_LOGIN_HOST in url or "/view/login" in url
            )
            if not on_login and "salesListByMenu" in url:
                print(f"\n✅ 売上ページを検知しました: {url}")
                detected = True
                break
            # 手動Enterでも抜けられるように標準入力を非ブロッキングで覗く。
            # 端末(tty)のときだけ有効化（/dev/null等のEOF誤発火を防ぐ）。
            if sys.stdin.isatty():
                ready, _, _ = select.select([sys.stdin], [], [], 2)
                if ready:
                    line = sys.stdin.readline()
                    if line:  # EOFでない実入力のみ確定
                        print("\n⏎ 手動確定を受け付けました。")
                        detected = True
                        break
            else:
                time.sleep(2)

        if not detected:
            print("\n⚠ タイムアウト（5分）。現在の状態でCookieを保存します。")

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
