"""
Firestore クライアント

既存 airregi-inventory の firebase-admin.ts と同じサービスアカウントを使い、
`automation` コレクションで admin画面と設定・ログを共有する。

- config の読取/更新（scheduledTime, enabled, lastRunDate, forceRun）
- 実行ログの書込（automation/logs サブコレクション）
"""

import base64
import json
import logging

import firebase_admin
from firebase_admin import credentials, firestore

import config

logger = logging.getLogger(__name__)

_db = None


def _init_app():
    """firebase-admin を初期化（多重初期化を防止）。"""
    global _db
    if _db is not None:
        return _db

    if not firebase_admin._apps:
        if config.FIREBASE_SERVICE_ACCOUNT_KEY_B64:
            decoded = base64.b64decode(config.FIREBASE_SERVICE_ACCOUNT_KEY_B64).decode("utf-8")
            cred = credentials.Certificate(json.loads(decoded))
            firebase_admin.initialize_app(cred)
        else:
            # ローカルで gcloud ADC 等を使う場合のフォールバック
            firebase_admin.initialize_app(
                options={"projectId": config.FIREBASE_PROJECT_ID}
            )

    _db = firestore.client()
    return _db


def _config_ref():
    db = _init_app()
    return db.collection(config.AUTOMATION_COLLECTION).document(config.CONFIG_DOC)


def get_config() -> dict:
    """automation/config を取得。無ければ既定値で作成して返す。"""
    ref = _config_ref()
    snap = ref.get()
    if not snap.exists:
        logger.info("config ドキュメントが無いため既定値で作成します")
        ref.set(config.DEFAULT_CONFIG)
        return dict(config.DEFAULT_CONFIG)

    data = snap.to_dict() or {}
    # 欠損キーは既定値で補完
    merged = dict(config.DEFAULT_CONFIG)
    merged.update(data)
    return merged


def update_config(fields: dict) -> None:
    """config の一部フィールドを更新する。"""
    _config_ref().set(fields, merge=True)


def set_last_run_date(date_str: str) -> None:
    """当日実行済みとして lastRunDate を更新（二重投入防止）。"""
    update_config({"lastRunDate": date_str})


def clear_force_run() -> None:
    """手動トリガ(forceRun)を消費したらフラグを下ろす。"""
    update_config({"forceRun": False})


def add_log(
    status: str,
    *,
    message: str = "",
    imported_count: int = 0,
    duration_ms: int = 0,
    stage: str = "done",
    run_at_iso: str = "",
) -> None:
    """実行ログを automation/logs に追記する。

    status: "success" | "failed" | "skipped"
    stage:  "airregi" | "upload" | "done"
    run_at_iso: 呼び出し側で生成した ISO8601(JST) 文字列
    """
    db = _init_app()
    logs_ref = (
        db.collection(config.AUTOMATION_COLLECTION)
        .document(config.CONFIG_DOC)
        .collection(config.LOGS_COLLECTION)
    )
    logs_ref.add(
        {
            "runAt": run_at_iso,
            "status": status,
            "message": message,
            "importedCount": imported_count,
            "durationMs": duration_ms,
            "stage": stage,
            # サーバー側タイムスタンプ（並び替え・保険用）
            "createdAt": firestore.SERVER_TIMESTAMP,
        }
    )
    logger.info("ログ記録: status=%s stage=%s msg=%s", status, stage, message)
