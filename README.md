# LINE 接龍助理

在**現有 LINE 群組**中幫忙管理工作分派、志工報名、認養接龍等名單。

> 不需要新建群組，也不需要其他人加入新群組。
> 只要把 Bot 加入你的群組，直接在原有對話中使用即可。

---

## 如何把 Bot 加入現有群組

1. 開啟你的 LINE 群組
2. 點右上角選單 → **邀請**
3. 搜尋 Bot 的名稱（你在 LINE Developers 設定的名稱）
4. 邀請加入即可

Bot 加入後不會主動說話，等到有人輸入指令才會回應。

---

## 使用方式（群組對話中直接輸入）

| 指令 | 說明 |
|------|------|
| `接龍 [名稱]` | 開始新的報名接龍 |
| `+1 姓名 工作項目 備註` | 加入名單（工作項目和備註可省略） |
| `列表` | 隨時查看目前名單 |
| `退出` | 從名單中移除自己 |
| `結束接龍` | 結束並公布最終名單 |
| `說明` | 顯示使用說明 |

> 📌 每天早上 **07:00（台灣時間）** 自動將最新名單推播至群組，不需要另外查詢。

---

## 對話範例

```
小陳：接龍 2/15 志工值班分配
Bot：✅ 接龍已開始！
     📋 2/15 志工值班分配
     群組成員直接輸入：+1 姓名 工作項目 備註

小明：+1 小明 早班 8:00-12:00
Bot：✅ 已加入！你是第 1 號
     （名單每天 07:00 公布，或輸入「列表」隨時查看）

小華：+1 小華 午班
Bot：✅ 已加入！你是第 2 號

（隔天早上 07:00，Bot 自動推播）
Bot：📣 早安！以下是今日接龍名單（2026/02/15）

     📋 2/15 志工值班分配
     （開團：小陳）
     🕖 更新時間：2026/02/15 07:00
     ────────────────
     1. 小明 早班 8:00-12:00
     2. 小華 午班

小陳：結束接龍
Bot：🔒 接龍已結束，以下為最終名單：...共 2 人報名
```

---

## 安裝與部署

### 1. 建立 LINE Bot

1. 前往 [LINE Developers Console](https://developers.line.biz/)
2. 建立 Provider → 建立 **Messaging API Channel**
3. 取得：
   - **Channel Secret**（Basic settings 頁面）
   - **Channel Access Token**（Messaging API → Issue）
4. Messaging API 設定中：
   - 關閉「Auto-reply messages」
   - 關閉「Greeting messages」
   - Use webhooks：**開啟**

### 2. 本地開發

```bash
# 安裝相依套件
pip install -r requirements.txt

# 設定環境變數
cp .env.example .env
# 編輯 .env，填入 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET

# 啟動伺服器
python app.py

# 另開終端機，用 ngrok 建立公開 HTTPS URL（LINE 需要 HTTPS）
ngrok http 5000
```

把 ngrok 產生的 URL（如 `https://xxxx.ngrok.io/webhook`）填入 LINE Developers Console 的 **Webhook URL**，然後點「Verify」確認連線正常。

### 3. 正式部署（Render 免費方案）

1. 將專案推送到 GitHub
2. 至 [render.com](https://render.com) 建立 **Web Service**，連接 GitHub repo
3. 設定：
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`gunicorn -c gunicorn_config.py app:app`
4. Environment Variables 填入：
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_CHANNEL_SECRET`
5. 部署完成後，將 Render 提供的網址 + `/webhook` 填入 LINE Developers Console

---

## 注意事項

- **每個群組各自獨立**維護接龍名單，不同群組互不影響
- 同一用戶重複輸入 `+1` 會更新項目，不會重複報名
- 資料儲存於 SQLite（`jielong.db`）；正式環境建議改用 PostgreSQL
- Render 免費方案會在閒置時休眠，可能導致 07:00 推播延遲；可考慮 Railway 或自架主機
