# AirREGI CSV 自動取込・アップロードシステム

> 毎日決まった時刻に、AirREGIの売上CSVを自動ダウンロードし、
> IPO在庫・売上管理システムへ自動アップロードする無人システム。

headless Selenium + GitHub Actions（無料）で動作。実行時刻の変更や稼働状況の確認は
[IPO在庫・売上管理システム](https://ipo-inventory-sales-management.vercel.app/)の
**「自動化管理」(/admin)** 画面から行えます。

---

## 🏗️ アーキテクチャ

```
GitHub Actions (cron 10分毎)
  └─ src/run.py
       1. Firestore automation/config を読む（実行時刻・有効・手動トリガ）
       2. ゲート判定（今が実行時刻 & 今日未実行 のときだけ実行）
       3. AirREGI: 保存Cookieでログイン復元 → 当日CSVをダウンロード
       4. 投入先: localStorage認証注入 → 「売上取込」→ file inputへ送信 → 完了待ち
       5. 結果を Firestore automation/logs に記録、lastRunDate更新
                   ▲ 設定・ログを共有
IPO在庫・売上管理システム (Vercel)
  └─ /admin  …… 時刻変更 / 有効トグル / 今すぐ実行 / 実行ログ閲覧
```

**なぜ「10分毎 + UIゲート」か**: GitHub Actionsのcronは静的YAMLでUIから変更できない。
そこでYAMLは10分間隔で回し、実際に実行するかは毎回 Firestore の `scheduledTime` と
現在時刻(JST)を比較して決める。これで「UIから時刻変更」と無料・無人を両立する。

---

## 📁 構成

```
airregi-csv-automation/
├── .github/workflows/automation.yml  # cron 10分毎 + 手動実行
├── src/
│   ├── run.py             # エントリポイント（ゲート→実行→ログ）
│   ├── config.py          # 環境変数・URL・セレクタを集約
│   ├── firestore_client.py# config読取/logs書込/lastRunDate更新
│   ├── browser.py         # headless Chrome 生成（共通）
│   ├── airregi_scraper.py # Cookie復元→CSVダウンロード
│   ├── uploader.py        # localStorage注入→アップロード
│   └── cookie_tool.py     # 【初回手動】Cookie取得
├── requirements.txt
├── .env.example
└── docs/TROUBLESHOOTING.md
```

---

## 🚀 セットアップ手順

### 1. 依存関係

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 値を埋める
```

### 2. Firebase サービスアカウント

既存の `airregi-inventory` と同じサービスアカウント（プロジェクト `ipo-kaidashi`）を使う。
サービスアカウントJSONをBase64化して `.env` / GitHub Secrets に設定:

```bash
cat service-account.json | base64 -w0
# → FIREBASE_ADMIN_SERVICE_ACCOUNT_KEY に設定
```

### 3. AirREGI ログイン方式

**直接ログイン方式（推奨・既定）**: ID/PASSから毎回自動ログインする。Cookie失効の手間が無い。
`LOGIN_MODE=auto` で、直接ログイン → 失敗時のみCookie方式にフォールバック。

ログインフォームには **honeypot（ダミー入力欄 dummy01-04）** が仕込まれているが、
本物の `#account`/`#password` のみを id 厳密指定で操作するため安全。
複数店舗アカウントの場合はログイン後に店舗選択（`AIRREGI_STORE_NAME`、既定 `CANVAS COFFEE`）。

> CAPTCHA/画像認証が要求された場合は検知して中断・`failed`ログを残す（手動対応）。

**Cookie方式（フォールバック）**: 直接ログインが使えない場合、admin画面 `/admin` の
「AirREGI ログインCookie」からDevToolsでコピーしたCookieを登録（DevTools表/JSON/Netscape対応）。

### 4. GitHub Secrets

リポジトリの Settings → Secrets and variables → Actions に登録:

| Secret | 内容 |
|---|---|
| `FIREBASE_ADMIN_SERVICE_ACCOUNT_KEY` | サービスアカウントJSON(Base64) |
| `FIREBASE_ADMIN_PROJECT_ID` | `ipo-kaidashi` |
| `IPO_UPLOAD_PASSWORD` | 投入先のログインパスワード |
| `AIRREGI_ID` | AirREGIのAirID（例 `cooon0201-cafe`） |
| `AIRREGI_PASS` | AirREGIのパスワード |

> `AIRREGI_COOKIES` は Cookie フォールバック用（任意）。直接ログインが動けば不要。
> ログイン疎通は `login_test.yml`（手動ワークフロー）で確認できる。

### 5. デプロイ

このリポジトリを GitHub に push すれば、`.github/workflows/automation.yml` が
自動で有効になり、10分毎にゲート判定が走る。

admin画面は既存 `airregi-inventory` 側の変更（`/admin`, `/api/automation/*`）を
Vercel に push すれば反映される。

---

## 🧪 ローカル検証

```bash
# Cookie取得（初回）
HEADLESS=false python src/cookie_tool.py

# AirREGIから当日CSVをDL（目視確認は HEADLESS=false）
python src/airregi_scraper.py --once

# 投入先へアップロード（件数が返れば成功）
python src/uploader.py --file "downloads/バリエーション別売上_YYYYMMDD-YYYYMMDD.csv"

# ゲート判定込みのフル実行
python src/run.py
```

admin画面で `scheduledTime` を直近の時刻に設定 → `python src/run.py` が実行、
ずらすと `skipped` ログになることを確認。

---

## ⚙️ 運用メモ

- **実行時刻の変更**: admin画面 `/admin` の「実行時刻」を変更して保存。
- **一時停止**: admin画面の「自動実行を有効にする」トグルをオフ。
- **手動実行**: admin画面「今すぐ実行」→ 次のActionsチェック（最大10分以内）で実行。
- **稼働状況**: admin画面の実行ログ（成功/失敗/スキップ・件数・所要時間）。

### 既知の注意点
- GitHub Actions の scheduled は高負荷時に数分遅延することがある（±10分ウィンドウで吸収）。
- 60日間リポジトリにコミットが無いと scheduled workflow は自動停止する（適宜コミット）。
- 二重投入は `lastRunDate` で防止。同日中の再実行は手動トリガ(forceRun)のみ。

問題が起きたら [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) を参照。
