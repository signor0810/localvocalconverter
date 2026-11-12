# Local Typeless Tool（本機語音口述轉結構化文字）

本專案對標 Typeless 體驗，專注於中英夾雜之計算微影（Computational Lithography）工程口述、技術摘要與簡報產出。

> 完整安裝步驟、環境建置與疑難排解請參閱 **[docs/安裝指南.md](docs/安裝指南.md)**。
> 安裝時會裝到電腦上的所有套件與授權列在 **[docs/套件清單.md](docs/套件清單.md)**。

---

## 1. 快速啟動 UI

專案提供三種啟動方式：

1. **無黑框背景執行（日常推薦）**：
   - 雙擊專案根目錄的 `LocalTypeless.vbs`。
   - 使用 `pythonw.exe` 於背景無黑框啟動停靠在螢幕右側的面板。
2. **偵錯模式（含主控台記錄）**：
   - 雙擊專案根目錄的 `start_ui.bat`。
   - 保留終端機視窗，可即時查看 STT 辨識、LLM 請求與日誌輸出。
3. **建立桌面捷徑**：
   - 以 PowerShell 執行 `scripts/create_desktop_shortcut.ps1`，即可在桌面生成「Local Typeless」捷徑（以 `wscript.exe` 靜默啟動 `LocalTypeless.vbs`）。

---

## 2. 全域快捷鍵（Global Hotkeys）

在任何輸入框、編輯器或投影片軟體中皆可直接使用：

| 快捷鍵 | 模式 | 說明 |
|---|---|---|
| `<Alt> + Q` | **整理模式（預設）** | 去除贅字贅詞，修正專業專有名詞與大小寫，輸出通暢的技術口述正文。 |
| `<Alt> + S` | **Slide（簡報模式）** | 整理為單頁簡報架構（標題＋精煉階層 Bullet points）；口述「這裡放圖 X」自動轉為 `> [!todo] 🖼️ 待補圖：X`。 |
| `<Alt> + W` | **Summary（摘要模式）** | 將長段口述濃縮為核心要點（精煉短段落或 3～6 點結構化清單）。 |
| `<Alt> + V` | **貼上圖片** | 僅在錄音中生效；把剪貼簿裡的圖片（截圖或複製的圖）插入到目前說到的位置，結果會依序貼成「文字 → 圖片 → 文字」。 |
| `<Esc>` | **取消** | 僅在錄音進行中生效；立即中止錄音並丟棄音訊緩衝區，不覆寫暫存檔。 |

*註：錄音採 Toggle 機制（按一次開始、再按一次停止後自動處理輸出）。*

---

## 3. 輸出位置與設定

文字處理完成後依 `config/config.yaml` 設定分發：
- **系統剪貼簿（預設開啟）**：文字自動寫入 Windows 剪貼簿，並可播放提示音。
- **自動模擬貼上（`auto_paste: true`）**：完成後自動釋放修飾鍵並模擬 `Ctrl+V`，直接貼至當前游標處。
- **Obsidian 筆記同步（可選）**：設定 `obsidian_note_path: "D:/Vault/slides.md"`，每次口述會自動追加 `\n\n---\n` 與分頁內容至指定 Markdown 檔案。

---

## 4. 設定遠端 LLM 後端（OpenAI 相容服務）

1. 複製設定檔範本：
   ```bash
   cp config/config.example.yaml config/config.yaml
   ```
2. 開啟 `config/config.yaml`，修改 `llm_profiles.dev` 區塊：
   ```yaml
   active_llm_profile: dev

   llm_profiles:
     dev:
       provider: openai_compatible
       base_url: "http://<YOUR_LLM_HOST>:8000/v1"  # vLLM / Ollama / SGLang 等 OpenAI 相容端點
       api_key_env: DGX_API_KEY
       model: "Qwen/Qwen2.5-32B-Instruct"
       timeout_sec: 15.0
       temperature: 0.2
   ```
3. 設定環境變數（若內網 vLLM 無金鑰保護可設為 `EMPTY`）：
   ```powershell
   [System.Environment]::SetEnvironmentVariable('DGX_API_KEY', 'EMPTY', 'User')
   ```

---

## 5. 斷線不丟字保證（Fallback 機制）

- 若遠端 LLM 離線、網路超時或未配置 LLM，Pipeline 會自動觸發**安全回退（Fallback）**。
- 工具會立即將本機 `faster-whisper` 產出之原始逐字稿，經術語詞庫（`config/glossary.yaml`）進行大小寫與縮寫正規化後直接輸出至剪貼簿，**絕不丟失任何口述內容**。

---

## 6. 圖片插入（截圖與圖片口述）

口述過程中可隨時補充截圖或複製圖片：
- **操作方式**：先截圖（`Win + Shift + S`）或複製圖片，錄音前或錄音中皆可；講到要放圖的地方按 `Alt + V`（或面板的「貼上圖片」），圖片就會以 `[圖1]`、`[圖2]` 標記插入該位置，錄音鍵同步顯示 `圖×N`。
- **圖文混合貼上**：完成後依序以「文字 → 圖片 → 文字」貼入目標應用程式（目標需支援貼上圖片，例如 Word、PowerPoint）。純文字欄位貼不進圖片，文字不受影響。
- **本機隱私保證**：圖片只存在本機（預設 `records/images/`，可設定），超過保留天數自動刪除。遠端 LLM 只收到 `[圖N]` 文字標記，圖片內容不會送到任何模型。
- **自動留存紀錄**：每次口述完成後，在 `records/<年-月>/` 存成一個 Markdown 檔（檔名為「日期-時間_模式_開頭文字」），內含整理結果、圖片連結與原始逐字稿。位置與保留天數（預設 90 天）可在 `storage` 設定。

---

## 7. 流暢長口述（串流 STT 與背景佇列）

針對數分鐘以上的長篇技術口述，提供流暢的錄製與整理體驗：
- **即時串流轉錄**：錄音中系統自動依語音停頓切段並於背景辨識，面板即時顯示累計逐字稿。
- **背景佇列處理**：按下停止鍵後立即回到就緒狀態，可立刻按下熱鍵展開下一段口述，無需等待後台處理完畢。
- **焦點保護機制**：若在進行新錄音時先前的口述整理完成，結果會暫存於佇列，待當前錄音結束後依序輸出，絕不搶奪焦點或干擾口述。
- **分段整理與部分回退**：長文自動按語意切分為約 700 字之區塊個別送交 LLM 整理，並自動附上前文語境；若單一段落 LLM 失敗僅該段落以原文輸出，其餘段落依然精準整理（面板顯示 `部分段落 LLM 失敗（X/Y）`）。

## 進階設定

調整 STT 執行硬體（CPU/GPU）、更換 STT 或 LLM 模型、圖片與串流長口述進階參數、公司內部 gateway 設定，請見 [docs/設定手冊.md](docs/設定手冊.md)。

## 授權

本專案以 [MIT License](LICENSE) 釋出。相依套件各自的授權見 [docs/套件清單.md](docs/套件清單.md)。
