"""
AirREGI スクレイパ（Cookie再利用方式）

毎回のOAuth/CAPTCHAを避けるため、cookie_tool.py で取得済みのCookieを注入して
セッションを復元し、商品別売上ページから当日CSVをダウンロードする。

ログイン状態は要素/URLで検証し、未ログイン（connect.airregi.jp へ誘導）なら
例外を送出して run.py 側で failed ログを残す（サイレント失敗の防止）。
"""

import argparse
import glob
import json
import logging
import os
import shutil
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config
from browser import create_driver

logger = logging.getLogger(__name__)


class LoginExpiredError(RuntimeError):
    """Cookieが失効しAirREGIに未ログインだった場合に送出。"""


def _load_cookies() -> list[dict]:
    """Cookieを取得する。優先順位:
    1. Firestore automation/config.airregiCookies（admin画面で登録）
    2. 環境変数 AIRREGI_COOKIES
    3. ローカルファイル airregi_cookies.json
    """
    raw = ""
    # 1. Firestore（admin画面でDevToolsから登録したもの）
    try:
        import firestore_client as fs

        raw = fs.get_cookies()
        if raw:
            logger.info("CookieをFirestoreから読み込みました")
    except Exception as e:  # noqa: BLE001
        logger.debug("Firestoreからのcookie取得をスキップ: %s", e)

    # 2. 環境変数
    if not raw:
        raw = config.AIRREGI_COOKIES_JSON

    # 3. ローカルファイル
    if not raw:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "airregi_cookies.json",
        )
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = f.read()

    if not raw or not raw.strip():
        raise LoginExpiredError(
            "AirREGIのCookieが未登録です。admin画面の「Cookie登録」から登録してください。"
        )
    return parse_cookies(raw)


def parse_cookies(raw: str) -> list[dict]:
    """貼り付けられたCookieを正規化する。

    対応形式:
      - JSON配列（Cookie-Editor / cookie_tool.py の出力）
      - DevTools "Application > Cookies" の表をコピーしたタブ区切りテキスト
        （1行目ヘッダ: Name<TAB>Value<TAB>Domain<TAB>Path ...）
    """
    raw = raw.strip()
    if not raw:
        return []

    # JSON形式を優先
    if raw[0] in "[{":
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return [_normalize_cookie(c) for c in data if c.get("name")]

    # タブ/カンマ区切りテーブル
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    cookies: list[dict] = []
    header = None
    # DevToolsのCookies表の既定列順（ヘッダ無しでコピーされた場合に使用）
    # Name, Value, Domain, Path, Expires/Max-Age, Size, HttpOnly, Secure, ...
    DEVTOOLS_COLS = ["name", "value", "domain", "path", "expiry"]

    for i, line in enumerate(lines):
        cols = line.split("\t") if "\t" in line else line.split(",")
        cols = [c.strip() for c in cols]

        # 1行目がヘッダ行か判定
        if i == 0 and any(h.lower() in ("name", "cookie") for h in cols):
            header = [c.lower() for c in cols]
            continue

        # ヘッダ付きテーブル
        if header:
            row = dict(zip(header, cols))
            name = row.get("name") or row.get("cookie")
            if not name:
                continue
            cookies.append(
                _normalize_cookie(
                    {
                        "name": name,
                        "value": row.get("value", ""),
                        "domain": row.get("domain", ""),
                        "path": row.get("path", "/"),
                    }
                )
            )
        # タブ区切りで3列以上 → ヘッダ無しDevTools表として固定列で解釈
        elif "\t" in line and len(cols) >= 3:
            row = dict(zip(DEVTOOLS_COLS, cols))
            name = row.get("name", "")
            if not name:
                continue
            expiry = row.get("expiry", "")
            # "セッション"/"Session" 等はexpiryなし扱い
            if expiry and expiry.lower() not in ("session", "セッション"):
                # ISO日時(2027-05-14T...)はそのまま渡し _normalize_cookie で無視させる
                row["expiry"] = "" if "T" in expiry or "-" in expiry else expiry
            else:
                row["expiry"] = ""
            cookies.append(_normalize_cookie(row))
        # "name=value" 形式の素朴なフォールバック
        elif "=" in line:
            name, _, value = line.partition("=")
            cookies.append(
                _normalize_cookie(
                    {
                        "name": name.strip(),
                        "value": value.strip().rstrip(";").strip(),
                    }
                )
            )
    return cookies


def _normalize_cookie(c: dict) -> dict:
    """add_cookie に渡せる最小フィールドへ整える。"""
    out = {
        "name": c["name"],
        "value": c.get("value", ""),
        "path": c.get("path", "/") or "/",
    }
    if c.get("domain"):
        out["domain"] = c["domain"]
    expiry = c.get("expiry") or c.get("expirationDate") or c.get("expires")
    if expiry:
        try:
            out["expiry"] = int(float(expiry))
        except (ValueError, TypeError):
            pass
    return out


