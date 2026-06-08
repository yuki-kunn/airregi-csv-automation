"""
エントリポイント（GitHub Actions が cron 10分毎に呼ぶ）

フロー:
  1. automation/config を読む（enabled / scheduledTime / lastRunDate / forceRun）
  2. ゲート判定:
       - enabled でなければ skip
       - forceRun=true なら即実行（手動トリガ）
       - そうでなければ「現在時刻(JST)が scheduledTime ± window 内」かつ
         「lastRunDate != 今日」のときだけ実行
  3. AirREGI から当日CSVをダウンロード → 投入先へアップロード
  4. 結果を automation/logs に記録、lastRunDate を更新、forceRun を下ろす

無限ループ防止: 例外は握りつぶさず failed ログに残して終了。
同種エラーの連続再発（Cookie失効/CAPTCHA等）は admin画面のログで検知できる。
"""

import logging
import time
from datetime import datetime

import config
import firestore_client as fs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run")


def _now_jst() -> datetime:
    return datetime.now(config.TIMEZONE)


def _within_schedule(scheduled_time: str, now: datetime) -> bool:
    """now が scheduled_time("HH:MM") ± SCHEDULE_WINDOW_MINUTES に入るか。"""
    try:
        hh, mm = map(int, scheduled_time.split(":"))
    except ValueError:
        logger.warning("scheduledTime の形式が不正: %s", scheduled_time)
        return False
    scheduled_minutes = hh * 60 + mm
    now_minutes = now.hour * 60 + now.minute
    return abs(now_minutes - scheduled_minutes) < config.SCHEDULE_WINDOW_MINUTES


def should_run(cfg: dict, now: datetime) -> tuple[bool, str]:
    """実行すべきか判定し、(実行可否, 理由) を返す。"""
    if not cfg.get("enabled", False):
        return False, "自動実行が無効(enabled=false)"

    if cfg.get("forceRun", False):
        return True, "手動トリガ(forceRun)"

    today = now.strftime("%Y-%m-%d")
    if cfg.get("lastRunDate") == today:
        return False, f"本日({today})は実行済み"

    if not _within_schedule(cfg.get("scheduledTime", "09:00"), now):
        return False, (
            f"実行予定時刻外 (now={now.strftime('%H:%M')}, "
            f"scheduled={cfg.get('scheduledTime')})"
        )

    return True, "スケジュール一致"


def run_pipeline(date_str: str) -> tuple[int, str]:
    """取得→投入を実行し、(件数, 取得CSVパス) を返す。"""
    from airregi_scraper import download_csv
    from uploader import upload_csv

    logger.info("AirREGIからCSVを取得します: %s", date_str)
    csv_path = download_csv(date_str)

    logger.info("投入先へアップロードします: %s", csv_path)
    imported = upload_csv(csv_path)
    return imported, csv_path


def main() -> int:
    now = _now_jst()
    run_at_iso = now.isoformat()
    today = now.strftime("%Y-%m-%d")

    cfg = fs.get_config()
    do_run, reason = should_run(cfg, now)

    if not do_run:
        logger.info("スキップ: %s", reason)
        fs.add_log("skipped", message=reason, stage="done", run_at_iso=run_at_iso)
        return 0

    logger.info("実行します: %s", reason)
    started = time.time()
    stage = "airregi"
    try:
        imported, _ = run_pipeline(today)
        stage = "done"
        duration_ms = int((time.time() - started) * 1000)
        fs.add_log(
            "success",
            message=f"{reason} / {imported}件インポート",
            imported_count=imported,
            duration_ms=duration_ms,
            stage=stage,
            run_at_iso=run_at_iso,
        )
        fs.set_last_run_date(today)
        if cfg.get("forceRun"):
            fs.clear_force_run()
        logger.info("完了: %d件", imported)
        return 0
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.time() - started) * 1000)
        # stage を例外発生箇所で推定（uploadは upload_csv 内）
        msg = f"{type(e).__name__}: {e}"
        logger.error("失敗: %s", msg, exc_info=True)
        fs.add_log(
            "failed",
            message=msg,
            duration_ms=duration_ms,
            stage=stage,
            run_at_iso=run_at_iso,
        )
        # forceRun での失敗時もフラグは下ろす（無限再試行を防ぐ）
        if cfg.get("forceRun"):
            fs.clear_force_run()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
