"""
Selenium WebDriver セットアップ（scraper / uploader 共通）

headless Chrome を生成する。CSVダウンロード先ディレクトリを指定でき、
GitHub Actions の ubuntu-latest（Chrome同梱）でもローカルWSLでも動くようにする。
"""

import logging
import os

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

import config

logger = logging.getLogger(__name__)


def create_driver(download_dir: str | None = None) -> webdriver.Chrome:
    """headless Chrome ドライバを生成する。

    download_dir を指定すると、その場所へ自動ダウンロードするよう構成する。
    """
    opts = Options()

    if config.HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--lang=ja-JP")
    # bot検知をやや緩和
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    if download_dir:
        os.makedirs(download_dir, exist_ok=True)
        opts.add_experimental_option(
            "prefs",
            {
                "download.default_directory": download_dir,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
            },
        )

    # ドライバ解決:
    # 1) システムの chromedriver (GitHub Actions / apt)
    # 2) webdriver-manager で自動取得（ローカルWSL）
    driver = None
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as e:  # noqa: BLE001
        logger.warning("既定の chromedriver 解決に失敗、webdriver-manager を試行: %s", e)
        from webdriver_manager.chrome import ChromeDriverManager

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)

    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)

    # navigator.webdriver を隠す
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return driver
