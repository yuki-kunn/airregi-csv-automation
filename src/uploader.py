"""
投入先アップローダ

IPO在庫・売上管理システム（SvelteKit / クライアント処理）へCSVを投入する。
auth.ts のとおり localStorage に ipo_authenticated=true を注入してログイン画面を
スキップし、SalesUploader.svelte の hidden file input に send_keys でCSVを渡す。
処理完了は成功テキストの出現で検証する。
"""

import argparse
import logging
import os
import re

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config
from browser import create_driver

logger = logging.getLogger(__name__)


class UploadError(RuntimeError):
    """アップロードが成功しなかった場合に送出。"""


def _inject_auth(driver) -> None:
    """localStorage に認証フラグを注入し、ログインをスキップする。"""
    # localStorage はオリジンを開いてからでないと書けない
    driver.get(config.UPLOAD_BASE_URL + "/login")
    driver.execute_script(
        "window.localStorage.setItem(arguments[0], arguments[1]);",
        config.UPLOAD_AUTH_LOCALSTORAGE_KEY,
        config.UPLOAD_AUTH_LOCALSTORAGE_VALUE,
    )
    logger.info("localStorage認証フラグを注入しました")


def upload_csv(csv_path: str, sales_date: str | None = None) -> int:
    """CSVをアップロードし、インポート件数を返す。

    sales_date(YYYY-MM-DD)を渡すと、アップロード後にその日の天候を取得して
    dailySalesに登録する（未指定ならファイル名から抽出を試みる）。
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)

    if not sales_date:
        sales_date = _date_from_filename(csv_path)

    driver = create_driver()
    try:
        _inject_auth(driver)

        # ホームへ遷移（認証済みなのでリダイレクトされない）
        driver.get(config.UPLOAD_BASE_URL + "/")
        wait = WebDriverWait(driver, config.ELEMENT_WAIT_TIMEOUT)

        # ログイン画面に飛ばされていないか確認
        if "/login" in driver.current_url:
            raise UploadError("認証注入後もログイン画面に留まりました")

        # 「売上取込」トグルを押さないと file input が描画されない
        # (+page.svelte: {#if showUploader} <SalesUploader />)
        toggle = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(.,'売上取込')]")
            )
        )
        driver.execute_script("arguments[0].click();", toggle)
        logger.info("「売上取込」トグルをクリックしました")

        # hidden file input を取得（存在すれば良い。可視性は問わない）
        file_input = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, config.UPLOAD_FILE_INPUT_CSS)
            )
        )
        # send_keys は絶対パスが必要
        file_input.send_keys(os.path.abspath(csv_path))
        logger.info("ファイルを input に送信: %s", csv_path)

        # 成功テキストの出現を待つ（処理は in-browser で時間がかかる）
        success_locator = (
            By.XPATH,
            f"//*[contains(text(),'{config.UPLOAD_SUCCESS_TEXT}')]",
        )
        try:
            el = WebDriverWait(driver, config.DOWNLOAD_WAIT_TIMEOUT).until(
                EC.presence_of_element_located(success_locator)
            )
        except Exception as e:  # noqa: BLE001
            raise UploadError(
                "アップロード成功の確認テキストが現れませんでした"
                "（処理失敗かDOM変更の可能性）"
            ) from e

        # "N件の売上データをインポートしました" から件数を抽出
        text = el.text
        m = re.search(r"(\d+)\s*件", text)
        imported = int(m.group(1)) if m else 0
        logger.info("アップロード成功: %s (%d件)", text, imported)

        # 在庫を確実に反映させるため、その日付の再計算を実行する。
        # （アップロード時はレシピ未ロードで在庫減算が空振りすることがあるため）
        if sales_date:
            _reprocess_inventory(driver, sales_date)

        # 天候を取得して dailySales に登録（同じブラウザのfetchでサイト内APIを叩く）
        if sales_date:
            _register_weather(driver, sales_date)

        return imported
    finally:
        driver.quit()


def _reprocess_inventory(driver, sales_date: str) -> None:
    """その日付のカレンダー詳細ページで「再計算」を実行し、在庫を反映させる。

    手動の「再計算」ボタンと同じ処理（processSalesData→markAsProcessed）を走らせる。
    既に反映済み(inventoryProcessed=true & 未登録0)ならボタンが無く、何もしない。
    在庫は付随処理のため、失敗してもアップロード自体は成功扱い（警告のみ）。
    """
    import time

    try:
        # レシピは Notion API(/api/notion/recipes)から非同期ロードされる。
        # ロード前に再計算すると全商品が「未登録」扱いになり在庫が減らないため、
        # 先にレシピAPIを叩いてロード完了＆件数>0 を確認してからページを開く。
        recipe_count = driver.execute_async_script(
            """
            const cb = arguments[arguments.length - 1];
            fetch('/api/notion/recipes')
              .then(r => r.json())
              .then(d => cb((d.recipes || d || []).length))
              .catch(() => cb(-1));
            """
        )
        logger.info("レシピ件数: %s", recipe_count)
        if not recipe_count or recipe_count <= 0:
            logger.warning(
                "レシピが取得できないため在庫再計算をスキップ（在庫未反映）: %s",
                sales_date,
            )
            return

        driver.get(f"{config.UPLOAD_BASE_URL}/calendar/{sales_date}")
        wait = WebDriverWait(driver, config.ELEMENT_WAIT_TIMEOUT)
        wait.until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        # レシピ store がページ内でロードされるのを十分待つ（Notion API経由で遅い）
        time.sleep(8)

        # confirm/alert ダイアログを自動承認
        driver.execute_script("window.confirm = function(){return true;};")
        driver.execute_script("window.alert = function(){};")

        # 「再計算」ボタンを探す（反映済みなら存在しない）
        btns = driver.find_elements(
            By.XPATH, "//button[contains(normalize-space(.),'再計算')]"
        )
        if not btns:
            logger.info("再計算ボタンなし（既に在庫反映済み）: %s", sales_date)
            return

        driver.execute_script("arguments[0].click();", btns[0])
        logger.info("在庫再計算を実行しました: %s", sales_date)
        # 再計算（processSalesData→markAsProcessed）の完了を待つ
        time.sleep(6)
    except Exception as e:  # noqa: BLE001
        logger.warning("在庫再計算に失敗（続行）: %s", e)


def _date_from_filename(csv_path: str) -> str | None:
    """ファイル名から日付(YYYY-MM-DD)を抽出する。
    例: バリエーション別売上_20260610-20260610.csv → 2026-06-10
    """
    m = re.search(r"(\d{4})(\d{2})(\d{2})", os.path.basename(csv_path))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _register_weather(driver, sales_date: str) -> None:
    """対象日の天候を /api/weather で取得し、dailySales に登録する。

    投入先サイト内で実行する fetch なので CSRF/認証(Origin)は自然に通る。
    天候は付加情報のため、失敗しても例外にせず警告に留める。
    その日の dailySales が存在しない（0件）場合はスキップ。
    """
    script = """
    const cb = arguments[arguments.length - 1];
    const date = arguments[0];
    const location = arguments[1];
    (async () => {
      try {
        // 1. 天候を取得
        const wRes = await fetch(`/api/weather?date=${date}&location=${encodeURIComponent(location)}`);
        if (!wRes.ok) { cb({ok:false, step:'weather', status:wRes.status}); return; }
        const w = await wRes.json();
        if (!w || !w.weather) { cb({ok:false, step:'weather-empty'}); return; }

        // 2. その日の dailySales を取得（無ければ登録対象なしでスキップ）
        const dRes = await fetch(`/api/firestore/dailySales?date=${date}`);
        if (!dRes.ok) { cb({ok:false, step:'dailySales-get', status:dRes.status}); return; }
        const dData = await dRes.json();
        const ds = dData.dailySale;
        if (!ds) { cb({ok:false, step:'no-dailySales', weather:w.weather}); return; }

        // 3. 天候を付与して addOrUpdate（上書き）
        const pRes = await fetch('/api/firestore/dailySales', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            action: 'addOrUpdate',
            date: date,
            salesData: ds.salesData || ds.sales || [],
            unregisteredCount: ds.unregisteredCount || 0,
            customerInfo: ds.customerInfo,
            weather: w.weather
          })
        });
        cb({ok: pRes.ok, step:'done', weather:w.weather, desc:w.description});
      } catch (e) {
        cb({ok:false, step:'exception', error:String(e)});
      }
    })();
    """
    try:
        # 非同期スクリプトのタイムアウトを設定
        driver.set_script_timeout(30)
        result = driver.execute_async_script(
            script, sales_date, config.WEATHER_LOCATION
        )
        if result and result.get("ok"):
            logger.info(
                "天候を登録しました: %s (%s) %s",
                sales_date,
                result.get("weather"),
                result.get("desc", ""),
            )
        else:
            logger.warning(
                "天候登録をスキップ/失敗: %s detail=%s", sales_date, result
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("天候登録でエラー（続行）: %s", e)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="アップロードするCSVパス")
    parser.add_argument("--date", help="売上日 YYYY-MM-DD（未指定はファイル名から）")
    args = parser.parse_args()

    count = upload_csv(args.file, sales_date=args.date)
    print(f"Imported: {count}")


if __name__ == "__main__":
    main()
