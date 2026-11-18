# SocialArchiveCore

Instagram 與 Twitter/X 的自動封存及媒體抓取核心，以兩個可獨立安裝的 Python
背景服務套件提供。支援 CLI、持久工作狀態、自動排程及下載流程。

| 套件 | 功能與安裝 | 狀態儲存 |
| --- | --- | --- |
| Instagram | [packages/instagram](packages/instagram/README.md) | 獨立 SQLite 狀態與媒體目錄 |
| Twitter / X | [packages/twitter](packages/twitter/README.md) | 獨立 PostgreSQL schema、帳號池及媒體目錄 |

兩個套件保留各自平台的抓取方式，使用各自的設定與帳號，不需要部署原本的網站。
下載前須配置外部登入憑證及媒體儲存；repo 不包含 Cookie、帳號資料庫或實際媒體。

## 安裝

Python 3.11+。建議兩個套件使用各自的 virtual environment，依照上方套件 README
設定所需的資料庫、目錄與憑證。只需其中一個平台時，可只安裝相應套件。

```sh
python3 -m venv .venv-instagram
.venv-instagram/bin/pip install ./packages/instagram

python3 -m venv .venv-twitter
.venv-twitter/bin/pip install ./packages/twitter
```

詳細命令、設定與測試方式見各套件 README；[維運說明](docs/operations.md)
涵蓋儲存、監控、備份、更新與回復。安裝不會自動啟動 worker 或建立主機服務。

## 提取範圍

來源為 [SocialMedia_autoArchive](https://github.com/Senachi124/SocialMedia_autoArchive)
及 [XLoader](https://github.com/Senachi124/XLoader) 的背景排程／抓取程式。
這是獨立維護的核心版本，有自己的 Git 歷史，不會自動替換來源專案的正式部署。

網站、管理介面、共用登入、Telegram Bot、原主機設定、NAS 傳送／讀取與媒體清理
不包含在此 repo。此處的 archive 是保存下載內容到本機持久儲存，並不表示已備份至 NAS。
平台登入及接口可能改變；離線回歸通過不等同目前所有帳號都能完成真實抓取。

每個套件附有來源檔案 SHA-256 與提取說明；詳見 [來源與架構](docs/architecture.md)。
原來源未附明確的專案授權檔，本次未另行授予新的開源 license；第三方依賴維持各自授權。

## 維護

執行兩個套件的離線測試，並在提交前檢查公開發布範圍：

```sh
python3 scripts/check_publication.py
git diff --check
```

檢查工具只檢查 Git 追蹤的程式與文件，不讀取外部憑證、媒體或正式狀態。
規則見 [AGENTS.md](AGENTS.md)，版本紀錄見 [CHANGELOG.md](CHANGELOG.md)。
