#!/usr/bin/env python3
"""
new_project.py
---------------
短影音自動剪輯工具鏈的「建專案」腳本（Windows 版）。

用法：
    python new_project.py "C:\\path\\to\\原始影片.mp4" --name "專案名稱"

它會做的事：
    1. 在 projects/ 底下建立這一集的資料夾，內含固定的六個子資料夾
       （對應影片中「11:40 剪完的檔案怎麼管理」講的結構）：
         01_source     原始素材（複製一份原始影片進來，不動原檔）
         02_transcript Whisper 轉出來的 SRT／逐字稿
         03_broll      抓回來的 B-roll 素材（Pexels）
         04_cuts       Auto-Editor 剪完停頓的中間檔
         05_render     燒字幕、字卡、配樂後的最終成品
         06_meta       這一集的設定紀錄（模板、主題、時間軸決策等）
    2. 把原始影片複製一份到 01_source/ 下，保留原檔不動。
    3. 呼叫本機安裝的 Whisper，把語音轉成 SRT 字幕，存到 02_transcript/。
       （這一步全部在本機 CPU/GPU 跑，不會呼叫任何雲端 API，也不吃
        Claude 的 token —— 這是影片裡特別強調的重點。）
    4. 寫一份 project.json 記錄這次的專案資訊，讓 Claude Code 之後
       可以讀取它，接續判斷怎麼剪。

這支腳本只負責「建專案＋轉字幕」，實際怎麼剪（挑片段、貼字幕、配
B-roll、配樂）是交給 short-video-cut 這個 Skill 裡的規則，由 Claude
邊讀 SRT 邊呼叫 ffmpeg / Auto-Editor / Pexels API 完成 —— 對應影片
裡「運作邏輯分三層，只有中間判斷的部分會吃 token」的說法。
"""

import argparse
import datetime
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from av_tools import find_ffmpeg_cmd, find_ffprobe_cmd, find_whisper_cmd, ffprobe_duration, log, srt_timestamp

# 六個固定子資料夾，順序即建立順序
SUBDIRS = [
    "01_source",
    "02_transcript",
    "03_broll",
    "04_cuts",
    "05_render",
    "06_meta",
]

# 專案都建立在這支腳本所在資料夾旁邊的 projects/ 底下
TOOLS_DIR = Path(__file__).resolve().parent
PROJECTS_ROOT = TOOLS_DIR / "projects"

# Whisper 設定：模型大小可依電腦效能調整（tiny/base/small/medium/large）
# 先用 base，速度快很多；之後想要更準確的字幕，可以改成 small 或 medium。
WHISPER_MODEL = "base"
WHISPER_LANGUAGE = "zh"  # 中文影片；英文內容可改 "en" 或拿掉這個參數用自動偵測

# Groq 雲端轉字幕設定：有設定 GROQ_API_KEY 環境變數時優先使用（最快）。
# turbo 版便宜又快，一般剪輯用途準確度已經夠。
GROQ_MODEL = "whisper-large-v3-turbo"
# 免費版帳號檔案上限 25MB，這裡抓 22MB 當安全門檻，留一點餘裕。
GROQ_MAX_CHUNK_BYTES = 22 * 1024 * 1024
# 壓縮音軌用的 bitrate（kbps）。人聲用 opus 在低 bitrate 下辨識度仍然夠好。
GROQ_AUDIO_BITRATE_KBPS = 24

try:
    from opencc import OpenCC
    _OPENCC = OpenCC("s2twp")  # 簡體轉繁體（台灣慣用詞），Groq/Whisper 中文輸出預設是簡體
except ImportError:
    _OPENCC = None


def _to_traditional(text: str) -> str:
    """
    Whisper/Groq 的中文輸出預設是簡體字，這台工具鏈給台灣使用者用，
    字幕跟逐字時間戳都要統一轉成繁體（含用詞在地化，例如「軟件」→
    「軟體」），不然同一個專案裡不同檔案繁簡混用會很奇怪。找不到
    opencc 套件時原樣放行，不擋住整個轉錄流程。
    """
    return _OPENCC.convert(text) if _OPENCC else text


