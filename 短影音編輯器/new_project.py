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

from av_tools import find_ffmpeg_cmd, find_ffprobe_cmd, find_whisper_cmd, ffprobe_duration, log, srt_timestamp, resolve_cli_path

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
# 用完整版而不是 turbo：turbo 是砍掉解碼器層數換速度的版本，時間戳
# 精準度跟少見口語現象（重複贅詞、快速接話）的辨識都比完整版差，
# 實測踩過詞條時間戳異常長、相鄰詞條重疊、重複語句漏抓其中一次
# 這幾個問題，換回完整版可以緩解（2026-08 debug AVIS專案時發現並換的）。
# 免費層請求上限比 turbo 低（Groq Playground 顯示 20/分鐘、2000/天），
# 但這個工作流程一支影片通常只呼叫個位數次，遠用不到這個上限。
GROQ_MODEL = "whisper-large-v3"
# 免費版帳號檔案上限 25MB，這裡抓 22MB 當安全門檻，留一點餘裕。
GROQ_MAX_CHUNK_BYTES = 22 * 1024 * 1024
# 壓縮音軌用的 bitrate（kbps）。只有 FLAC 超過大小上限時才會用到這個
# 降級路徑（見 _extract_audio_for_transcription 的說明），所以不用壓到
# 太低，優先保留辨識度。
GROQ_AUDIO_BITRATE_KBPS = 64

# --- 轉錄後自動覆核（QA）設定 ---
# Whisper 解碼器有個已知的行為：為了避免自己陷入「無限重複同一個詞」的
# hallucination loop，它內建了抑制重複輸出的機制；副作用是使用者真的口語
# 重複講了同一個詞時，有機率被誤判成解碼迴圈而吞掉，合併成一個異常拖長的
# 詞（正常中文字時長約 0.1-0.5 秒，這種吞字後常常標到 1.5 秒以上）。這個
# 行為是機率性的（同一份音檔重轉好幾次結果會不一樣），沒辦法靠調整
# bitrate、切段方式等參數穩定避免，Groq API 也沒開放底層的 repetition
# 相關參數給我們調——只能靠「事後自動覆核」來補救：轉錄完後掃一次逐字
# 時間戳，把異常長的詞抓出來，各自單獨抓一小段音檔重新問一次 Groq
# （脫離原本整段音檔的上下文，降低同樣吞字的機率），比對後修正。
QA_LONG_WORD_THRESHOLD_SEC = 1.2   # 超過這個時長的單一詞視為可疑
QA_RECHECK_PAD_SEC = 5.0           # 覆核時，異常詞前後各抓多少秒的上下文
QA_RECHECK_MAX_ATTEMPTS = 2        # 同一個異常點最多重轉幾次找乾淨結果
QA_RECHECK_AUDIO_BITRATE_KBPS = 64  # 覆核用的音檔品質（比主流程稍高，小片段成本很低）

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
        try:
            same_file = video_path.resolve() == dest.resolve() or os.path.samefile(video_path, dest)
        except OSError:
            same_file = False
        if same_file:
            log(f"01_source 已有同名檔案，略過複製：{dest.name}")
            return dest
        src_stat, dest_stat = video_path.stat(), dest.stat()
        if src_stat.st_size == dest_stat.st_size and int(src_stat.st_mtime) == int(dest_stat.st_mtime):
            log(f"01_source 已有內容相同的同名檔案，略過複製：{dest.name}")
            return dest
        log(f"01_source 已有同名但內容不同的檔案，視為新版本並覆蓋：{dest.name}（原檔案已被取代）")
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


