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

### 3. AirREGI Cookie の登録（admin画面から / 推奨）

AirREGIは Recruit ID / OAuth ログインで、自動ログインは CAPTCHA / 2段階認証で
詰まるリスクが高い。そのため **一度だけ人がブラウザでログインして Cookie を登録** する。
登録は **admin画面 `/admin` の「AirREGI ログインCookie」** から行う（Firestoreに保存され、
run.py が自動で読む）。

**手順（Windowsの普段使いブラウザでOK・WSLのGUI不要）:**

1. [AirREGI売上ページ](https://airregi.jp/CLP/view/salesListByMenu/) を開いてログイン
2. `F12`（開発者ツール）→「Application」タブ →「Cookies」
3. `https://airregi.jp` と `https://connect.airregi.jp` の両方の行を全選択コピー
4. admin画面 `/admin` の「AirREGI ログインCookie」欄に貼り付けて「保存」

> Cookie-Editor 等の拡張機能でエクスポートした **JSON配列**も貼り付け可能。
> Cookieが失効すると `failed` ログが出るので、その時は同じ手順で再登録する。

> **代替（WSL GUIが使える場合）**: `HEADLESS=false python src/cookie_tool.py` で
> ブラウザログイン → 出力JSONを admin画面 or `AIRREGI_COOKIES` に設定。

### 4. GitHub Secrets

リポジトリの Settings → Secrets and variables → Actions に登録:

| Secret | 内容 |
|---|---|
| `FIREBASE_ADMIN_SERVICE_ACCOUNT_KEY` | サービスアカウントJSON(Base64) |
| `FIREBASE_ADMIN_PROJECT_ID` | `ipo-kaidashi` |
| `IPO_UPLOAD_PASSWORD` | 投入先のログインパスワード |

> **Cookieは Secrets 不要**: admin画面から Firestore に登録するため、
> `AIRREGI_COOKIES` の Secret 登録は任意（ローカル実行時のフォールバック用）。

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
