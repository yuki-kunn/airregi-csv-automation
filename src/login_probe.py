"""
ログインページ調査スクリプト（一時・診断用）

Cookie無しで AirREGI 売上ページを開く → ログインページにリダイレクトされる。
そのログインフォームの全要素（input/button/隠し要素/honeypot候補）をダンプし、
HTMLとスクリーンショットを downloads/ に保存する。

直接ログイン方式を安全に実装するため、本物の入力欄とダミー要素を見分けるのが目的。
ダミーボタンを誤操作しないよう、実装前に必ず実態を把握する。
"""

import logging
import os

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config
from browser import create_driver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("login_probe")


def _describe_element(driver, el) -> dict:
    """要素の可視性・属性を取得する（honeypot判定用）。"""
    try:
        info = driver.execute_script(
            """
            const el = arguments[0];
            const cs = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return {
              tag: el.tagName,
              type: el.getAttribute('type'),
              name: el.getAttribute('name'),
              id: el.id,
              cls: el.className,
              placeholder: el.getAttribute('placeholder'),
              autocomplete: el.getAttribute('autocomplete'),
              ariaHidden: el.getAttribute('aria-hidden'),
              tabindex: el.getAttribute('tabindex'),
              display: cs.display,
              visibility: cs.visibility,
              opacity: cs.opacity,
              width: rect.width,
              height: rect.height,
              offTop: rect.top,
              offLeft: rect.left,
              text: (el.innerText || el.value || '').slice(0, 40)
            };
            """,
            el,
        )
        # 可視判定: display:none/visibility:hidden/opacity:0/サイズ0/画面外 は怪しい
        hidden = (
            info["display"] == "none"
            or info["visibility"] == "hidden"
            or str(info["opacity"]) == "0"
            or (info["width"] == 0 and info["height"] == 0)
            or info["offLeft"] < -1000
            or info["offTop"] < -1000
        )
        info["_suspicious"] = hidden
        return info
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def main():
    driver = create_driver(download_dir=config.DOWNLOAD_DIR)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    try:
        # Cookie無しで売上ページ → ログインページへリダイレクト
        driver.get(config.AIRREGI_SALES_URL)
        WebDriverWait(driver, config.PAGE_LOAD_TIMEOUT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        logger.info("=== 到達URL: %s", driver.current_url)
        logger.info("=== title: %s", driver.title)

        # HTML・スクショ保存
        with open(
            os.path.join(config.DOWNLOAD_DIR, "login_page.html"), "w", encoding="utf-8"
        ) as f:
            f.write(driver.page_source)
        driver.save_screenshot(os.path.join(config.DOWNLOAD_DIR, "login_page.png"))
        logger.info("login_page.html / .png を保存")

        # フォーム
        forms = driver.find_elements(By.TAG_NAME, "form")
        logger.info("=== <form> 数: %d", len(forms))
        for i, fm in enumerate(forms):
            logger.info(
                "   form[%d] action=%r method=%r id=%r",
                i,
                fm.get_attribute("action"),
                fm.get_attribute("method"),
                fm.get_attribute("id"),
            )

        # 全 input
        inputs = driver.find_elements(By.TAG_NAME, "input")
        logger.info("=== <input> 数: %d", len(inputs))
        for el in inputs:
            d = _describe_element(driver, el)
            mark = "⚠HONEYPOT?" if d.get("_suspicious") else "OK"
            logger.info(
                "   [%s] type=%s name=%s id=%s autocomplete=%s ph=%r "
                "disp=%s vis=%s opa=%s size=%sx%s",
                mark,
                d.get("type"),
                d.get("name"),
                d.get("id"),
                d.get("autocomplete"),
                d.get("placeholder"),
                d.get("display"),
                d.get("visibility"),
                d.get("opacity"),
                d.get("width"),
                d.get("height"),
            )

        # 全 button と submit
        btns = driver.find_elements(
            By.XPATH, "//button | //input[@type='submit'] | //a[@role='button']"
        )
        logger.info("=== button/submit 数: %d", len(btns))
        for el in btns:
            d = _describe_element(driver, el)
            mark = "⚠DECOY?" if d.get("_suspicious") else "OK"
            logger.info(
                "   [%s] tag=%s type=%s name=%s id=%s text=%r disp=%s size=%sx%s",
                mark,
                d.get("tag"),
                d.get("type"),
                d.get("name"),
                d.get("id"),
                d.get("text"),
                d.get("display"),
                d.get("width"),
                d.get("height"),
            )
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
