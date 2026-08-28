"""
av_tools.py
-----------
new_project.py 和 render.py 共用的小工具：找 ffmpeg/ffprobe/ImageMagick
執行檔路徑、統一的 log 格式、SRT 時間戳格式化。全部集中在這裡，
避免兩支程式各自複製一份「找路徑」邏輯，路徑壞了也只要改一個地方。
"""

import datetime
import shutil
import sys
from pathlib import Path

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
    print(f"[{ts}] {msg}", flush=True)


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