def _write_qa_report_detection_only(words: list[dict], transcript_dir: Path, stem: str) -> Path:
    """
    本機轉錄（faster-whisper／openai-whisper CLI）沒有像 Groq 那樣能快速又
    便宜地單獨重問一小段音檔來覆核，所以這裡只做「偵測並列出可疑點」，不
    自動修正——比完全沒有 QA 報告好，至少讓使用者知道要去哪幾個時間點
    人工核對，跟 Groq 路徑寫出來的報告是同一個檔名慣例。
    """
    anomalies = _find_long_word_anomalies(words)
    lines = []
    if not anomalies:
        lines.append("這次轉錄沒有偵測到異常長詞（可能吞字的訊號），不需要人工核對。")
    else:
        lines.append(
            f"偵測到 {len(anomalies)} 個異常長詞（時長 > {QA_LONG_WORD_THRESHOLD_SEC} 秒，"
            "可能是 Whisper 把口語重複的內容吞成一個拖長的詞），本機轉錄路徑不會自動覆核，"
            "建議剪輯前對照原始音檔逐一人工確認："
        )
        for w in anomalies:
            lines.append(f"  - [{w['start']:.2f}s-{w['end']:.2f}s] 「{w['word']}」")
    qa_report_path = transcript_dir / f"{stem}.qa_report.txt"
    qa_report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return qa_report_path


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
    qa_report_path = _write_qa_report_detection_only(words, transcript_dir, source_video.stem)

    log(f"字幕已產生：{srt_path}")
    log(f"逐字時間戳已產生：{words_path}")
    log(f"轉錄 QA 報告已產生：{qa_report_path}（本機轉錄路徑只能偵測、無法自動覆核，剪輯前建議看一下）")
    return {"srt": srt_path, "words": words_path, "qa_report": qa_report_path}


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
        # openai-whisper CLI 預設 --verbose True 會邊轉邊把每一段逐句時間戳
        # 跟文字即時印到 stdout。這裡沒有 capture_output，交給 agent 執行時
        # 那些逐句輸出會被當成 shell 指令輸出整段讀進上下文，長影片可能就是
        # 幾百行——關掉 verbose，失敗時仍然靠下面的 except 印出 exit code。
        "--verbose", "False",
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
    qa_report_path = _write_qa_report_detection_only(words, transcript_dir, source_video.stem)
    log(f"字幕已產生：{srt_path}")
    log(f"逐字時間戳已產生：{words_path}")
    log(f"轉錄 QA 報告已產生：{qa_report_path}（本機轉錄路徑只能偵測、無法自動覆核，剪輯前建議看一下）")
    return {"srt": srt_path, "words": words_path, "qa_report": qa_report_path}


