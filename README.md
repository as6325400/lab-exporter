# Lab Exporter

Lab Portal 節點監控代理程式。部署在每台 GPU 節點上，自動探索硬體並定期回報監控數據至 Lab Portal 後端。

## 功能

- 自動探索硬體：CPU、記憶體、GPU（NVIDIA）、硬碟、網路介面
- 首次執行自動向 Lab Portal 註冊
- 管理員可在後台選擇要監控的 GPU、硬碟、網卡
- 每 5 秒（可設定）回報一次監控快照
- 支援 systemd 服務管理

## 系統需求

- Python 3.8+
- root 權限（讀取硬體資訊）
- NVIDIA 驅動程式（若要監控 GPU）

## 安裝

### 1. 複製檔案到節點

```bash
# 在節點上
sudo mkdir -p /opt/lab-exporter
sudo cp lab_exporter.py requirements.txt /opt/lab-exporter/
```

### 2. 建立虛擬環境並安裝依賴

```bash
cd /opt/lab-exporter
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt
```

> 若節點無 NVIDIA GPU，可跳過 `pynvml`：
> ```bash
> sudo ./venv/bin/pip install psutil requests
> ```

### 3. 首次註冊

```bash
sudo ./venv/bin/python lab_exporter.py --server https://your-lab-portal.example.com
```

首次執行時：
1. 程式會探索本機硬體（CPU、RAM、GPU、硬碟、網卡）
2. 向 Lab Portal 後端發送註冊請求
3. 取得 token 並儲存至 `config.json`
4. 開始等待管理員設定監控項目

> **重要**：首次註冊後，請通知管理員到 Lab Portal 後台的「節點管理」頁面設定要監控的項目。

### 4. 管理員設定（在 Lab Portal 網頁）

1. 登入 Lab Portal → 管理 → 節點管理
2. 找到剛註冊的節點
3. 展開節點，會看到該節點的所有硬體（GPU 清單、硬碟掛載點、網卡）
4. 勾選要監控的項目
5. 儲存

設定完成後，exporter 會在下次拉取設定時（約 5 分鐘內）自動套用新設定。

### 5. 設定 systemd 服務

```bash
# 編輯 service 檔案，修改 --server 網址
sudo cp lab-exporter.service /etc/systemd/system/
sudo vim /etc/systemd/system/lab-exporter.service
# 修改 ExecStart 中的 --server 為實際的 Lab Portal URL

# 啟用並啟動
sudo systemctl daemon-reload
sudo systemctl enable lab-exporter
sudo systemctl start lab-exporter

# 查看狀態
sudo systemctl status lab-exporter

# 查看日誌
sudo journalctl -u lab-exporter -f
```

## 命令列參數

| 參數 | 必填 | 說明 |
|------|------|------|
| `--server URL` | 是 | Lab Portal 後端網址 |
| `--config PATH` | 否 | config.json 路徑（預設：同目錄下的 config.json） |
| `--interval N` | 否 | 強制指定回報間隔秒數（覆蓋伺服器設定） |
| `--debug` | 否 | 啟用 debug 日誌 |

## config.json

首次註冊成功後自動產生，內容如下：

```json
{
  "token": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "server": "https://lab.example.com"
}
```

> 請勿外洩 token。若 token 洩漏，請到 Lab Portal 後台重設 token，然後刪除本機 config.json 並重新註冊。

## 監控項目

### 固定回報（無法關閉）
- CPU 使用率（%）
- CPU 核心數
- 記憶體使用量 / 總量（GB）
- 系統負載（1/5/15 min）
- 開機時間
- 程序數量

### 可選回報（管理員在後台勾選）
- **GPU**：使用率、顯存、溫度、功耗（可選擇特定 GPU）
- **硬碟**：各掛載點使用量 / 總量（可選擇特定掛載點）
- **網路**：收發速率 Mbps（可選擇特定網卡）

## 故障排除

### 「Node already registered」

節點已經註冊過。解決方式：

1. 請管理員在後台刪除該節點
2. 刪除本機 `config.json`
3. 重新啟動 exporter

### 「Token rejected (401)」

Token 已失效（可能被管理員重設）。解決方式：

1. 刪除本機 `config.json`
2. 重新啟動 exporter（會自動重新註冊）

### 「Config fetch failed / Report failed」

與 Lab Portal 後端連線失敗。檢查：

1. 網路是否可達：`curl -I https://your-lab-portal.example.com/api/health`
2. 防火牆是否開放
3. Lab Portal 後端是否正在執行

### GPU 資訊未顯示

1. 確認 NVIDIA 驅動已安裝：`nvidia-smi`
2. 確認 pynvml 已安裝：`/opt/lab-exporter/venv/bin/pip list | grep pynvml`
3. exporter 需要 root 權限才能存取 NVML

## 架構

```
節點                            Lab Portal
┌──────────────┐               ┌──────────────────┐
│ lab_exporter │──register────→│ POST /register    │
│              │←──token───────│                   │
│              │               │                   │
│              │──get config──→│ GET  /config      │
│              │←──config──────│                   │
│              │               │                   │
│              │──report──────→│ POST /report      │ → in-memory store
│              │  (every 5s)   │                   │   → frontend polling
└──────────────┘               └──────────────────┘
```