def make_project_dirs(project_dir: Path) -> None:
    for sub in SUBDIRS:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)
    log(f"已建立專案資料夾結構：{project_dir}")


def copy_source_video(video_path: Path, project_dir: Path) -> Path:
    dest = project_dir / "01_source" / video_path.name
    if dest.exists():
        log(f"01_source 已有同名檔案，略過複製：{dest.name}")
    else:
        log("複製原始影片到 01_source/ ...（檔案較大時請耐心等候）")
        shutil.copy2(video_path, dest)
    return dest


def _write_srt(srt_path: Path, segments: list[dict]) -> None:
    with srt_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(f"{i}\n")
            f.write(f"{srt_timestamp(seg['start'])} --> {srt_timestamp(seg['end'])}\n")
            f.write(f"{_to_traditional(seg['text'].strip())}\n\n")


def _write_words_json(words_path: Path, words: list[dict]) -> None:
    payload = [{"word": _to_traditional(w["word"]), "start": round(w["start"], 3), "end": round(w["end"], 3)} for w in words]
    words_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_whisper_faster(source_video: Path, transcript_dir: Path) -> dict:
    """
    用 faster-whisper（CTranslate2 後端）在 CPU 上跑轉字幕，同時輸出
    逐句 SRT（供人工校對）與逐字時間戳 words.json（供 edit_state.json /
    未來編輯器的卡拉OK字幕、微調時間軸用）。
    這台機器沒有 NVIDIA GPU，量化過的 faster-whisper 實測比原本的
    openai-whisper CLI 快約 2-4 倍，且不用上傳雲端、不吃 Claude token。
    """
    from faster_whisper import WhisperModel

    log(f"開始跑 faster-whisper 轉字幕（模型：{WHISPER_MODEL}，CPU int8，含逐字時間戳），這步不耗 Claude token...")
    log("影片較長時這一步仍需要一些時間，請耐心等候，不要中斷。")
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(source_video), language=WHISPER_LANGUAGE, word_timestamps=True)

    seg_list = []
    words = []
    for seg in segments:
        seg_list.append({"start": seg.start, "end": seg.end, "text": seg.text})
        for w in (seg.words or []):
            words.append({"word": w.word.strip(), "start": w.start, "end": w.end})

    srt_path = transcript_dir / f"{source_video.stem}.srt"
    words_path = transcript_dir / f"{source_video.stem}.words.json"
    _write_srt(srt_path, seg_list)
    _write_words_json(words_path, words)

    log(f"字幕已產生：{srt_path}")
    log(f"逐字時間戳已產生：{words_path}")
    return {"srt": srt_path, "words": words_path}


def run_whisper_legacy(source_video: Path, transcript_dir: Path) -> dict:
    """
    退回原本的 openai-whisper CLI 做法（faster-whisper 沒裝時的備援）。
    用 --word_timestamps True --output_format json 一次拿到逐句與逐字
    資料，自己從 json 轉出 srt 跟 words.json，不用跑兩次 whisper。
    """
    whisper_cmd = find_whisper_cmd()
    log(f"開始跑 Whisper 轉字幕（模型：{WHISPER_MODEL}，含逐字時間戳），這步不耗 Claude token...")
    log("影片較長時這一步可能要跑數分鐘，請耐心等候，不要中斷。")
    cmd = [
        whisper_cmd,
        str(source_video),
        "--model", WHISPER_MODEL,
        "--language", WHISPER_LANGUAGE,
        "--word_timestamps", "True",
        "--output_format", "json",
        "--output_dir", str(transcript_dir),
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        log(f"Whisper 執行失敗（exit code {e.returncode}）")
        sys.exit(e.returncode)

    raw_json_path = transcript_dir / f"{source_video.stem}.json"
    srt_path = transcript_dir / f"{source_video.stem}.srt"
    words_path = transcript_dir / f"{source_video.stem}.words.json"
    if not raw_json_path.exists():
        log("警告：預期的 whisper json 輸出未產生，請檢查 Whisper 輸出。")
        return {"srt": srt_path, "words": words_path}

    raw = json.loads(raw_json_path.read_text(encoding="utf-8"))
    seg_list = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in raw.get("segments", [])]
    words = []
    for s in raw.get("segments", []):
        for w in s.get("words", []):
            words.append({"word": w["word"].strip(), "start": w["start"], "end": w["end"]})

    _write_srt(srt_path, seg_list)
    _write_words_json(words_path, words)
    log(f"字幕已產生：{srt_path}")
    log(f"逐字時間戳已產生：{words_path}")
    return {"srt": srt_path, "words": words_path}


