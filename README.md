# SocialArchiveCore

SocialArchiveCore 是公開的 Python 實作參考，收錄 Instagram 與 Twitter／X 的媒體抓取、自動封存及工作排程。你可以按需求閱讀單一模組，例如貼文分頁、下載去重、封存索引或 Cookie／Session 管理。想看完整流程，也可以獨立安裝任一平台的套件，透過 CLI 執行。

## 專案範圍

兩個套件各自接收封存目標及排程設定，讓背景程序抓取內容，把媒體及封存紀錄保存到操作者指定的儲存位置。Instagram 使用 SQLite。X 使用 SQLite 保存帳號池，並以 PostgreSQL 保存工作和封存索引。

需要傳連結到 Telegram 並在對話中收取媒體，可參考 [TelegramSocialDownloader](https://github.com/Senachi124/TelegramSocialDownloader)。兩個專案各自安裝、維護與發布，使用各自的設定、工作佇列和儲存目錄。

## 按功能找程式碼

| 想參考的功能 | Instagram | Twitter／X |
| --- | --- | --- |
| **抓取與分頁** | [instagram_web_timeline.py](packages/instagram/src/instagram_archive_core/instagram_web_timeline.py) 處理貼文分頁和媒體欄位解析。[instagram_web_gallery.py](packages/instagram/src/instagram_archive_core/instagram_web_gallery.py) 將 Web GraphQL 抓取接到 gallery-dl。 | [xsource.py](packages/twitter/src/xloader/xsource.py) 用 twscrape 取得帳號貼文、搜尋結果與單篇貼文。[search.py](packages/twitter/src/xloader/search.py) 將歷史日期範圍切成工作區段，[capture.py](packages/twitter/src/xloader/capture.py) 負責媒體下載。 |
| **封存紀錄與防重複下載** | [catalog.py](packages/instagram/src/instagram_archive_core/catalog.py) 在 SQLite 記下媒體 ID、路徑、大小與 SHA-256。重用前會核對實際檔案，貼文紀錄也保留所屬目標。[gallery_worker.py](packages/instagram/src/instagram_archive_core/gallery_worker.py) 用該目標已完整封存的貼文作為增量抓取的停止點。 | [archive.py](packages/twitter/src/xloader/archive.py) 在 PostgreSQL 保存貼文版本與檔案清單。重用既有封存前，會逐檔核對大小與 SHA-256。[schema.sql](packages/twitter/src/xloader/schema.sql) 定義貼文版本及工作關聯。 |
| **Cookie 與 Session** | [core.py](packages/instagram/src/instagram_archive_core/core.py) 的 `register_account()` 保存外部 Cookie 檔的路徑，`build_config()` 將該路徑交給 gallery-dl。[instagram_web_client.py](packages/instagram/src/instagram_archive_core/instagram_web_client.py) 處理已登入的請求與登入失效等回應。 | [cookies.py](packages/twitter/src/xloader/cookies.py) 解析與驗證匯出的 X Cookie。[xsource.py](packages/twitter/src/xloader/xsource.py) 的 `Source` 與 `SessionPool` 管理 twscrape 帳號池，並在重新匯入 Cookie 或重啟時保留端點冷卻紀錄。 |
| **自動排程、工作防重與重試** | [core.py](packages/instagram/src/instagram_archive_core/core.py) 處理到期目標、排程入列與自動工作優先順序。[auto_recovery.py](packages/instagram/src/instagram_archive_core/auto_recovery.py) 記錄自動重試及後續執行時間。 | [worker.py](packages/twitter/src/xloader/worker.py) 選取到期目標並優先處理自動工作。[db.py](packages/twitter/src/xloader/db.py) 的 `create_job()` 管理同目標工作去重，[jobs.py](packages/twitter/src/xloader/jobs.py) 處理暫停、恢復與重試。 |

如果你只想參考去重邏輯，先讀 Instagram 的 `known_media()`、`known_post_id()`，以及 X 的 `reusable_post()`。這些函式把「資料庫有紀錄」與「本機檔案仍完整」連在一起。Instagram 的測試展示輪播缺少一張圖時如何繼續抓取，也展示不同目標共用媒體時如何保留各自的增量邊界。X 的整合測試展示同一貼文重跑後如何重用本機封存。[Instagram 增量邊界測試](packages/instagram/tests/test_boundary.py) · [X 封存整合測試](packages/twitter/tests/test_postgres.py)

閱讀單一模組時，可用各套件的 `pyproject.toml` 查看依賴，並從測試找輸入及預期結果。Instagram 的抓取接到 gallery-dl，封存索引使用 SQLite。X 的抓取接到 twscrape，Session 帳號池使用 SQLite，工作和貼文封存索引使用 PostgreSQL。[Instagram 依賴](packages/instagram/pyproject.toml) · [Instagram 測試](packages/instagram/tests/) · [X 依賴](packages/twitter/pyproject.toml) · [X 測試](packages/twitter/tests/)

## 執行完整套件

兩個套件均支援 Python 3.11 以上。選擇需要的平台，在獨立虛擬環境安裝。執行抓取前，依照套件指南設定媒體目錄、自己的 Cookie 檔與狀態儲存。[Instagram 安裝與 CLI](packages/instagram/README.md) · [Twitter／X 安裝與 CLI](packages/twitter/README.md) · [維運說明](docs/operations.md)

```sh
python3 -m venv .venv-instagram
.venv-instagram/bin/pip install ./packages/instagram
.venv-instagram/bin/instagram-archive --help

python3 -m venv .venv-twitter
.venv-twitter/bin/pip install ./packages/twitter
.venv-twitter/bin/social-archive-twitter --help
```

## 來源與授權

本 repo 收錄本機封存所需的背景程式碼與合成測試，來源為 SocialMedia_autoArchive 與 XLoader。來源檔的路徑與 SHA-256 列在各平台的提取紀錄中。[SocialMedia_autoArchive](https://github.com/Senachi124/SocialMedia_autoArchive) · [XLoader](https://github.com/Senachi124/XLoader) · [架構與來源紀錄](docs/architecture.md)

本 repo 未附開源授權，第三方依賴各有其授權。若要將程式碼納入自己的專案或重新發佈，請先向權利人確認授權。
