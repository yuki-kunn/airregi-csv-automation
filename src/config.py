"""
設定・定数モジュール

環境変数とアプリケーション全体で使う定数を一元管理する。
SeleniumのセレクタやURLもここに集約し、対象サイトのDOM変更時の
修正範囲を局所化する（plan: リスク「投入先のDOM変更」対策）。
"""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# ===== タイムゾーン =====
# スケジュール判定はすべて日本時間(JST)で行う
TIMEZONE = ZoneInfo("Asia/Tokyo")

# ===== Firebase =====
# 既存 airregi-inventory と同じサービスアカウント(Base64)を共有する
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_ADMIN_PROJECT_ID", "ipo-kaidashi")
FIREBASE_SERVICE_ACCOUNT_KEY_B64 = os.environ.get("FIREBASE_ADMIN_SERVICE_ACCOUNT_KEY", "")

# Firestore コレクション/ドキュメント
AUTOMATION_COLLECTION = "automation"
CONFIG_DOC = "config"          # automation/config
LOGS_COLLECTION = "logs"        # automation/logs/{autoId}  (config と同階層のサブコレクション)

# config ドキュメントの既定値（未作成時に使用）
DEFAULT_CONFIG = {
    "enabled": True,
    "scheduledTime": "09:00",   # JST "HH:MM"
    "timezone": "Asia/Tokyo",
    "lastRunDate": "",          # "YYYY-MM-DD"
    "forceRun": False,          # admin画面からの手動トリガ
}

# scheduledTime と現在時刻の許容ずれ(分)。cronが10分毎なので window は cron間隔以上にする
SCHEDULE_WINDOW_MINUTES = int(os.environ.get("SCHEDULE_WINDOW_MINUTES", "10"))

# ===== AirREGI（取得元） =====
AIRREGI_SALES_URL = "https://airregi.jp/CLP/view/salesListByMenu/"
AIRREGI_LOGIN_HOST = "connect.airregi.jp"  # ここに飛ばされたら未ログイン判定
# 集計単位ラジオ（実DOM確認済み）:
#   <input type="radio" name="searchOrderBy" value="0" checked>  → 商品単位
#   <input type="radio" name="searchOrderBy" value="1">          → バリエーション単位
# バリエーション別CSVが欲しいので value="1" を選択してから再表示する。
AIRREGI_VARIATION_RADIO_CSS = 'input[name="searchOrderBy"][value="1"]'
# 「表示する」ボタン（再集計）
AIRREGI_SEARCH_BUTTON_CSS = "button.btn-search"
# CSVダウンロードボタン:
#   <button class="btn-CSV-DL ...">
#     <span class="download-text">商品単位の売上(CSV)をダウンロードする</span>
#   </button>
# ※ ボタン名は「商品単位」表記だが、表示単位に応じた内容が出力される
AIRREGI_CSV_BUTTON_CSS = "button.btn-CSV-DL"
# Cookie再利用方式: cookie_tool.py で取得したCookie(JSON文字列)
AIRREGI_COOKIES_JSON = os.environ.get("AIRREGI_COOKIES", "")
# 認証情報はコードに埋めず .env / Secrets から注入する（publicリポ前提）
AIRREGI_ID = os.environ.get("AIRREGI_ID", "")
AIRREGI_PASS = os.environ.get("AIRREGI_PASS", "")

# ===== 投入先（IPO在庫・売上管理システム） =====
UPLOAD_BASE_URL = "https://ipo-inventory-sales-management.vercel.app"
UPLOAD_AUTH_LOCALSTORAGE_KEY = "ipo_authenticated"  # auth.ts と一致
UPLOAD_AUTH_LOCALSTORAGE_VALUE = "true"
UPLOAD_PASSWORD = os.environ.get("IPO_UPLOAD_PASSWORD", "")

# ホームの hidden file input セレクタ（SalesUploader.svelte: <input type="file" accept=".csv">）
# 単一ファイル用 input（multiple/webkitdirectory が無い方）を狙う
UPLOAD_FILE_INPUT_CSS = 'input[type="file"][accept=".csv"]:not([multiple])'
# アップロード成功を示すテキスト（SalesUploader.svelte より）
UPLOAD_SUCCESS_TEXT = "件の売上データをインポートしました"

# ===== Selenium 全般 =====
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
DOWNLOAD_DIR = os.environ.get(
    "DOWNLOAD_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads"),
)
PAGE_LOAD_TIMEOUT = int(os.environ.get("PAGE_LOAD_TIMEOUT", "30"))
ELEMENT_WAIT_TIMEOUT = int(os.environ.get("ELEMENT_WAIT_TIMEOUT", "30"))
DOWNLOAD_WAIT_TIMEOUT = int(os.environ.get("DOWNLOAD_WAIT_TIMEOUT", "60"))


def csv_filename_for(date_str: str) -> str:
    """投入先のファイル名規約に整形する。

    例: バリエーション別売上_20260424-20260424.csv
    date_str: "YYYY-MM-DD"
    """
    compact = date_str.replace("-", "")
    return f"バリエーション別売上_{compact}-{compact}.csv"
