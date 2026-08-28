@echo off
chcp 65001 >nul
echo ===============================================
echo   JCFREECUT 短影音編輯器 - 環境設定精靈
echo ===============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 Python。
    echo 請先到 https://www.python.org/downloads/ 安裝 Python 3.10 以上版本，
    echo 安裝時記得勾選「Add python.exe to PATH」，裝完後重新執行這個檔案。
    pause
    exit /b 1
)
echo [OK] 已找到 Python。

echo.
echo [1/4] 安裝 Python 套件（flask / groq / faster-whisper / opencc）...
pip install -r requirements.txt
if errorlevel 1 (
    echo [警告] 套件安裝過程有錯誤，請往上捲看是哪個套件失敗，
    echo         通常重跑一次「pip install -r requirements.txt」就會好。
)

echo.
echo [2/4] 檢查 ffmpeg（剪片/燒字幕引擎）...
where ffmpeg >nul 2>nul
if errorlevel 1 (
    where winget >nul 2>nul
    if errorlevel 1 (
        echo [錯誤] 找不到 ffmpeg，也找不到 winget 可以自動安裝。
        echo 請手動到 https://www.gyan.dev/ffmpeg/builds/ 下載安裝，
        echo 並把 ffmpeg.exe 所在資料夾加進系統 PATH。
    ) else (
        echo 找不到 ffmpeg，正在用 winget 自動安裝...
        winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    )
) else (
    echo [OK] 已找到 ffmpeg。
)

echo.
echo [3/4] 檢查 ImageMagick（字卡產生用）...
where magick >nul 2>nul
if errorlevel 1 (
    where winget >nul 2>nul
    if errorlevel 1 (
        echo [錯誤] 找不到 ImageMagick，也找不到 winget 可以自動安裝。
        echo 請手動到 https://imagemagick.org/script/download.php#windows 下載安裝。
    ) else (
        echo 找不到 ImageMagick，正在用 winget 自動安裝...
        winget install --id ImageMagick.ImageMagick -e --accept-package-agreements --accept-source-agreements
    )
) else (
    echo [OK] 已找到 ImageMagick。
)

echo.
echo [4/4] 設定 API Key
echo -----------------------------------------------
echo 轉字幕需要 Groq 的免費 API Key，還沒申請的話先到：
echo   https://console.groq.com/keys
echo 申請完再貼過來這裡（貼上後畫面上看得到，注意旁邊有沒有人）。
echo 不想現在設定的話，直接按 Enter 跳過，之後可以再手動用 setx 設定。
echo -----------------------------------------------
set /p GROQKEY="貼上 GROQ_API_KEY: "
if not "%GROQKEY%"=="" (
    setx GROQ_API_KEY "%GROQKEY%" >nul
    echo 已設定 GROQ_API_KEY。
) else (
    echo 已跳過，之後可以自己執行：setx GROQ_API_KEY "你的key"
)

echo.
echo 如果想要影片自動配 B-roll 補充畫面，還可以設定 Pexels 的免費 API Key：
echo   https://www.pexels.com/api/
echo 不需要的話直接按 Enter 跳過。
set /p PEXELSKEY="貼上 PEXELS_API_KEY（可略過）: "
if not "%PEXELSKEY%"=="" (
    setx PEXELS_API_KEY "%PEXELSKEY%" >nul
    echo 已設定 PEXELS_API_KEY。
)

echo.
echo ===============================================
echo 設定完成！
echo 重要：環境變數要「重新開一個新的終端機視窗」才會生效，
echo       這個視窗接下來設定的東西在這個視窗裡看不到效果。
echo.
echo 下一步：關掉這個視窗，重新開一個終端機，
echo         cd 到這個資料夾，開 Claude Code，
echo         跟它說「幫我剪這支影片」就可以開始了。
echo ===============================================
pause
