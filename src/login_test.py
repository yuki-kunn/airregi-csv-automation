"""
直接ログイン単体テスト（Firestore不使用）

AIRREGI_ID / AIRREGI_PASS で直接ログインを試行し、売上ページに到達できるか確認する。
クォータ超過中でもテスト可能（Firestoreを一切使わない）。
結果のスクリーンショットを downloads/ に保存する。
"""

import logging
import os

import config
from airregi_scraper import (
    CaptchaRequiredError,
    LoginExpiredError,
    _login_with_credentials,
)
from browser import create_driver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("login_test")


def main() -> int:
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    driver = create_driver(download_dir=config.DOWNLOAD_DIR)
    try:
        logger.info("ID設定: %s / PASS設定: %s", bool(config.AIRREGI_ID), bool(config.AIRREGI_PASS))
        try:
            _login_with_credentials(driver)
            logger.info("✅ 直接ログイン成功: %s", driver.current_url)
            driver.save_screenshot(os.path.join(config.DOWNLOAD_DIR, "login_test_ok.png"))
            return 0
        except CaptchaRequiredError as e:
            logger.error("⚠ CAPTCHA要求で中断: %s", e)
            driver.save_screenshot(
                os.path.join(config.DOWNLOAD_DIR, "login_test_captcha.png")
            )
            return 2
        except LoginExpiredError as e:
            logger.error("❌ ログイン失敗: %s", e)
            driver.save_screenshot(
                os.path.join(config.DOWNLOAD_DIR, "login_test_fail.png")
            )
            return 1
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
