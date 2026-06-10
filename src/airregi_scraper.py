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
from datetime import datetime

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

        wait = WebDriverWait(driver, config.ELEMENT_WAIT_TIMEOUT)

        # 対象日を datepicker に設定（バリエーション切替前に行い、
        # _select_variation_unit 内の「表示する」でまとめて再集計させる）
        date_ok = _set_date_range(driver, wait, date_str)

        # 安全策: 日付が対象日に設定できなかった場合、当日データを誤った日付で
        # 保存する事故を防ぐため中断する（無限ループ防止のため即失敗）。
        if not date_ok:
            _dump_page_diagnostics(driver)
            raise RuntimeError(
                f"対象日({date_str})をAirREGIの日付欄に設定できませんでした。"
                "当日データを誤登録しないよう中断します。"
            )

        # 集計単位を「バリエーション単位」に切り替えてから再表示する。
        # （既定は商品単位。投入先はバリエーション別CSVを期待）
        _select_variation_unit(driver, wait)

        # CSVダウンロードボタンを探してクリック。
        # AirREGIの売上ページのDLリンクは表記揺れがあるため複数候補で探索。
        candidates = [
            # 実DOM確認済みの確定セレクタ（最優先）
            (By.CSS_SELECTOR, config.AIRREGI_CSV_BUTTON_CSS),
            (By.XPATH, "//button[contains(.,'CSV') and contains(.,'ダウンロード')]"),
            (By.XPATH, "//*[contains(@class,'btn-CSV-DL')]"),
            # フォールバック（DOM変更時の保険）
            (By.XPATH, "//button[contains(.,'CSV')]"),
            (By.XPATH, "//a[contains(.,'CSV')]"),
            (By.XPATH, "//button[contains(.,'ダウンロード')]"),
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
            _dump_page_diagnostics(driver)
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


def _set_date_range(driver, wait, date_str: str) -> bool:
    """datepicker に対象日(YYYY-MM-DD)を単日範囲として設定する。

    AirREGIの日付入力は readonly のカスタムウィジェット。value書換では
    内部状態に反映されないため、カレンダーを開いて日付セルをクリックする。
    単日選択なので同じ日を2回クリック（開始=終了）。

    返り値: input value が対象日になったら True（設定成功）。
    """
    slash = date_str.replace("-", "/")
    range_value = f"{slash} ~ {slash}"
    target_dt = datetime.strptime(date_str, "%Y-%m-%d")
    target_day = target_dt.day
    target_ym = f"{target_dt.year}年{target_dt.month}月"

    try:
        date_input = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, config.AIRREGI_DATE_INPUT_CSS)
            )
        )
        current = (date_input.get_attribute("value") or "").strip()
        if current == range_value:
            logger.info("日付は既に対象日: %s", range_value)
            return True

        # カレンダーを開く（input か icon をクリック）
        try:
            driver.execute_script("arguments[0].click();", date_input)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
        icon = driver.find_elements(By.CSS_SELECTOR, "div.input-date .regicon")
        if icon:
            try:
                driver.execute_script("arguments[0].click();", icon[0])
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.5)

        # カレンダーDOMを診断出力（構造特定用）
        _dump_calendar_diagnostics(driver, target_ym, target_day)

        # 目的の年月までナビゲートしつつ日付セルをクリック
        clicked = _click_calendar_day(driver, target_dt)
        if not clicked:
            logger.warning(
                "カレンダーで対象日セルをクリックできませんでした: %s", date_str
            )
            return False

        time.sleep(1)
        # 単日選択: 終了日も同じ日をクリック（範囲ピッカーの場合）
        _click_calendar_day(driver, target_dt)
        time.sleep(1)

        # 反映確認: input value が対象日(単日範囲)になっているか
        new_val = (date_input.get_attribute("value") or "").strip()
        logger.info("日付クリック後の input value: %r (期待=%r)", new_val, range_value)
        # 開始・終了とも対象日を含むか（範囲表記/単日表記どちらも許容）
        return slash in new_val
    except Exception as e:  # noqa: BLE001
        logger.warning("日付設定に失敗: %s", e)
        return False


