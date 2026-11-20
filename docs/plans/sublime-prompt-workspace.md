# 交接計畫：Sublime Text 版 Prompt 工作區（Local Typeless）

> 對象：在使用者 Windows 電腦上工作的 coding agent
> 目標 repo：`signor0810/localvocalconverter`（以下簡稱「repo」）
> 撰寫日期：2026-11-20
> 狀態：設計已定案，可直接實作

---

## 0. 已定案的決策（不要重新討論）

1. **語音辨識改用 Windows 內建的 Win+H**。faster-whisper 不再是主要輸入，但程式碼保留不刪。
2. **編輯介面改用 Sublime Text 4**（使用者的版本是 build 4200），取代現有 Tk 草稿視窗。**Win+H 能打進 Sublime 已實測確認。**
3. 使用者原本用 Sublime 寫 prompt，痛點是「存檔、取名、貼來貼去」。所以這套系統的核心價值是：
   **自動存檔與歸檔、智慧貼上（圖片和檔案自動轉成路徑）、一鍵 AI 整理、所有 prompt 集中記錄**，而且不綁定特定 agent。
4. **只有一個設定中心**：沿用 repo 的 `config/config.yaml`（加上 `settings.local.yaml` 覆蓋層），新增 `editor:` 區塊。現有設定（LLM profiles、glossary、prompts/*.md、cleanup、guard、records）全部保留並照常生效。
5. 舊的 Tk 草稿視窗**保留可用**，兩種介面共用同一套後端。

## 1. 範圍

### 要做
- 後端：新增 `local-typeless serve` 本機 HTTP 服務，沿用 `Pipeline.refine_only()`
- Sublime plugin：自動建檔與存檔、智慧 `Ctrl+V`、AI 整理（頂端可點連結、右鍵選單、快捷鍵、Command Palette）、送出、歷史清單、圖片預覽
- `config.yaml` 的 `editor:` 區塊與熱重載
- 安裝腳本、文件、測試

### 不做
- 語音辨識整合（使用者自己按 Win+H）
- 攔截「從檔案總管拖檔案進 Sublime」：Sublime 會直接開啟檔案，無法穩定攔截。改用「複製後貼上」
- 把圖片本體貼進網頁 agent（只提供路徑）

## 2. 架構

```
┌──────────────── Sublime Text 4 (build 4200) ────────────────┐
│ Packages/LocalTypeless/  (plugin, Python 3.8, 只用 stdlib)    │
│  - lt_core.py     純邏輯，不 import sublime，可用 pytest 測   │
│  - lt_plugin.py   commands / listeners / phantoms           │
└──────────────┬──────────────────────────────────────────────┘
               │ HTTP 127.0.0.1:<port>，帶 token 標頭
┌──────────────▼──────────────────────────────────────────────┐
│ local-typeless serve  (repo 的 venv，Python ≥3.10)           │
│  /health /config /refine /clipboard/capture                  │
│  └─ Pipeline(cfg, stt=NullSTT).refine_only(...)              │
│     └─ glossary / prompts/*.md / guard / LLM profiles（沿用） │
└─────────────────────────────────────────────────────────────┘
```

**為什麼分成兩個行程**：Sublime 的 plugin host 用自己內建的 Python 3.8，裝不了 repo 的相依套件（pydantic、httpx、pillow……）。所以 plugin 只做 UI 和檔案操作，其他工作都透過 HTTP 交給後端。

## 3. Repo 現況（實作時會用到的介面）

| 用途 | 位置 | 說明 |
|---|---|---|
| 純文字整理 | `src/local_typeless/pipeline.py` `Pipeline.refine_only(text, mode, guard_warns=False)` | 不輸出、不寫 history 和 record。回傳 `PipelineResult`，含 `final_text`、`fallback_triggered`、`error_message`、`guard_warnings`、`llm_elapsed_sec` |
| Pipeline 建構 | `pipeline.py` `Pipeline.__init__(config, *, stt=None, llm=None, ...)` | `stt=None` 會建 FasterWhisperSTT。**serve 要傳入不做事的 NullSTT**，避免 import faster-whisper |
| 草稿版整理的呼叫方式 | `ui/draft_bridge.py` `DraftBridge.refine()` | 呼叫時帶 `guard_warns=True`，`fallback_triggered` 視為失敗。**serve 照這套語意做** |
| 標記保護與還原 | `ui/draft_model.py` `protect()` / `restore()` | 嵌入內容換成 `[文字N]`，還原時檢查每個標記剛好出現一次，否則回傳 None。**目前跟 buffer tag 綁在一起，要抽出純函式**（見 4.3） |
| 模式 | `types.py` `Mode`：`clean`、`slide`、`transform`、`summary`、`prompt` | 對應 `prompts/<mode>.md`。`prompt` 模式是「把口述改寫成給 AI 的 prompt」，很適合這個情境 |
| 設定載入 | `config/loader.py`：`resolve_config_path()`、`_with_settings()`（疊上 `settings.local.yaml`）、`load_config()` | Pydantic 預設忽略未知欄位，所以加上 `editor:` 不會影響舊的 loader |
| 剪貼簿 | `capture/clipboard_images.py` `Win32ClipboardBackend`：`grab_png()`、`grab_files()`、`grab_text()`；另有 `ClipboardBackend` Protocol 可注入 fake 做測試 | 讀剪貼簿圖片與檔案直接沿用這裡 |
| CLI | `cli.py` `_build_parser()` / `main()` | 在這裡加 `serve` 子命令 |
| 測試 | `tests/`（pytest，`pip install -e .[dev]`） | 既有測試必須全部通過 |

**限制**：`src/local_typeless/types.py` 檔頭標示 **LOCKED**，不要修改。新的設定模型放在新模組裡（見 4.2），不要改 `AppConfig`。

## 4. 設計細節

### 4.1 後端 `local-typeless serve`

- 新增模組 `src/local_typeless/editor/server.py`，用 stdlib 的 `http.server.ThreadingHTTPServer`，**不新增相依套件**。
- CLI：`local-typeless serve [--port N]`。預設 port 取自 `editor.server.port`（預設 `8765`）。
- **只綁 `127.0.0.1`。**
- **安全性**：
  - 啟動時產生隨機 token，連同 port 寫入 `%LOCALAPPDATA%\local-typeless\serve.json`，內容為 `{port, token, pid}`。
  - 每個請求都要帶 `X-LT-Token`，否則回 401。這樣網頁就無法對 localhost 發動 CSRF：自訂標頭會觸發 CORS preflight，而服務不回 CORS 標頭。
  - 帶有 `Origin` 標頭的請求一律拒絕。
  - 請求 body 上限 2 MB。
- 建立 Pipeline 時用 `Pipeline(cfg, stt=NullSTT(), records=False)`。

| Endpoint | 輸入 | 輸出 |
|---|---|---|
| `GET /health` | – | `{ok, version, llm_profile}` |
| `GET /config` | – | 解析好的 `editor` 設定（已套用預設值），加上 `modes` 清單（`prompts/*.md` 中實際存在的模式）、`active_llm_profile`，以及設定錯誤清單 `config_errors` |
| `POST /refine` | `{text, mode, protect: true}` | `{ok, text, warnings[], fallback, error, elapsed_sec}`。`fallback_triggered` 時回 `ok=false`，並附 `error` |
| `POST /clipboard/capture` | `{prompt_path}` | `{kind: "image"\|"files"\|"text", paths: [...]}`。圖片依 `editor.paste.image_dir` 和 `image_name` 存檔，回傳絕對路徑；`text` 時 paths 為空，由 plugin 自行做一般貼上 |

- **設定熱重載**：每次處理請求前，先比對 `config.yaml` 和 `settings.local.yaml` 的修改時間，有變動就重新載入。載入失敗時沿用上一份可用的設定，並在 `/config` 的 `config_errors` 回報。

### 4.2 `editor:` 設定區塊

- 新模組 `src/local_typeless/editor/config.py`：
  - 用 Pydantic 定義 `EditorConfig`。
  - 讀取方式：`resolve_config_path()` → 讀 YAML → 用 `_with_settings()` 疊上 `settings.local.yaml` → 取出 `editor` 子樹 → 驗證。
  - **欄位層級容錯**：某個欄位驗證失敗時，該欄位退回預設值，錯誤訊息收進 `config_errors`，不讓整份設定失效。
- 在 `config/config.example.yaml` 補上完整的 `editor:` 範例。所有欄位與預設值如下：

```yaml
editor:
  prompts_root: "~/Documents/prompts"   # 使用者尚未指定；可設 OneDrive 底下以自動備份
  file_layout: "{yyyy}/{mm}/{date}_{time}_{title}.md"   # title 初始為 "untitled"
  auto_rename_after_refine: true
  autosave_delay_ms: 1000

  server:
    port: 8765
    autostart_cmd: ""                   # 例："pythonw -m local_typeless.cli serve"；空字串＝不自動啟動

  paste:
    image_dir: "assets/{yyyy}-{mm}"      # 相對於 prompts_root
    image_name: "{prompt}_{n:02}.png"   # {prompt}＝prompt 檔名主幹
    path_style: windows                 # windows | forward | wsl | markdown
    quote_paths_with_spaces: true
    image_preview_on_hover: true

  refine:
    default_mode: clean
    modes_in_palette: [clean, prompt, slide, summary, transform]
    result_view: split_right            # split_right | below | replace
    protect_paths: true
    protect_code_blocks: true

  send:
    copy: refined                       # refined | raw | both
    auto_paste_to_last_window: false
    mark_sent_in_frontmatter: true

  frontmatter:
    fields: [created, status, mode, refined_at, sent_at, target]
```

- 路徑格式範例：
  - `windows`：`C:\Users\me\x.png`
  - `forward`：`C:/Users/me/x.png`
  - `wsl`：`/mnt/c/Users/me/x.png`
  - `markdown`：`![](C:/Users/me/x.png)`

### 4.3 送 LLM 前的保護與還原（純函式，後端）

- 新模組 `src/local_typeless/editor/protect.py`：
  - `protect(text, *, paths=True, code=True) -> (protected_text, mapping)`
  - `restore(refined, mapping) -> Optional[str]`
- 要保護的內容依序為：
  1. 程式碼區塊（三個反引號圍住的 fenced block）
  2. 行內程式碼
  3. 加了引號的路徑（`"C:\a b\c.png"`）
  4. Windows 絕對路徑 `[A-Za-z]:[\\/][^\s"'<>|]*`
  5. UNC 路徑 `\\server\share\...`
  6. WSL／POSIX 絕對路徑 `/mnt/...`、`/home/...`
  7. URL `https?://\S+`
  8. Markdown 圖片 `![...](...)`
- 保護的內容一律換成 `[文字N]`，與現有 prompt 和 guard 使用的標記相同。
- `restore` 的語意照抄 `DraftModel.restore`：每個標記必須剛好出現一次，也不能多出未知的標記，否則回傳 `None`。後端收到 `None` 時回 `ok=false, error="整理結果遺失了路徑或程式碼標記，已保留原文"`。
- **可以順便**讓 `DraftModel.protect()/restore()` 改呼叫這組純函式，但不強制。如果動了，`tests/test_draft_model.py` 必須照常通過。

### 4.4 Sublime plugin

**位置**：repo 內 `sublime/LocalTypeless/`。用 `scripts/install_sublime.ps1` 建立 junction（`mklink /J`），指向 `%APPDATA%\Sublime Text\Packages\LocalTypeless`，這樣改 repo 就會立即生效。

| 檔案 | 內容 |
|---|---|
| `.python-version` | `3.8` |
| `lt_core.py` | 純邏輯，不 import `sublime`：路徑格式化、frontmatter 解析與寫回、檔名 layout 展開、標題 slug、serve.json 讀取 |
| `lt_plugin.py` | 所有 `sublime_plugin` commands 和 listeners |
| `Default.sublime-keymap` | 快捷鍵（見下方） |
| `Context.sublime-menu` | 右鍵選單：AI 整理、整理選取範圍、各模式整理、送出 |
| `Main.sublime-menu` | 上方選單「Local Typeless」：新 prompt、歷史、開啟設定、後端狀態 |
| `Default.sublime-commands` | Command Palette 項目，每個模式一條 |

**「受管理的檔案」**：路徑在 `prompts_root` 底下、副檔名為 `.md` 的檔案。開啟時設定 view setting `lt_managed = true`。以下所有行為都只作用在受管理的檔案，**一般檔案的 Sublime 行為完全不變**。

#### Commands

| Command | 快捷鍵 | 行為 |
|---|---|---|
| `lt_new_prompt` | `Ctrl+Alt+N` | 依 `file_layout` 算出路徑，寫入 frontmatter（`created`、`status: draft`），立即存檔並開啟，游標放在 frontmatter 之後 |
| `lt_smart_paste` | `Ctrl+V`（context：`setting.lt_managed`） | 呼叫 `/clipboard/capture`。`image`／`files`：依 `path_style` 格式化後插入，多個路徑一行一個；`text` 或後端離線：執行內建的 `paste`，後端離線時在狀態列提示 |
| `lt_paste_raw` | `Ctrl+Shift+V` | 內建 `paste` |
| `lt_refine` | `Ctrl+Alt+R` | 有選取範圍時只整理選取部分，否則整理 frontmatter 以外的全文。用 `sublime.set_timeout_async` 搭配 `urllib` 呼叫 `/refine`，狀態列顯示「整理中…」，不阻塞 UI。參數 `mode` 不給時用 `default_mode` |
| `lt_send` | `Ctrl+Alt+Enter` | 依 `send.copy` 把整理結果（或原文）放進剪貼簿，frontmatter 寫入 `status: sent` 和 `sent_at`。`auto_paste_to_last_window` 留到 Phase 3 |
| `lt_history` | `Ctrl+Alt+H` | Quick panel 列出 `prompts_root` 底下最近 200 個 prompt，依修改時間排序，每列顯示 `狀態 · 時間 · 第一行內容`，選了就開 |
| `lt_open_settings` | – | 開啟 `config.yaml`（後端從 `/config` 回傳實際路徑） |

#### 整理結果怎麼呈現
- 結果存成同資料夾的 `<主檔名>.refined.md`，也算受管理的檔案，所以原文和整理結果都會留紀錄。
- `split_right`：視窗切成左右兩欄，原文在左、結果在右。再次整理時覆寫右欄內容，用 `view.run_command` 取代內容，這樣 Ctrl+Z 可以退回上一版結果。
- `guard_warnings` 以 phantom 列在結果檔頂端，狀態列也同時顯示。warnings 只是文字清單、沒有位置資訊，**不要做逐字底線標示**。
- 原文檔的 frontmatter 更新 `status: refined`、`mode`、`refined_at`。
- 第一次整理成功、且 `auto_rename_after_refine` 為真時：從整理結果的第一個標題或第一句取標題（最多 20 字，去掉檔名不允許的字元），把原文檔和 `.refined.md` 一起改名。已開啟的 view 用 `View.retarget()` 指向新路徑，先確認 4200 的 API 有這個方法；沒有的話就關閉再重開。

#### 頂端工具列（phantom）
- 在受管理檔案的 frontmatter 下方，用 `sublime.Phantom` 搭配 `LAYOUT_BLOCK` 顯示：`✨ AI 整理 ｜ 模式：clean ▾ ｜ 📋 送出 ｜ 🕘 歷史 ｜ ● 後端：線上`
- 點擊交給 `on_navigate` 處理，href 格式如 `refine`、`mode`、`send`、`history`。點「模式」時跳出 quick panel 選模式，選完就用該模式整理。
- 檔案內容變動時更新 phantom 的位置，因為 frontmatter 的長度可能改變。

#### 自動存檔
- `EventListener.on_modified_async` 做 debounce，`autosave_delay_ms` 之後執行 `view.run_command("save")`。
- 另外在 `on_deactivated` 時立即存檔。
- 只作用於受管理的檔案。

#### 圖片預覽
- `on_hover`：游標下的 token 是存在的圖片路徑（png、jpg、gif）時，`show_popup` 顯示 `<img src="file://...">`，寬度上限 400px。
- 如果 4200 不支援 `file://`，改用 `data:image/png;base64,...`，檔案超過 1 MB 時不預覽。

#### 後端連線
- 從 `%LOCALAPPDATA%\local-typeless\serve.json` 讀 port 和 token，每次請求都重讀，因為後端重啟後 token 會變。
- plugin 載入時呼叫 `/health`。離線且 `server.autostart_cmd` 不為空時，用 `subprocess.Popen` 啟動後端，並加上 `CREATE_NO_WINDOW`。
- 狀態列（`view.set_status("lt", ...)`）顯示 `LT ● dev`（線上）或 `LT ○ 離線`。

#### Frontmatter 格式
Sublime 的 Python 沒有 PyYAML，所以只用扁平的 `key: value`，在 `lt_core.py` 手寫解析，不支援巢狀結構：

```
---
created: 2026-11-20 14:32
status: draft
mode: clean
refined_at:
sent_at:
target:
---
```

### 4.5 啟動
- 新增 `LocalTypelessServe.vbs`：用 `pythonw -m local_typeless.cli serve` 在背景啟動、不開視窗，並附上加入「啟動」資料夾的說明。
- `scripts/install_sublime.ps1`：建立 Packages junction，若 prompts_root 不存在就建立。

## 5. 分階段實作與驗收

每個階段結束時都要：pytest 全數通過、用 Conventional Commit 提交，並請使用者手動驗收後再進下一階段。

### Phase 0：最小驗證版（約半天，**先讓使用者試用**）
- `serve`：`/health`、`/refine`（含 protect）、`/clipboard/capture`（只處理圖片）、token
- plugin：`lt_new_prompt`、自動存檔、`lt_smart_paste`（圖片轉路徑）、`lt_refine`（右側結果）、頂端 phantom 的「✨ AI 整理」
- **驗收**：使用者實際跑一輪「Win+H 講一段 → Win+Shift+S 截圖 → Ctrl+V 得到路徑 → 再講一段 → 點 AI 整理」。右側結果的路徑要原封不動，左側原文不能被改，檔案要自動存在 `prompts_root/年/月/`。

### Phase 1：後端完整化
- `/config`、熱重載、欄位層級容錯、`/clipboard/capture` 支援檔案、`protect.py` 的完整規則、NullSTT
- **測試**：
  - `tests/test_editor_protect.py`：各類路徑、程式碼區塊、URL、標記遺失或重複時回傳 None
  - `tests/test_editor_config.py`：預設值、`settings.local.yaml` 覆蓋、壞掉的欄位退回預設
  - `tests/test_editor_server.py`：用 fake pipeline 和 fake `ClipboardBackend` 啟動服務，測 token 401、Origin 拒絕、fallback 時 `ok=false`、圖片存檔路徑與命名

### Phase 2：plugin 完整化
- 所有 commands、右鍵選單、上方選單、Command Palette、模式選單、frontmatter 更新、`lt_send`、`lt_history`、hover 預覽、狀態列、autostart
- `tests/test_lt_core.py`：直接 import `sublime/LocalTypeless/lt_core.py`，測四種路徑格式與空格加引號、frontmatter 讀寫往返、layout 展開、標題 slug（含中文和非法字元）

### Phase 3：打磨
- `auto_rename_after_refine`
- `send.auto_paste_to_last_window`：後端新增 `/paste_to_previous`，沿用 `ui/draft_bridge.py` 的 `PreviousWindow` 和 `paste_to_previous` 邏輯。要記錄「Sublime 之前的前景視窗」，需要後端定期追蹤
- plugin 端在後端離線時也能貼檔案路徑：用 ctypes 讀 `CF_HDROP`，選做
- 文件：README 新增「Sublime 工作區」一節，`docs/設定手冊.md` 補上 `editor:` 所有欄位，`docs/安裝指南.md` 補上 plugin 安裝方式

## 6. 手動驗收清單（Windows，Sublime 4200）

- [ ] `Ctrl+Alt+N` 開新檔，從頭到尾不出現任何存檔對話框，檔案出現在正確的資料夾
- [ ] Win+H 口述的中英夾雜內容正常出現在 Sublime
- [ ] 截圖工具（Win+Shift+S）、瀏覽器「複製圖片」、PowerPoint 複製圖片：三種來源按 Ctrl+V 都得到 PNG 路徑，檔案確實存在
- [ ] 在檔案總管選 3 個檔案按 Ctrl+C，回 Sublime 按 Ctrl+V，得到 3 行路徑
- [ ] 一般文字照常貼上；非 prompt 的檔案按 Ctrl+V，行為跟原本一樣
- [ ] 四種 `path_style` 都正確；含空格的路徑有加引號
- [ ] AI 整理：路徑、URL、程式碼區塊原封不動，贅字被清掉，術語依 glossary 修正
- [ ] 選一段文字做 AI 整理，只有那段被處理
- [ ] 後端關閉時：智慧貼上退回一般貼上，AI 整理提示離線，Sublime 不卡住
- [ ] 改 `config.yaml` 的 `path_style` 並存檔後，不用重啟，下一次貼上就套用新格式
- [ ] 舊的 Tk 草稿視窗（`LocalTypeless.vbs`）照常可用
- [ ] `Ctrl+Alt+H` 列出歷史 prompt，選了就開

## 7. 給實作 agent 的規則

- 先讀 repo 的 README 和 `docs/設定手冊.md`，確認現況與本文件一致；有出入時以程式碼為準，並在 PR 說明。
- **不要改 `src/local_typeless/types.py`**（LOCKED）。
- 後端不新增相依套件，用 stdlib 的 `http.server`。plugin 只能用 Sublime 內建 Python 3.8 的 stdlib。
- 修改現有模組時以最小改動為原則；舊的 Tk UI 和 hotkey daemon 不能壞。
- 每個 Phase 開一個分支，例如 `feature/sublime-workspace-p0`；commit 用 Conventional Commits。
- token 和 port 檔不能 commit；`.gitignore` 要涵蓋 `prompts_root` 被設在 repo 內的情況。
- 以下事項不確定時，**先做最小實驗再實作**，並把結果記在 PR 裡：
  - 4200 的 `View.retarget`
  - minihtml 的 `file://` 圖片
  - phantom 的 `on_navigate`
  - Ctrl+V keymap context 能否覆蓋內建 paste

## 8. 尚待使用者確認（先用預設值，之後改設定即可）

| 項目 | 預設值 |
|---|---|
| `prompts_root` | `~/Documents/prompts`（建議改到 OneDrive 底下） |
| `path_style` | `windows`（agent 跑在 WSL 時改 `wsl`） |
| `default_mode` | `clean`（另可試 `prompt` 模式） |