def _inject_cookies(driver, cookies: list[dict]) -> None:
    """Cookieを注入する。ドメインごとに該当オリジンを開いてから add する。"""
    # まず airregi.jp を開く（about:blank には cookie を add できない）
    driver.get("https://airregi.jp/")
    _safe_add(driver, cookies, suffix="airregi.jp")
    # connect.airregi.jp 用Cookieも復元
    driver.get("https://connect.airregi.jp/")
    _safe_add(driver, cookies, suffix="connect.airregi.jp")


def _safe_add(driver, cookies: list[dict], suffix: str) -> None:
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        # domainが指定されている場合のみ suffix で絞り込む。
        # domainが空（DevTools表でdomain列が無い等）は現在のオリジン用として注入。
        if domain and suffix not in domain:
            continue
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "path": c.get("path", "/"),
        }
        if "expiry" in c and c["expiry"]:
            cookie["expiry"] = int(c["expiry"])
        # domain は現在のオリジンと不一致だと拒否されるため付けない
        try:
            driver.add_cookie(cookie)
        except Exception as e:  # noqa: BLE001
            logger.debug("cookie add 失敗(%s): %s", c.get("name"), e)


def _verify_logged_in(driver) -> None:
    driver.get(config.AIRREGI_SALES_URL)
    WebDriverWait(driver, config.PAGE_LOAD_TIMEOUT).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    current = driver.current_url
    if config.AIRREGI_LOGIN_HOST in current or "/view/login" in current:
        raise LoginExpiredError(
            f"AirREGIに未ログイン（ログイン画面にリダイレクト）: {current}"
        )
    logger.info("AirREGIログイン確認OK: %s", current)


def _wait_download(download_dir: str, before: set[str], timeout: int) -> str:
    """新規にダウンロードされた .csv ファイルパスを返す。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = set(glob.glob(os.path.join(download_dir, "*.csv")))
        new_files = files - before
        # .crdownload が残っていない完了済みのものを採用
        crdownload = glob.glob(os.path.join(download_dir, "*.crdownload"))
        if new_files and not crdownload:
            return max(new_files, key=os.path.getctime)
        time.sleep(1)
    raise TimeoutError("CSVダウンロードがタイムアウトしました")


def download_csv(date_str: str) -> str:
    """当日（date_str: YYYY-MM-DD）の売上CSVをダウンロードし、
    投入先の命名規約にリネームしたパスを返す。
    """
    download_dir = config.DOWNLOAD_DIR
    os.makedirs(download_dir, exist_ok=True)

    driver = create_driver(download_dir=download_dir)
    try:
        cookies = _load_cookies()
        _inject_cookies(driver, cookies)
        _verify_logged_in(driver)

        before = set(glob.glob(os.path.join(download_dir, "*.csv")))

        # CSVダウンロードボタンを探してクリック。
        # AirREGIの売上ページのDLリンクは表記揺れがあるため複数候補で探索。
        wait = WebDriverWait(driver, config.ELEMENT_WAIT_TIMEOUT)
        candidates = [
            (By.XPATH, "//a[contains(text(),'CSV')]"),
            (By.XPATH, "//button[contains(text(),'CSV')]"),
            (By.XPATH, "//a[contains(text(),'ダウンロード')]"),
            (By.XPATH, "//button[contains(text(),'ダウンロード')]"),
            (By.CSS_SELECTOR, "a[href*='csv'], a[href*='download']"),
        ]
        clicked = False
        for by, sel in candidates:
            try:
                el = wait.until(EC.element_to_be_clickable((by, sel)))
                el.click()
                clicked = True
                logger.info("CSVダウンロード要素をクリック: %s", sel)
                break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            raise RuntimeError(
                "CSVダウンロードボタンが見つかりませんでした（DOM変更の可能性）。"
                "config.py のセレクタ更新が必要かもしれません。"
            )

        downloaded = _wait_download(download_dir, before, config.DOWNLOAD_WAIT_TIMEOUT)

        # 投入先の命名規約にリネーム
        target = os.path.join(download_dir, config.csv_filename_for(date_str))
        if os.path.abspath(downloaded) != os.path.abspath(target):
            shutil.move(downloaded, target)
        logger.info("CSV取得完了: %s", target)
        return target
    finally:
        driver.quit()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD（既定: 今日JST）")
    parser.add_argument("--once", action="store_true", help="単発実行")
    args = parser.parse_args()

    from datetime import datetime

    date_str = args.date or datetime.now(config.TIMEZONE).strftime("%Y-%m-%d")
    path = download_csv(date_str)
    print(f"Downloaded: {path}")


if __name__ == "__main__":
    main()
