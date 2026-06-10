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

---

## 5. cookie_tool.py のブラウザが開かない / 「COPYMODE」表示

**症状**: `HEADLESS=false python src/cookie_tool.py` でブラウザが画面に出ない、
またはターミナルに「COPYMODE」と出てEnterが効かない。

**原因**:
- 「COPYMODE」は**ターミナル（Windows Terminal / tmux）の選択モード**表示で、
  Chromeのエラーではない。選択モード中はキー入力(Enter)が効かない。
- WSLからGUIを出すには WSLg が必要。環境によってはウィンドウが
  Windows画面に転送されないことがある。

**対処（推奨・GUI不要）**: **admin画面 `/admin` の「AirREGI ログインCookie」**から
DevToolsでコピーしたCookieを貼り付けて登録する方式に変更した。
WindowsのChromeでログイン → F12 → Application > Cookies をコピー → 貼り付け → 保存。
Cookieは Firestore `automation/config.airregiCookies` に保存され、
`airregi_scraper.py._load_cookies()` が Firestore → 環境変数 → ファイルの順で読む。

**cookie_tool.py 自体の改善**: Enter入力に依存せず、売上ページ到達を
URLポーリングで自動検知するように変更（COPYMODEでも確実に進む）。

---

## 6. Cookie登録APIが Forbidden になる

**症状**: `/api/automation/cookies` への POST が
`{"error":"Forbidden","message":"リクエストが拒否されました"}`。

**原因**: 既存の `hooks.server.ts` のCSRF保護が Origin/Referer をチェックしている。
curl等でOriginヘッダー無しのPOSTは弾かれる（仕様通り・他APIと同じ）。

**対処**: ブラウザのfetchは自動でOriginを付けるため、admin画面からの操作では問題なし。
検証時にcurlを使う場合は `-H "Origin: <自サイト>"` を付ける。

---

## 7. CSVダウンロードボタンが見つからない

**症状**: ログインは成功（売上ページ「商品別売上 | Airレジ」に到達）するが、
`CSVダウンロードボタンが見つかりませんでした` で失敗。

**原因**: 推測で書いていたセレクタが実DOMと不一致。実際のボタンは:
```html
<button class="btn-CSV-DL pull-left menu-text ...">
  <span class="download-text">商品単位の売上(CSV)をダウンロードする</span>
</button>
```
ボタンのテキストが内側の `<span>` にあるため `button/text()` では一致しなかった。

**対処**:
- `config.AIRREGI_CSV_BUTTON_CSS = "button.btn-CSV-DL"` を最優先セレクタに。
- XPathは `contains(text(),...)` → `contains(.,...)` に変更（子要素のテキストも対象）。
- 診断機能を追加: ボタン未検出時に全リンク/ボタンのテキスト・classをログ出力し、
  HTML/スクショを Actions アーティファクト(debug-page)として保存する。
  → `_dump_page_diagnostics()` / workflow の Upload debug artifacts ステップ。

**実DOM調査の方法**: `gh run view <id> --log | grep 診断` で要素一覧、
Actions の Artifacts から debug_page.html / debug_page.png をダウンロード。

---

## 8. 商品単位ではなくバリエーション単位のCSVが必要

**要件**: 投入先はバリエーション別CSV（ファイル名も「バリエーション別売上_…」）を期待。
だが商品別売上ページの既定は「商品単位」で、CSVボタンも1つだけ。

**実DOM**: ページ上部に集計単位のラジオがある:
```html
<input type="radio" name="searchOrderBy" value="0" checked>  → 商品単位で表示
<input type="radio" name="searchOrderBy" value="1">          → バリエーション単位で表示
```
CSVボタン名は「商品単位の売上(CSV)」のままだが、選択中の表示単位に応じた内容が出力される。

**対処**: CSVダウンロード前に
1. `input[name="searchOrderBy"][value="1"]` をJSクリックで選択
2. `button.btn-search`（表示する）で再集計
3. `button.btn-CSV-DL` でダウンロード
→ `_select_variation_unit()` を `download_csv` に追加。

---

## 9. 指定日実行 + 天候自動登録（フェーズ2）

**機能**: admin画面から日付を選んで過去日のCSVを取得、アップロード後に天候も登録。

**指定日実行の仕組み**:
- admin画面「指定日のデータを取得」→ `automation/config` に `{forceRun:true, runDate:"YYYY-MM-DD"}`
- run.py の `_resolve_target_date()` が runDate を対象日に採用。成功/失敗後 `_consume_triggers()` で
  forceRun/runDate をクリア（無限再試行防止）。指定日実行では lastRunDate は更新しない。
- airregi_scraper の `_set_date_range()`: readonly datepicker の value を
  `YYYY/MM/DD ~ YYYY/MM/DD` にJSで設定し input/change を発火 → btn-search で再集計。

**天候登録の仕組み**:
- uploader の `_register_weather()`: アップロード成功後、同ブラウザの `execute_async_script` で
  サイト内 `/api/weather` → `/api/firestore/dailySales`(addOrUpdate) を叩く（CSRF/Origin自然通過）。
- その日の dailySales が無い（0件）場合は天候登録スキップ。失敗は警告のみ。
- 場所は `WEATHER_LOCATION="Izumi, Osaka, Japan"`。

**注意**: datepicker の value 書換がウィジェット内部状態に反映されない場合は、
debug_page.html を見てカレンダークリック方式へ切替が必要。
天候APIは WeatherAPI.com 無料プランで履歴取得可（過去日OK、未来は3日先まで）。
