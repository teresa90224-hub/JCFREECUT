# 短影音自動剪輯工具包

這個資料夾是把長版錄影（會議紀錄、教學錄影）自動剪成 9:16 短影音的
工具鏈。**完整操作手冊在**
`C:\Users\Jessie\.claude\skills\short-video-cut\SKILL.md`——那份是
給 AI agent 看的判斷邏輯／標準流程／踩過的坑，這裡不重複貼一份（會
跟那邊改動脫節），**任何跟這個工具包有關的任務，動手之前先完整讀過
那份檔案再開始**，裡面涵蓋：

- 三層架構（辨識層轉字幕／判斷層決定剪輯點／執行層跑 ffmpeg）
- 開工前要跟使用者一次問清楚的三件事（剪輯模式、主題、B-roll 要不
  要配及密集程度）
- `new_project.py` → 判斷寫 `edit_state.json` → `render.py` 出片 →
  `verify_render.py` 驗證 → 使用者確認後開 `caption_editor/` 網頁
  GUI 微調，整條標準流程
- 大量已經踩過、寫進去避免重踩的坑（語意重複判斷、片段起訖點核對、
  字幕同步驗證方法等）

`edit_state.json` 的 schema 範例在同資料夾的
[edit_state.example.json](edit_state.example.json)，技術細節（ASS
顏色格式、Windows 換行符號陷阱等）在 [README.md](README.md)「已知的
技術眉角」。

這個工具包本身跟任何特定 AI agent 無關，純粹是 Python + ffmpeg +
ImageMagick + Groq API 的命令列腳本，Claude Code、Codex 或任何能跑
shell 指令的 agent 都能執行——差別只在「判斷層」（讀逐字稿、決定剪
哪裡）需要 agent 自己讀懂 `SKILL.md` 的邏輯去做判斷，不是靠固定程式
碼自動完成。

**維護守則**：`SKILL.md` 是唯一詳細操作手冊，這份 `AGENTS.md` 只是
入口指標，不要把 `SKILL.md` 的內容複製貼進這裡——維護兩份容易寫到
一半漏改另一份，之前就發生過技能檔跟實作脫節的狀況。