def _dump_calendar_diagnostics(driver, target_ym: str, target_day: int) -> None:
    """カレンダーポップアップのDOM構造を診断ログに出す。"""
    try:
        # よくあるカレンダーのコンテナ候補を広く探す
        for sel in (
            "[class*='calendar']",
            "[class*='datepicker']",
            "[class*='Calendar']",
            "[role='dialog']",
        ):
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els[:2]:
                html = (el.get_attribute("outerHTML") or "")[:600]
                logger.info("=== 診断[cal %s]: %s", sel, html.replace("\n", " "))
        # 表示中の年月ラベルらしき要素
        heads = driver.find_elements(
            By.XPATH, "//*[contains(text(),'年') and contains(text(),'月')]"
        )
        for h in heads[:5]:
            logger.info("   年月ラベル候補: %r", (h.text or "").strip()[:20])
        # クリック可能な日付セル候補
        cells = driver.find_elements(
            By.XPATH,
            f"//td[normalize-space(text())='{target_day}'] | "
            f"//*[contains(@class,'day') and normalize-space(text())='{target_day}']",
        )
        logger.info("=== 診断: 日付セル候補(%d) = %d個", target_day, len(cells))
    except Exception as e:  # noqa: BLE001
        logger.warning("カレンダー診断に失敗: %s", e)


def _click_calendar_day(driver, target_dt) -> bool:
    """カレンダー上で対象日のセルをクリックする。年月ナビゲーション込み。

    実DOM（AirREGIの datepicker-container）:
      <table class="dates-table">
        <tr class="movement"><td>«</td><td class="switch">2026年06月</td><td>»</td></tr>
        ...
        <tr class="week"><td class="old"><div>31</div></td><td><div>1</div></td>...
    - 当月セルは class に 'old'(前月)/'new'(翌月) を含まない <td><div>N</div></td>
    - 月ラベルは td.switch、前月は « 、翌月は »
    """
    target_day = str(target_dt.day)
    target_label = f"{target_dt.year}年{target_dt.month:02d}月"

    for _ in range(14):
        # 現在表示中の年月ラベル
        labels = driver.find_elements(By.CSS_SELECTOR, "td.switch")
        current_label = labels[0].text.strip() if labels else ""

        if current_label == target_label:
            # 当月の対象日セル（old/new を除外、div内テキスト一致）
            cells = driver.find_elements(
                By.XPATH,
                "//table[contains(@class,'dates-table')]"
                "//td[not(contains(@class,'old')) and not(contains(@class,'new')) "
                "and not(contains(@class,'disabled'))]"
                f"/div[normalize-space(text())='{target_day}']",
            )
            logger.info(
                "対象月%s 日付セル(%s)候補: %d個", target_label, target_day, len(cells)
            )
            if cells:
                # 親td をクリック対象にする（divよりtdの方がハンドラを持つことが多い）
                td = cells[0].find_element(By.XPATH, "./..")
                for target_el in (td, cells[0]):
                    try:
                        target_el.click()  # 実クリック
                        logger.info("日付セルを実クリックしました")
                        return True
                    except Exception as e1:  # noqa: BLE001
                        logger.info("実クリック失敗(%s)、JSクリック試行", e1)
                        try:
                            driver.execute_script("arguments[0].click();", target_el)
                            logger.info("日付セルをJSクリックしました")
                            return True
                        except Exception:  # noqa: BLE001
                            continue
                return False
            return False

        # 目的月へ移動。target が現在より過去なら « 、未来なら »
        go_prev = target_label < current_label if current_label else True
        arrow = "«" if go_prev else "»"
        nav = driver.find_elements(
            By.XPATH,
            f"//tr[contains(@class,'movement')]/td[normalize-space(text())='{arrow}']",
        )
        if not nav:
            break
        try:
            driver.execute_script("arguments[0].click();", nav[0])
            time.sleep(0.6)
        except Exception:  # noqa: BLE001
            break
    return False


