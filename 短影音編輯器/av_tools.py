"""
av_tools.py
-----------
new_project.py 和 render.py 共用的小工具：找 ffmpeg/ffprobe/ImageMagick
執行檔路徑、統一的 log 格式、SRT 時間戳格式化。全部集中在這裡，
避免兩支程式各自複製一份「找路徑」邏輯，路徑壞了也只要改一個地方。
"""

import datetime
import re
import shutil
import sys
from pathlib import Path

# 這支工具鏈同時會被 PowerShell 和 Git Bash 呼叫，兩者對「終端機期待的
# 輸出編碼」認知不一樣：早期版本這裡假設 Windows 主控台的 cp950（繁中
# Big5）codepage，PowerShell 底下大致還看得懂，但同樣的輸出丟進 Git
# Bash（本質上假設 UTF-8）看到的就是整串亂碼——實測踩過。統一鎖死
# UTF-8 輸出，兩邊都正常；極少數真的不支援 UTF-8 的舊主控台，靠
# errors="replace" 退而求其次顯示可顯示的部分，不要讓腳本直接崩潰。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # 某些非終端輸出目標（例如被重導向的管線）可能不支援 reconfigure，忽略即可

# 這台機器上已知的安裝位置（winget 裝的 ffmpeg、官方安裝檔裝的 ImageMagick）。
# PATH 沒設定好的時候當備援猜測用；PATH 裡找得到的話一律優先用 PATH 版本。
_KNOWN_FFMPEG_GUESS = (
    Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-9.0.1-full_build" / "bin" / "ffmpeg.exe"
)
_KNOWN_MAGICK_GUESS = Path("C:/Program Files/ImageMagick-7.1.2-Q16-HDRI/magick.exe")

# 燒字幕/標題卡用的預設中文字型（Windows 內建，粗體，繁簡都能顯示）。
DEFAULT_FONT_BOLD = "C:/Windows/Fonts/msjhbd.ttc"


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # 上面已經把 stdout 鎖成 UTF-8，這裡理論上不會再觸發；保留只是
        # 防呆，萬一遇到極少數 reconfigure 失敗的環境也不要讓腳本崩潰。
        encoding = sys.stdout.encoding or "utf-8"
        print(line.encode(encoding, errors="replace").decode(encoding), flush=True)


def resolve_cli_path(raw: str) -> Path:
    """
    命令列路徑參數的正規化，取代直接寫 `Path(raw).expanduser().resolve()`。

    這支工具鏈同時會被 PowerShell 和 Git Bash 呼叫。Git Bash（MSYS）平常
    會自動把 POSIX 風格路徑（例如 /c/Users/x）轉成 Windows 路徑再傳給
    非 MSYS 的執行檔（像 python.exe），但遇到含空白／中文／方括號的長
    路徑時常常轉換失敗，導致 "/c/Users/x" 原封不動傳進 Python。Python 在
    Windows 上看到開頭的 "/" 會當成「目前所在磁碟的根目錄」，把
    "/c/Users/x" 解析成 "C:\\c\\Users\\x"（多套了一層假的 "c" 資料夾）
    ——這支工具鏈已經因為這個原因炸過一次「找不到影片檔案」。

    這裡偵測這個特徵（開頭是 /<單一字母>/，照原本邏輯解析出來的路徑不
    存在），試著改用「該字母當磁碟代號」重新解析，找得到才採用；找不到
    就維持原本的解析結果，讓呼叫端原有的「檔案不存在」錯誤訊息照常顯示
    ——不要在這裡吞掉真正的路徑打錯，這個修正只處理「Git Bash 轉換失敗」
    這一種已知情況。
    """
    original = Path(raw).expanduser().resolve()
    if original.exists():
        return original
    m = re.match(r"^/([A-Za-z])/(.+)$", raw)
    if m:
        drive, rest = m.group(1), m.group(2)
        corrected = Path(f"{drive.upper()}:/{rest}").expanduser().resolve()
        if corrected.exists():
            log(f"[路徑修正] 偵測到 Git Bash 風格路徑沒有正確轉換成 Windows 路徑："
                f"\"{raw}\" 被誤判成 \"{original}\"，已自動改用 \"{corrected}\"。"
                f"建議之後改用 PowerShell 執行本工具、或直接用 Windows 風格路徑"
                f"（例如 C:\\Users\\...）當參數，避免每次都要靠這個修正。")
            return corrected
    return original


def find_ffmpeg_cmd() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if _KNOWN_FFMPEG_GUESS.exists():
        return str(_KNOWN_FFMPEG_GUESS)
    log("找不到 ffmpeg，請確認已安裝並加入 PATH。")
    sys.exit(1)


def find_ffprobe_cmd() -> str:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    guess = _KNOWN_FFMPEG_GUESS.parent / "ffprobe.exe"
    if guess.exists():
        return str(guess)
    log("找不到 ffprobe，請確認已安裝並加入 PATH。")
    sys.exit(1)


def find_magick_cmd() -> str:
    exe = shutil.which("magick")
    if exe:
        return exe
    if _KNOWN_MAGICK_GUESS.exists():
        return str(_KNOWN_MAGICK_GUESS)
    log("找不到 ImageMagick（magick），請確認已安裝並加入 PATH。")
    sys.exit(1)


def find_whisper_cmd() -> str:
    """openai-whisper CLI 的位置（faster-whisper/Groq 都沒有時的最後備援）。"""
    import os

    exe = shutil.which("whisper")
    if exe:
        return exe
    guess = (
        Path.home() / "AppData" / "Roaming" / "Python"
        / f"Python{sys.version_info.major}{sys.version_info.minor}" / "Scripts" / "whisper.exe"
    )
    if guess.exists():
        return str(guess)
    log("找不到 whisper 指令。")
    log("請確認已執行 pip install -U openai-whisper，")
    log("並確認安裝路徑（通常是 %APPDATA%\\Python\\Python3xx\\Scripts）已加入 PATH。")
    sys.exit(1)


def srt_timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ffprobe_duration(ffprobe_cmd: str, media_path: Path) -> float:
    import subprocess

    result = subprocess.run(
        [ffprobe_cmd, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(media_path)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())
