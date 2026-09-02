# 短影音編輯器

把一支長版錄影（會議紀錄、教學錄影）自動剪成 9:16 短影音的工具鏈。分兩層：

- `new_project.py`：建專案 + 轉字幕（AI 判斷層之前的準備工作）
- `render.py`：讀一份 `edit_state.json`，輸出最終 mp4（實際剪輯/燒字幕/合成引擎）

中間「要剪哪幾段、標題怎麼下、哪句是金句」是 AI（Claude）讀逐字稿之後的判斷，寫進
`edit_state.json`，`render.py` 照著執行——所以同一份 `edit_state.json` 不管是 AI
第一次生成、還是之後手動調整過，呼叫 `render.py` 都會得到一致的結果。

## 快速上手（Windows）

**最簡單的方式：下載這個 repo（Code → Download ZIP，或 `git clone`）解壓縮後，
直接雙擊執行 `setup.bat`**：

```bash
git clone https://github.com/teresa90224-hub/JCFREECUT.git
cd JCFREECUT
```

雙擊 `setup.bat` 會自動幫你：

1. 檢查 Python、跑 `pip install -r requirements.txt` 裝好套件
2. 檢查 ffmpeg／ImageMagick，沒有的話用 winget 自動安裝（電腦上沒有 winget
   的話會提示你手動下載連結）
3. 引導你貼上 Groq（必要，轉字幕用）跟 Pexels（選用，抓 B-roll 用）的
   免費 API key，自動用 `setx` 設成系統環境變數——**這一步只會存在你自己
   電腦的環境變數裡，不會傳到任何地方，也不會進到這個 repo**

跑完 `setup.bat` 之後：

