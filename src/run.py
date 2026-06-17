"""
エントリポイント（GitHub Actions が cron 10分毎に呼ぶ）

フロー:
  1. automation/config を読む（enabled / lastRunDate / forceRun）
  2. ゲート判定:
       - enabled でなければ skip
       - forceRun=true なら即実行（手動トリガ）
       - lastRunDate == today なら skip（当日すでに取得済み）
       - それ以外は即実行（当日中に初めて起動した時点で前日分を取得）
  3. 対象日 = 前日（runDate 指定がある場合はその日付）
  4. AirREGI から対象日CSVをダウンロード → 投入先へアップロード
  5. 結果を automation/logs に記録、lastRunDate(today)を更新、forceRun を下ろす

設計ポリシー:
  GitHub Actions 無料枠の cron は数時間単位の遅延・間引きが発生する。
  「scheduledTime を過ぎたら実行」では深夜しか起動しない日に取りこぼす。
  そのため時刻チェックを廃止し「当日まだ動いていなければ即実行」とする。
  取得対象は「前日」なので、当日0:00〜23:59のいつ起動しても正しいデータが得られる。

無限ループ防止: 例外は握りつぶさず failed ログに残して終了。
同種エラーの連続再発（Cookie失効/CAPTCHA等）は admin画面のログで検知できる。
"""

import logging
import time
from datetime import datetime, timedelta

import config
import firestore_client as fs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run")


def _now_jst() -> datetime:
    return datetime.now(config.TIMEZONE)


def should_run(cfg: dict, now: datetime) -> tuple[bool, str]:
    """実行すべきか判定し、(実行可否, 理由) を返す。"""
    if not cfg.get("enabled", False):
        return False, "自動実行が無効(enabled=false)"

    if cfg.get("forceRun", False):
        return True, "手動トリガ(forceRun)"

    today = now.strftime("%Y-%m-%d")
    if cfg.get("lastRunDate") == today:
        return False, f"本日({today})は実行済み"

    # 時刻チェックなし: 当日中に初めて起動した時点で前日分を取得する
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return True, f"当日初回起動 (実行={now.strftime('%H:%M')}, 対象日={yesterday})"


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


def _resolve_target_date(cfg: dict, now: datetime) -> tuple[str, bool]:
    """対象日と「指定日かどうか」を返す。

    runDate(指定日)があればその日、無ければ前日。
    戻り値: (対象日 YYYY-MM-DD, is_specified)
    """
    run_date = (cfg.get("runDate") or "").strip()
    if run_date:
        return run_date, True
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return yesterday, False


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

    target_date, is_specified = _resolve_target_date(cfg, now)
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
        # 指定日実行の場合も lastRunDate を today で更新する
        # （当日の自動取得は済んだとみなし、二重実行を防ぐ）
        fs.set_last_run_date(today)
        _consume_triggers(cfg)
        logger.info("完了: %d件", imported)
        return 0
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.time() - started) * 1000)
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