def _extract_compressed_audio(ffmpeg_cmd: str, source_video: Path, out_path: Path) -> None:
    """把影片音軌抽出來，壓成低 bitrate 單聲道 opus，縮小上傳體積用。"""
    cmd = [
        ffmpeg_cmd, "-y", "-i", str(source_video),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libopus", "-b:a", f"{GROQ_AUDIO_BITRATE_KBPS}k",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _split_audio_into_chunks(ffmpeg_cmd: str, audio_path: Path, chunk_dir: Path) -> list[tuple[Path, float]]:
    """
    音檔太大時依時間切段，回傳 [(chunk路徑, 這段在原始音檔裡的起始秒數), ...]。
    切段大小依 bitrate 反推，讓每段都在 GROQ_MAX_CHUNK_BYTES 以下。
    """
    size_bytes = audio_path.stat().st_size
    if size_bytes <= GROQ_MAX_CHUNK_BYTES:
        return [(audio_path, 0.0)]

    total_duration = ffprobe_duration(find_ffprobe_cmd(), audio_path)
    bytes_per_sec = size_bytes / total_duration
    chunk_seconds = max(60.0, math.floor(GROQ_MAX_CHUNK_BYTES / bytes_per_sec))

    log(f"壓縮後音檔約 {size_bytes / 1024 / 1024:.1f}MB，超過 Groq 免費版單檔上限，"
        f"依每段約 {chunk_seconds:.0f} 秒切段上傳...")

    chunk_pattern = chunk_dir / "chunk_%03d.ogg"
    cmd = [
        ffmpeg_cmd, "-y", "-i", str(audio_path),
        "-f", "segment", "-segment_time", str(int(chunk_seconds)),
        "-c", "copy", str(chunk_pattern),
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    chunks = sorted(chunk_dir.glob("chunk_*.ogg"))
    return [(c, i * chunk_seconds) for i, c in enumerate(chunks)]


def run_whisper_groq(source_video: Path, transcript_dir: Path) -> dict:
    """
    用 Groq 雲端 API（whisper-large-v3-turbo）轉字幕，request 時一併要
    word 級 timestamp_granularities，同時拿到逐句與逐字時間戳。
    需要環境變數 GROQ_API_KEY（SDK 會自動讀取，這支程式不會碰到 key 本身）。
    免費版帳號單檔 25MB 上限，這裡自動壓縮音軌＋必要時切段上傳再合併時間軸。
    """
    from groq import Groq

    ffmpeg_cmd = find_ffmpeg_cmd()
    client = Groq()  # 從 GROQ_API_KEY 環境變數讀取

    with tempfile.TemporaryDirectory(prefix="groq_audio_") as tmp:
        tmp_dir = Path(tmp)
        audio_path = tmp_dir / "audio.ogg"
        log("抽取並壓縮音軌準備上傳 Groq...")
        _extract_compressed_audio(ffmpeg_cmd, source_video, audio_path)

        chunks = _split_audio_into_chunks(ffmpeg_cmd, audio_path, tmp_dir)
        log(f"開始呼叫 Groq API 轉字幕（模型：{GROQ_MODEL}，含逐字時間戳，共 {len(chunks)} 段），這步不耗 Claude token...")

        all_segments = []
        all_words = []
        for i, (chunk_path, offset) in enumerate(chunks, start=1):
            log(f"上傳第 {i}/{len(chunks)} 段...")
            with chunk_path.open("rb") as f:
                result = client.audio.transcriptions.create(
                    file=f,
                    model=GROQ_MODEL,
                    response_format="verbose_json",
                    language=WHISPER_LANGUAGE,
                    timestamp_granularities=["word", "segment"],
                )
            for seg in result.segments:
                all_segments.append({
                    "start": seg["start"] + offset,
                    "end": seg["end"] + offset,
                    "text": seg["text"].strip(),
                })
            for w in (result.words or []):
                all_words.append({
                    "word": w["word"].strip(),
                    "start": w["start"] + offset,
                    "end": w["end"] + offset,
                })

    srt_path = transcript_dir / f"{source_video.stem}.srt"
    words_path = transcript_dir / f"{source_video.stem}.words.json"
    _write_srt(srt_path, all_segments)
    _write_words_json(words_path, all_words)

    log(f"字幕已產生：{srt_path}")
    log(f"逐字時間戳已產生：{words_path}")
    return {"srt": srt_path, "words": words_path}


def run_whisper(source_video: Path, transcript_dir: Path) -> dict:
    """
    優先序：Groq 雲端 API（有設定 GROQ_API_KEY 才會用，最快）
          → faster-whisper（本機 CPU，其次快）
          → openai-whisper CLI（都沒有的最後備援）。
    任何一層失敗都不會自己往下掉，除非是「沒裝/沒設定」這種明確可預期的情況，
    避免把真正的執行錯誤誤判成「換一種方式重試」。
    回傳 {"srt": Path, "words": Path}：srt 給人工校對讀，words.json（逐字
    時間戳）是下游 edit_state.json / 未來編輯器工具真正要吃的資料。
    """
    if os.environ.get("GROQ_API_KEY"):
        try:
            import groq  # noqa: F401
        except ImportError:
            log("偵測到 GROQ_API_KEY，但未安裝 groq 套件（pip install groq），改用本機方式。")
        else:
            return run_whisper_groq(source_video, transcript_dir)

    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        log("未偵測到 faster-whisper，改用 openai-whisper（較慢）。")
        log("可執行 pip install faster-whisper 之後這一步會自動變快。")
        return run_whisper_legacy(source_video, transcript_dir)
    return run_whisper_faster(source_video, transcript_dir)


def write_project_meta(project_dir: Path, project_name: str, source_video: Path, transcript: dict) -> Path:
    srt_path = transcript.get("srt")
    words_path = transcript.get("words")
    meta = {
        "project_name": project_name,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_video": str(source_video),
        "transcript_srt": str(srt_path) if srt_path and srt_path.exists() else None,
        "transcript_words": str(words_path) if words_path and words_path.exists() else None,
        # 下面這些欄位先留空，對應影片 demo 中 Claude 會接著詢問使用者
        # 的兩件事：走哪種模板、這一集的主題。由 short-video-cut Skill
        # 在對話中問完使用者後，自己把答案寫回這個檔案。
        "template": None,        # 例如 "talking_head_3zone" 或 "selfie_cover_mask"
        "topic": None,           # 用來重新命名資料夾的簡短主題句
        "status": "transcribed", # transcribed -> cut -> captioned -> rendered
    }
    meta_path = project_dir / "06_meta" / "project.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"專案設定檔已寫入：{meta_path}")
    return meta_path


def main() -> None:
    parser = argparse.ArgumentParser(description="建立短影音剪輯專案並自動轉出字幕")
    parser.add_argument("video", help="原始影片的完整路徑")
    parser.add_argument("--name", required=True, help="專案名稱（資料夾名稱，可先用暫定名稱，之後再改）")
    args = parser.parse_args()

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        log(f"找不到影片檔案：{video_path}")
        sys.exit(1)

    project_dir = PROJECTS_ROOT / args.name
    make_project_dirs(project_dir)

    source_video = copy_source_video(video_path, project_dir)
    transcript = run_whisper(source_video, project_dir / "02_transcript")
    write_project_meta(project_dir, args.name, source_video, transcript)

    log("完成。接下來請由 Claude Code 讀取 06_meta/project.json 與 SRT，")
    log("依 short-video-cut Skill 的規則繼續判斷剪輯點、抓 B-roll、燒字幕。")


if __name__ == "__main__":
    main()