1. **重新開一個新的終端機視窗**（環境變數要重開才會生效）
2. 用 [Claude Code](https://claude.com/claude-code) 在這個資料夾底下開一個
   對話，跟它說「幫我剪這支影片」並附上影片路徑——`.claude/skills/
   short-video-cut/` 裡的技能檔會自動被載入，Claude 接下來會照著技能檔的
   流程幫你問清楚需求、轉字幕、判斷剪輯點、出片。
3. 也可以不透過 Claude，直接手動跑：見下方「標準流程」的三行指令。
4. 出片之後想手動微調字幕/標題，開 `python 短影音編輯器/caption_editor/server.py`，
   瀏覽器打開 http://localhost:8770。

沒有要用 `setup.bat`、想自己手動裝的話，照下面「環境需求」跟
「API Key 怎麼設定」自己一步步來也可以。

## 環境需求

- **ffmpeg**、**ffprobe**（PATH 裡要找得到，或裝在 winget 常見安裝路徑，見
  `av_tools.py` 的 `find_ffmpeg_cmd`）
- **ImageMagick**（`magick` 指令，目前只用在早期版本，`render.py` 現在的字幕
  改用 ffmpeg 內建的 `libass`，不再依賴 ImageMagick 燒字幕，但 `find_magick_cmd`
  還留著給標題/CTA 卡用）
- **auto-editor**（剪掉停頓用）
- 轉字幕三選一，`new_project.py` 會依序自動偵測、優先序如下：
  1. **Groq API**（設定環境變數 `GROQ_API_KEY` 才會用，最快，見下方說明）
  2. **faster-whisper**（本機 CPU，`pip install faster-whisper`）
  3. **openai-whisper CLI**（最後備援，`pip install -U openai-whisper`）
- B-roll 素材用 **Pexels API**（設定環境變數 `PEXELS_API_KEY` 才會用，非必要）
- **opencc-python-reimplemented**（`pip install opencc-python-reimplemented`）：
  把 Whisper/Groq 輸出的簡體字轉繁體，沒裝也不會擋住轉錄，只是字幕會是簡體
- **flask**（`pip install flask`）：`caption_editor/` 網頁編輯器要用

### API Key 怎麼設定

不要把 key 貼在跟 AI 助理的對話裡。改用系統環境變數，開一個新的終端機視窗執行：

```powershell
setx GROQ_API_KEY "你的key"
setx PEXELS_API_KEY "你的key"
```

設定完**要開新的終端機視窗才會生效**（不會影響已經開著的舊視窗/session）。

## 標準流程

實際的程式碼在 `短影音編輯器/` 這個子資料夾裡，下面的指令都要先 `cd` 進去：

```bash
cd 短影音編輯器

# 1. 建專案 + 轉字幕（含逐句 .srt 跟逐字時間戳 .words.json）
python new_project.py "path/to/原始影片.mp4" --name "專案名稱"

# 2. 判斷剪輯點、寫 06_meta/edit_state.json（AI 或人工）
#    schema 範例見 edit_state.example.json

# 3. 依 edit_state.json 產生最終影片
python render.py "projects/專案名稱/06_meta/edit_state.json"

# 4. 出片後驗證字幕有沒有跟語音對不上（需要 GROQ_API_KEY）
python verify_render.py "projects/專案名稱/06_meta/edit_state.json"
```

輸出在 `projects/<專案名稱>/05_render/`。`verify_render.py` 會把最新那支
成品重新轉錄一次，拿實際聽得到的逐字時間去跟 `subtitles[]` 宣稱的時間
比對，超過 0.4 秒差距就列出來——每次出片後都建議跑一次，比人工聽過
一遍更容易抓到不明顯的偷跑/搶拍。

### 轉錄後的人工校對

Whisper/Groq 難免會有同音字辨識錯誤（尤其是專有名詞、統編、公司名這類
詞），AI 判斷剪輯點時不會自動抓出來改。**寫 `edit_state.json` 前，先過
一遍 `.srt` 找明顯的辨識錯誤**（例如「簽誠」應為「簽呈」、「同編」應為
「統編」）。不確定原音在講什麼的地方，抓一小段音檔片段（`ffmpeg -ss
<start> -to <end> -i 來源 -c copy 片段.mp4`）讓使用者確認，不要用猜的
直接改。

## 用網頁 GUI 手動微調（caption_editor/）

AI 判斷剪輯點/字幕/主題已經夠可靠，但同音字校正、斷句時間點微調、排版
跟字卡文字這類細節，還是需要人工手動調整比較快。`caption_editor/` 是
一個本機網頁小工具，直接讀寫 `edit_state.json`（跟 `render.py` 共用同一份
schema，不是另一套格式），改完可以在網頁上直接按「產生影片」呼叫
`render.py` 出片。

```bash
cd 短影音編輯器
python caption_editor/server.py
# 開瀏覽器 http://localhost:8770
```
![edit_state 編輯器截圖](./edit_state%20編輯器_1.png)

### 「draft → approved」：GUI 只給看得到成品的人挑

`edit_state.json` 的 `status` 欄位預設是 `"draft"`——**GUI 選單只列出
`status == "approved"` 的檔案**，不是資料夾裡隨便一個 `edit_state*.json`
都會出現。用意是：GUI 是拿來微調「使用者已經看過、確認可以」的成品，
不是拿來從一堆 AI 實驗/測試用的中間檔裡亂挑。

標準流程是：
1. Claude 用 short-video-cut 技能剪出一版、寫出 `edit_state.json`
   （`status` 維持 `"draft"`），先算一次成片給使用者看。
2. 使用者看過確認可以了，Claude 才把該檔案的 `status` 改成
   `"approved"`——這一步不是自動的，Claude 自己測試/實驗用的
   `edit_state` 檔案要一直留在 `"draft"`，不要手滑改成 approved
   混進使用者要挑的清單。
3. 之後使用者才會在 GUI 選單裡看到這個專案，可以自己微調字幕/排版/
   字卡再重新出片。

### 目前支援

- **字幕（逐字編輯，唯一資料來源）**：點字直接改文字（校正同音字）、
  紅色×刪字（連影片/聲音一起剪掉）、藍色｜在字後面斷句、拖曳紅色
  「/」斷句點移動位置、點「/」旁邊的×合併回上一句、★設金句、✎整句
  重打（不用逐字改，仍維持原本這句的起訖時間）。改完按「套用到字幕／
  剪輯點」才會真的算出新的 `clips[]`/`subtitles[]`/`broll[]`（時間會
  跟著剪掉的字自動平移，不會跟語音對不上），再按「存檔」寫回檔案。
- **排版**：畫面比例（9:16 / 16:9）、背景顏色。
- **字卡內容**：標題卡（title）、結尾卡（CTA）的文字內容（字級/顏色/
  背景目前仍只能改 `edit_state.json` 裡的對應欄位，GUI 這版先不開放）。

不支援（仍然要手動改 JSON 或請 AI 判斷）：B-roll、配樂、`subtitle_style`
的字級/顏色/字型。

`title`/`cta` 的 `text` 可以用 `\n` 分兩行，兩行可以各自上色（第一行用
`color`、第二行用 `color2`，沒給 `color2` 就跟第一行同色）——像新聞標題
那種「白字+紅字」的樣式。每一行預設都會有黑色描邊（`border`，預設
`{color:"#000000", width:4}`）＋往右下偏移的半透明黑色陰影
（`shadow_offset`，預設 4px，設 0 關掉陰影）。

逐字編輯需要來源影片自己的 `.words.json`（時間軸要對應 `state.source.
video_path` 這個檔案自己，不能借用建專案時對原始錄影做的那份）。GUI
會自動找 `02_transcript/` 或影片旁邊有沒有現成的，沒有才會轉錄一次。

**title/cta/subtitles 的文字不要放 emoji**——字卡是用 ImageMagick 配單一
中文字型（微軟正黑體）畫出來的，這個字型沒有 emoji 圖案，emoji 會直接
消失不顯示；就算換成 Windows 內建的 emoji 字型，中文字又會反過來消失
（兩者是不同字型，這台機器沒有做多字型 fallback 拼接）。這台的
ImageMagick 也只能畫黑白單色 emoji，不是彩色的，所以就算之後要做
fallback 拼接，效果也有限。

### 存檔自動備份

每次按「存檔」，實際覆寫檔案之前會先把目前內容複製一份到
`06_meta/.backups/<檔名>.<時間戳>.json`，保留最近 10 份。逐字編輯器
是在瀏覽器裡操作、沒有人在旁邊核對每一步的流程，誤觸按鈕把字幕時間
弄亂又直接存檔覆蓋掉是真的發生過的情況——有這份備份，事後可以直接
把 `.backups/` 裡最近一份正常的檔案複製回 `edit_state.json` 復原，
不用重新剪一次。

## 兩種剪輯模式

`edit_state.json` 的 `clips[]` 本身沒有限制片段怎麼選，差別在「AI 判斷時用什麼邏輯」：

- **行銷短影音**：抓 3-5 個最精簡有力的片段（可以互相不連續），拼成 30-60 秒的
  重點精華版，故事線要完整（問題→構想→解法→結論這種節奏）。
- **教育版本**：先分析逐字稿有哪些主題，讓使用者多選要哪幾個主題，選定的主題
  **保留完整解說**（不抓重點），只剪掉停頓、明顯贅詞/重複、離題的閒聊——長度
  通常會到幾分鐘，不是短影音的長度。

兩種都是同一套 `clips[]` + `render.py` 機制，差別只在「AI 怎麼決定要保留哪些
時間區間」，不需要不同的程式碼路徑。

## edit_state.json 重點欄位

完整範例見 `edit_state.example.json`。幾個容易搞混的地方：

- `clips[]`：`{start, end, keep, reason}`，`keep=false` 的片段會被跳過。不管是
  「剪掉停頓」還是「抓不連續的重點片段」，邏輯完全一樣，都是很多小段
  `keep=true`/`false` 交錯。
- `subtitles[]`：**整句字幕卡**，不是逐字時間戳（曾經做過逐字 karaoke 即時
  變色，使用者確認不需要，已經拿掉——不要重新加回去，除非明確被要求）。
  格式是 `{text, start, end, emphasis}`：
  - `emphasis: true`（金句/重點句）→ 用 `subtitle_style.emphasis_size`、
    `emphasis_color`，還會自動斜體。
  - `emphasis: false`（一般句）→ 用 `subtitle_style.size`、`color`。
  - `text` 建議直接沿用 Whisper/Groq 切好的**短句**（通常 1-2 秒一句），不要
    自己手動合併成長段落——合併過的長句在畫面上停留太久，會跟語音對不上
    （實測踩過這個坑，使用者明確回報過）。
  - 中文沒有空白可以斷字，`render.py` 的 `_auto_wrap_cjk()` 會依畫面寬度跟
    字級自動幫長句插入換行（ASS 格式的自動換行是設計給西方文字用的，純中文
    長句不會自動換行，這是 libass 的已知限制，已經處理掉，不用再擔心這個）。
- `broll[]` / `music`：都要「真的有素材路徑」才會套用。B-roll 是 Claude 用
  Pexels API 幫忙找、下載到 `03_broll/`；配樂要使用者自己準備音檔給 Claude。
  兩者都是空的話這兩個欄位就留空陣列/`null`，不會出錯。

## 已知的技術眉角（省得重踩）

- **ASS 顏色是 `&HBBGGRR&`**（藍綠紅，跟一般網頁 `#RRGGBB` 反過來）。
- **這台機器（如果同一台）沒有 NVIDIA GPU**，faster-whisper 只能用 CPU int8，
  Groq API 因此是預設優先選項，比本機任何方案都快很多。
- Windows 上 Python 文字模式寫檔預設會把 `\n` 存成 `\r\n`，如果那個檔案後面
  要被 ffmpeg 濾鏡或 shell 逐行讀取，記得用 `newline='\n'` 或讀取時
  `tr -d '\r'`，不然參數會被吃掉一截，出現看起來莫名其妙的錯誤。
- **`setx` 設定的 `GROQ_API_KEY`／`PEXELS_API_KEY`，Claude Code 的 Bash
  session 常常吃不到**：`setx` 寫的是 Windows 使用者層級環境變數，只有
  **新開的**終端機行程才會繼承到；如果 Bash session 是在使用者 `setx`
  之前就已經開著（例如整個對話一開始就開著），`os.environ` 裡就是空的，
  `new_project.py` 會誤判成沒設定，直接 fallback 去用本機 faster-whisper
  （慢很多）。**用之前不要先假設它沒設定**：先跑
  `powershell.exe -NoProfile -Command '[Environment]::GetEnvironmentVariable("GROQ_API_KEY","User")'`
  確認使用者層級到底有沒有值。有的話用這個指令動態抓進當次指令的環境變數
  再跑 `new_project.py`（例如
  `export GROQ_API_KEY="$(powershell.exe -NoProfile -Command '[Environment]::GetEnvironmentVariable(\"GROQ_API_KEY\",\"User\")' | tr -d '\r')"`），
  不要重開一個新終端機再等使用者手動重跑。**注意：金鑰字串本身不要直接
  打在 Bash 指令裡**（例如 `export GROQ_API_KEY="gsk_..."` 這種字面值），
  Claude Code 的 auto mode classifier 會擋下含明文密鑰的指令；一定要用
  上面這種「執行當下才動態查詢」的寫法，指令文字本身不含金鑰明文。
- **Whisper/Groq 的中文輸出預設是簡體字**，這台工具鏈給台灣使用者用，
  `new_project.py` 的 `_write_srt` / `_write_words_json` 已經統一用
  `opencc`（`s2twp` 設定，簡轉繁＋台灣慣用詞）轉過一次再存檔，所以
  `new_project.py` 產生的 `.srt` / `.words.json` 都已經是繁體，不用
  再另外轉。如果看到某個 `.srt`/`.words.json` 是簡體字，代表它是在
  這個轉換邏輯補上之前產生的舊檔，重新轉錄一次或手動跑
  `opencc.OpenCC('s2twp').convert(...)` 補救即可。