def _select_variation_unit(driver, wait) -> None:
    """集計単位を「バリエーション単位」(searchOrderBy=1)に切り替えて再表示する。

    既定は商品単位(value=0)。ラジオを選び「表示する」を押して再集計させる。
    要素が無い/変更済みでも致命的でないため、失敗は警告に留める。
    """
    import time

    try:
        radio = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, config.AIRREGI_VARIATION_RADIO_CSS)
            )
        )
        if radio.is_selected():
            logger.info("既にバリエーション単位が選択済み")
        else:
            # ラベルが重なってクリックを奪うことがあるためJSで確実に選択
            driver.execute_script("arguments[0].click();", radio)
            logger.info("バリエーション単位ラジオを選択")

        # 「表示する」で再集計
        try:
            search_btn = driver.find_element(
                By.CSS_SELECTOR, config.AIRREGI_SEARCH_BUTTON_CSS
            )
            driver.execute_script("arguments[0].click();", search_btn)
            logger.info("「表示する」をクリックして再集計")
            # 再集計の描画を待つ（テーブル再構築の猶予）
            time.sleep(3)
            WebDriverWait(driver, config.PAGE_LOAD_TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("「表示する」クリックに失敗（続行）: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "バリエーション単位の切替に失敗（商品単位のまま続行の可能性）: %s", e
        )


def _dump_page_diagnostics(driver) -> None:
    """CSVボタンが見つからない時、ページ構造をログ＆ファイルに出力する。

    GitHub Actions では downloads/ をアーティファクトとして回収できる。
    実際のDOMを見てセレクタを修正するための診断用。
    """
    try:
        out_dir = config.DOWNLOAD_DIR
        os.makedirs(out_dir, exist_ok=True)

        logger.info("=== 診断: 現在URL = %s ===", driver.current_url)
        logger.info("=== 診断: title = %s ===", driver.title)

        # HTMLとスクリーンショットを保存
        with open(os.path.join(out_dir, "debug_page.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        try:
            driver.save_screenshot(os.path.join(out_dir, "debug_page.png"))
        except Exception:  # noqa: BLE001
            pass

        # iframe があるかもしれない（AirREGIは iframe を使う画面がある）
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        logger.info("=== 診断: iframe数 = %d ===", len(iframes))

        # 全ボタン・リンクのテキストを列挙
        from selenium.webdriver.common.by import By as _By

        for tag in ("a", "button"):
            els = driver.find_elements(_By.TAG_NAME, tag)
            logger.info("=== 診断: <%s> %d個 ===", tag, len(els))
            for el in els[:40]:
                try:
                    txt = (el.text or "").strip().replace("\n", " ")
                    href = el.get_attribute("href") or ""
                    cls = el.get_attribute("class") or ""
                    onclick = el.get_attribute("onclick") or ""
                    if txt or href or onclick:
                        logger.info(
                            "   <%s> text=%r href=%r class=%r onclick=%r",
                            tag,
                            txt[:40],
                            href[:80],
                            cls[:50],
                            onclick[:60],
                        )
                except Exception:  # noqa: BLE001
                    continue
        # CSV/ダウンロードを含む要素を広く探す
        kw = driver.find_elements(
            _By.XPATH,
            "//*[contains(text(),'CSV') or contains(text(),'ダウンロード') "
            "or contains(text(),'出力') or contains(text(),'エクスポート')]",
        )
        logger.info("=== 診断: CSV/DL/出力 を含む要素 = %d個 ===", len(kw))
        for el in kw[:20]:
            try:
                logger.info(
                    "   %s text=%r class=%r",
                    el.tag_name,
                    (el.text or "").strip()[:40],
                    (el.get_attribute("class") or "")[:50],
                )
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("診断ダンプに失敗: %s", e)


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
