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


def _is_past_scheduled(scheduled_time: str, now: datetime) -> bool:
    """now が当日の scheduled_time("HH:MM") を過ぎているか。

    GitHub Actions の cron は遅延・間引きが激しく「±窓」では取りこぼすため、
    「今日まだ実行しておらず、予定時刻を過ぎていれば実行」する方式に変更。
    起動が何時間遅れても、その日の最初の起動で必ず実行される。
    """
    try:
        hh, mm = map(int, scheduled_time.split(":"))
    except ValueError:
        logger.warning("scheduledTime の形式が不正: %s", scheduled_time)
        return False
    scheduled_minutes = hh * 60 + mm
    now_minutes = now.hour * 60 + now.minute
    return now_minutes >= scheduled_minutes


def should_run(cfg: dict, now: datetime) -> tuple[bool, str]:
    """実行すべきか判定し、(実行可否, 理由) を返す。"""
    if not cfg.get("enabled", False):
        return False, "自動実行が無効(enabled=false)"

    if cfg.get("forceRun", False):
        return True, "手動トリガ(forceRun)"

    today = now.strftime("%Y-%m-%d")
    if cfg.get("lastRunDate") == today:
        return False, f"本日({today})は実行済み"

    scheduled = cfg.get("scheduledTime", "09:00")
    if not _is_past_scheduled(scheduled, now):
        return False, (
            f"予定時刻前 (now={now.strftime('%H:%M')}, scheduled={scheduled})"
        )

    # 今日未実行 かつ 予定時刻を過ぎている → 実行
    # (cron起動が遅延しても、その日の最初の起動で必ず拾える)
    return True, f"スケジュール到達 (予定={scheduled}, 実行={now.strftime('%H:%M')})"


def run_pipeline(date_str: str) -> tuple[int, str]:
    """取得→投入を実行し、(件数, 取得CSVパス) を返す。

    date_str: 対象日(YYYY-MM-DD)。CSV取得・天候登録ともこの日付を使う。
    """
    from airregi_scraper import download_csv
    from uploader import upload_csv

    logger.info("AirREGIからCSVを取得します: %s", date_str)
    csv_path = download_csv(date_str)

    logger.info("投入先へアップロードします: %s", csv_path)
    imported = upload_csv(csv_path, sales_date=date_str)
    return imported, csv_path


def _resolve_target_date(cfg: dict, today: str) -> str:
    """対象日を決定する。runDate(指定日)があればそれ、無ければ当日。"""
    run_date = (cfg.get("runDate") or "").strip()
    if run_date:
        return run_date
    return today


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

    target_date = _resolve_target_date(cfg, today)
    is_specified = target_date != today
    if is_specified:
        reason = f"{reason} / 指定日={target_date}"
    logger.info("実行します: %s (対象日=%s)", reason, target_date)

    started = time.time()
    stage = "airregi"
    try:
        imported, _ = run_pipeline(target_date)
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
        # 当日処理のときだけ lastRunDate を更新（指定日実行で当日扱いを汚さない）
        if not is_specified:
            fs.set_last_run_date(today)
        _consume_triggers(cfg)
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
        # forceRun/runDate での失敗時もフラグは下ろす（無限再試行を防ぐ）
        _consume_triggers(cfg)
        return 1


def _consume_triggers(cfg: dict) -> None:
    """手動トリガ(forceRun)と指定日(runDate)を消費して下ろす。"""
    updates = {}
    if cfg.get("forceRun"):
        updates["forceRun"] = False
    if (cfg.get("runDate") or "").strip():
        updates["runDate"] = ""
    if updates:
        fs.update_config(updates)


if __name__ == "__main__":
    raise SystemExit(main())
