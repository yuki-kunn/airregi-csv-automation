# トラブルシューティング / 開発ログ

開発中に発生した問題と対処を記録する（同じエラーの再発時の参照用）。

---

## 1. 投入先ホームに file input が見つからない

**症状**: `/` を開いて `input[type="file"]` を探すと 0 件。

**原因**: `SalesUploader` は `+page.svelte` で `{#if showUploader}` に囲まれており、
「売上取込」ボタンを押すまで file input が DOM に描画されない。

**対処**: アップロード前に `//button[contains(.,'売上取込')]` をクリックする。
クリック後に file input が 2 つ（単一/複数）現れるので、
`input[type="file"][accept=".csv"]:not([multiple])` で単一用を選ぶ。
→ `uploader.py` に反映済み。

---

## 2. WSL に Chrome / sudo が無い

**症状**: WSL Ubuntu に google-chrome 未インストール、sudo はパスワード必須で
非対話インストール不可。chromium は snap のみ（WSLで不安定）。

**対処**: Selenium 4.27 の **Selenium Manager** が Chrome-for-Testing を
自動ダウンロードするため、sudo 無しでローカル実行できる。
GitHub Actions の ubuntu-latest は Chrome 同梱なので本番は問題なし。
→ `browser.py` は既定ドライバ→webdriver-manager の順でフォールバック。

---

## 3. localStorage 認証注入のタイミング

**症状**: about:blank では localStorage に書けない。

**対処**: 先に対象オリジン（`/login`）を開いてから
`localStorage.setItem('ipo_authenticated','true')` を実行し、その後 `/` へ遷移。
→ `uploader.py` の `_inject_auth` で対応済み。

---

## 4. AirREGI ログイン（Recruit ID / OAuth）

**注意**: 自動ID/PASSログインは CAPTCHA / 2段階認証で詰まるリスクが高い。
本システムは **Cookie再利用方式**を採用。`cookie_tool.py` で人が一度ログインして
Cookie を取得し、`AIRREGI_COOKIES`（Secrets/.env）に保存する。

**Cookie失効時**: `airregi_scraper.py` が `LoginExpiredError` を送出し、
`run.py` が `failed` ログを Firestore に残す。`cookie_tool.py` で再取得すること。

> ⚠ 同じログイン/CAPTCHAエラーが繰り返し再現してループしそうな場合は
> 作業を中断し、状況を簡潔に報告する方針（ユーザー要望）。