def _extract_flac_audio(ffmpeg_cmd: str, source_video: Path, out_path: Path) -> None:
    """把影片音軌抽出來存成無損 FLAC，單聲道 16kHz（人聲轉錄夠用，不用立體聲/更高取樣率）。"""
    cmd = [
        ffmpeg_cmd, "-y", "-i", str(source_video),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "flac",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _extract_compressed_audio(ffmpeg_cmd: str, source_video: Path, out_path: Path) -> None:
    """把影片音軌抽出來，壓成低 bitrate 單聲道 opus，縮小上傳體積用（只在 FLAC 超過大小上限時才會用到，見下方 _extract_audio_for_transcription）。"""
    cmd = [
        ffmpeg_cmd, "-y", "-i", str(source_video),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libopus", "-b:a", f"{GROQ_AUDIO_BITRATE_KBPS}k",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _extract_audio_for_transcription(ffmpeg_cmd: str, source_video: Path, tmp_dir: Path) -> Path:
    """
    優先用無損 FLAC 抽音軌上傳給 Groq。

    這是踩過真實的坑才這樣做的：實測發現 libopus（有損）編碼器**不是逐位元組
    決定性的**——同一支來源影片、同一組 ffmpeg 參數，編兩次出來的 24kbps
    Opus 檔案位元組不一樣（多執行緒編碼器常見的行為）。Whisper 對「使用者
    口語重複講同一個詞」這種情況的解碼判斷，剛好非常接近一個決策邊界（要
    輸出兩次還是合併成一個拖長的詞），音檔位元組的微小差異就足以把結果推
    到邊界的不同側，導致同一支影片每次轉錄結果不穩定、有機率把真的講了
    兩次的話吞成一次。改用 FLAC 後，同一支影片不管轉幾次，位元組都逐一
    相同（已用 md5 驗證過），從根本消除了這個變因。

    FLAC 檔案比較大，只有在超過 Groq 免費版單檔上限時才會降級用 Opus 壓縮
    （這種情況下沒辦法保證同樣的穩定性，會印警告告訴使用者）。
    """
    flac_path = tmp_dir / "audio.flac"
    _extract_flac_audio(ffmpeg_cmd, source_video, flac_path)
    if flac_path.stat().st_size <= GROQ_MAX_CHUNK_BYTES:
        return flac_path

    log(f"無損 FLAC 音軌約 {flac_path.stat().st_size / 1024 / 1024:.1f}MB，超過 Groq 免費版單檔上限，"
        f"降級改用 {GROQ_AUDIO_BITRATE_KBPS}kbps Opus 壓縮（這種情況下無法保證每次轉錄結果完全穩定，"
        "轉錄後請務必看一下自動產生的 QA 報告）。")
    flac_path.unlink(missing_ok=True)
    opus_path = tmp_dir / "audio.ogg"
    _extract_compressed_audio(ffmpeg_cmd, source_video, opus_path)
    return opus_path


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

    log(f"音檔約 {size_bytes / 1024 / 1024:.1f}MB，超過 Groq 免費版單檔上限，"
        f"依每段約 {chunk_seconds:.0f} 秒切段上傳...")

    suffix = audio_path.suffix  # .flac 或 .ogg，切段後容器格式要跟來源一致
    chunk_pattern = chunk_dir / f"chunk_%03d{suffix}"
    cmd = [
        ffmpeg_cmd, "-y", "-i", str(audio_path),
        "-f", "segment", "-segment_time", str(int(chunk_seconds)),
        "-c", "copy", str(chunk_pattern),
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    chunks = sorted(chunk_dir.glob(f"chunk_*{suffix}"))
    return [(c, i * chunk_seconds) for i, c in enumerate(chunks)]


def _find_long_word_anomalies(words: list[dict], threshold: float = QA_LONG_WORD_THRESHOLD_SEC) -> list[dict]:
    """找出時長異常長的單一詞（見上方 QA 設定的說明），回傳這些詞本身。"""
    return [w for w in words if (w["end"] - w["start"]) > threshold]


def _extract_qa_recheck_clip(ffmpeg_cmd: str, source_video: Path, out_path: Path, win_start: float, win_end: float) -> None:
    """
    覆核片段很短（幾十秒內），檔案大小完全不用擔心，直接用無損 FLAC——
    理由跟主流程改用 FLAC 一樣：避免有損編碼器的位元組非決定性，把覆核
    這一步本身變成不穩定的來源。
    """
    cmd = [
        ffmpeg_cmd, "-y",
        "-ss", str(max(0.0, win_start)), "-t", str(win_end - win_start),
        "-i", str(source_video),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "flac",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _repair_transcript_anomalies(
    client, ffmpeg_cmd: str, source_video: Path,
    all_segments: list[dict], all_words: list[dict],
    transcript_dir: Path,
) -> tuple[list[dict], list[dict], list[str]]:
    """
    轉錄完後的自動覆核：抓出異常長的詞，各自單獨重新問一次 Groq（脫離原本
    整段音檔的上下文），如果覆核結果在同一個時間點沒有出現異常，就用覆核
    結果取代原本那個詞／那句話；每個異常點最多重試 QA_RECHECK_MAX_ATTEMPTS
    次找乾淨結果，找不到就保留原樣但寫進報告，讓使用者知道這幾個地方最好
    人工聽過確認。

    這是機率性問題的機率性緩解，不是保證——同一個異常點就算覆核了，也有
    機率再度吞字，所以報告裡永遠列出「還需要人工複查」的項目，不會假裝
    百分之百修好。
    """
    anomalies = _find_long_word_anomalies(all_words)
    report_lines = []
    if not anomalies:
        report_lines.append("這次轉錄沒有偵測到異常長詞（可能吞字的訊號），不需要覆核。")
        return all_segments, all_words, report_lines

    log(f"轉錄後 QA：發現 {len(anomalies)} 個異常長詞（可能吞掉了重複口語內容），逐一自動覆核中...")
    report_lines.append(f"偵測到 {len(anomalies)} 個異常長詞（時長 > {QA_LONG_WORD_THRESHOLD_SEC} 秒），逐一覆核結果如下：")

    with tempfile.TemporaryDirectory(prefix="qa_recheck_") as tmp:
        tmp_dir = Path(tmp)
        for idx, anomaly in enumerate(anomalies):
            core_start = anomaly["start"] - 0.1
            core_end = anomaly["end"] + 0.1
            win_start = anomaly["start"] - QA_RECHECK_PAD_SEC
            win_end = anomaly["end"] + QA_RECHECK_PAD_SEC
            clip_path = tmp_dir / f"anomaly_{idx}.flac"

            try:
                _extract_qa_recheck_clip(ffmpeg_cmd, source_video, clip_path, win_start, win_end)
            except subprocess.CalledProcessError:
                report_lines.append(f"  - [{anomaly['start']:.2f}s-{anomaly['end']:.2f}s] 「{_to_traditional(anomaly['word'])}」：覆核音檔擷取失敗，跳過，建議人工核對。")
                continue

            clip_win_start = max(0.0, win_start)
            best_words = None
            best_segments = None
            clean = False
            for attempt in range(QA_RECHECK_MAX_ATTEMPTS):
                with clip_path.open("rb") as f:
                    result = client.audio.transcriptions.create(
                        file=f,
                        model=GROQ_MODEL,
                        response_format="verbose_json",
                        language=WHISPER_LANGUAGE,
                        timestamp_granularities=["word", "segment"],
                    )
                recheck_words = [
                    {"word": w["word"].strip(), "start": w["start"] + clip_win_start, "end": w["end"] + clip_win_start}
                    for w in (result.words or [])
                ]
                recheck_segments = [
                    {"start": s["start"] + clip_win_start, "end": s["end"] + clip_win_start, "text": s["text"].strip()}
                    for s in result.segments
                ]
                has_anomaly = any((w["end"] - w["start"]) > QA_LONG_WORD_THRESHOLD_SEC for w in recheck_words
                                   if core_start - 0.5 <= w["start"] <= core_end + 0.5)
                if best_words is None:
                    best_words, best_segments = recheck_words, recheck_segments
                if not has_anomaly and recheck_words:
                    best_words, best_segments = recheck_words, recheck_segments
                    clean = True
                    break

            if best_words is None:
                report_lines.append(f"  - [{anomaly['start']:.2f}s-{anomaly['end']:.2f}s] 「{_to_traditional(anomaly['word'])}」：覆核沒有回傳內容，跳過，建議人工核對。")
                continue

            # 只替換「核心區」（異常詞自己的時間範圍）內的詞／句，覆核片段裡
            # 屬於前後 padding 上下文的部分不動，避免跟旁邊本來就正確的內容
            # 重複或錯位。
            core_recheck_words = [w for w in best_words if core_start <= w["start"] <= core_end or core_start <= w["end"] <= core_end]
            core_recheck_segments = [s for s in best_segments if s["end"] > core_start and s["start"] < core_end]

            if not core_recheck_words:
                report_lines.append(f"  - [{anomaly['start']:.2f}s-{anomaly['end']:.2f}s] 「{_to_traditional(anomaly['word'])}」：覆核結果對不到核心時間範圍，跳過，建議人工核對。")
                continue

            old_word_text = anomaly["word"]
            new_word_text = "".join(w["word"] for w in core_recheck_words)

            if new_word_text == old_word_text:
                report_lines.append(f"  - [{anomaly['start']:.2f}s-{anomaly['end']:.2f}s] 「{_to_traditional(old_word_text)}」：覆核結果跟原本一樣，可能真的就是這個字被拖長發音，非吞字，保留原樣。")
                continue

            # 修正 words：移除原本的異常詞，插入覆核抓到的詞
            all_words[:] = [w for w in all_words if not (w is anomaly)]
            all_words.extend(core_recheck_words)
            all_words.sort(key=lambda w: w["start"])

            # 修正 segments：移除跟核心區重疊的原始句子，插入覆核對應的句子
            all_segments[:] = [s for s in all_segments if not (s["end"] > core_start and s["start"] < core_end)]
            all_segments.extend(core_recheck_segments)
            all_segments.sort(key=lambda s: s["start"])

            status = "找到乾淨覆核結果並已修正" if clean else "重試後仍有異常，先採用較完整的版本，仍建議人工核對"
            display_old = _to_traditional(old_word_text)
            display_new = _to_traditional(new_word_text)
            report_lines.append(
                f"  - [{anomaly['start']:.2f}s-{anomaly['end']:.2f}s] 「{display_old}」→「{display_new}」：{status}。"
            )
            log(f"  QA 修正：「{display_old}」→「{display_new}」（{status}）")

    return all_segments, all_words, report_lines


def run_whisper_groq(source_video: Path, transcript_dir: Path) -> dict:
    """
    用 Groq 雲端 API（whisper-large-v3）轉字幕，request 時一併要
    word 級 timestamp_granularities，同時拿到逐句與逐字時間戳。
    需要環境變數 GROQ_API_KEY（SDK 會自動讀取，這支程式不會碰到 key 本身）。
    免費版帳號單檔 25MB 上限，音軌優先抽成無損 FLAC 上傳（見
    _extract_audio_for_transcription 的說明：這是為了避免有損編碼器每次
    編出來的位元組不一樣，導致同一支影片重轉結果不穩定），檔案太大時才
    降級用壓縮格式，並視大小自動切段上傳再合併時間軸。轉錄完會自動跑一次
    異常詞覆核（見 _repair_transcript_anomalies），把可能被吞掉的重複內容
    抓出來修正或至少列進 QA 報告。
    """
    from groq import Groq

    ffmpeg_cmd = find_ffmpeg_cmd()
    client = Groq()  # 從 GROQ_API_KEY 環境變數讀取

    with tempfile.TemporaryDirectory(prefix="groq_audio_") as tmp:
        tmp_dir = Path(tmp)
        log("抽取音軌準備上傳 Groq（優先用無損 FLAC，確保結果可重現）...")
        audio_path = _extract_audio_for_transcription(ffmpeg_cmd, source_video, tmp_dir)

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

    all_segments, all_words, qa_report = _repair_transcript_anomalies(
        client, ffmpeg_cmd, source_video, all_segments, all_words, transcript_dir,
    )

    srt_path = transcript_dir / f"{source_video.stem}.srt"
    words_path = transcript_dir / f"{source_video.stem}.words.json"
    qa_report_path = transcript_dir / f"{source_video.stem}.qa_report.txt"
    _write_srt(srt_path, all_segments)
    _write_words_json(words_path, all_words)
    qa_report_path.write_text("\n".join(qa_report) + "\n", encoding="utf-8")

    log(f"字幕已產生：{srt_path}")
    log(f"逐字時間戳已產生：{words_path}")
    log(f"轉錄 QA 報告已產生：{qa_report_path}（有列出建議人工核對的地方，剪輯前建議看一下）")
    return {"srt": srt_path, "words": words_path, "qa_report": qa_report_path}


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
    qa_report_path = transcript.get("qa_report")
    meta_path = project_dir / "06_meta" / "project.json"
    existing: dict = {}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    meta = {
        "project_name": project_name,
        "created_at": existing.get("created_at", datetime.datetime.now().isoformat(timespec="seconds")),
        "source_video": str(source_video),
        "transcript_srt": str(srt_path) if srt_path and srt_path.exists() else None,
        "transcript_words": str(words_path) if words_path and words_path.exists() else None,
        "transcript_qa_report": str(qa_report_path) if qa_report_path and qa_report_path.exists() else None,
        # 下面這些欄位對應影片 demo 中 Claude 會接著詢問使用者的兩件事：
        # 走哪種模板、這一集的主題。由 short-video-cut Skill 在對話中
        # 問完使用者後，自己把答案寫回這個檔案。若專案已存在（例如重跑
        # 轉錄），沿用既有值，不因為重新轉錄而被清空。
        "template": existing.get("template"),  # 例如 "talking_head_3zone" 或 "selfie_cover_mask"
        "topic": existing.get("topic"),         # 用來重新命名資料夾的簡短主題句
        "status": existing.get("status", "transcribed"), # transcribed -> cut -> captioned -> rendered
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"專案設定檔已寫入：{meta_path}")
    return meta_path


def main() -> None:
    parser = argparse.ArgumentParser(description="建立短影音剪輯專案並自動轉出字幕")
    parser.add_argument("video", help="原始影片的完整路徑")
    parser.add_argument("--name", required=True, help="專案名稱（資料夾名稱，可先用暫定名稱，之後再改）")
    args = parser.parse_args()

    video_path = resolve_cli_path(args.video)
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
