#!/usr/bin/env python3
"""
render.py
---------
整併方案的核心引擎：讀一份 06_meta/edit_state.json，輸出一支 mp4 到
05_render/。無論是 short-video-cut skill 第一次自動生成，還是使用者
之後用網頁編輯器調完參數，呼叫的都是同一支程式——保證兩邊產出的結果
邏輯一致，也讓「改參數重算」不用重新走一次 AI 判斷。

用法：
    python render.py "projects/<專案名稱>/06_meta/edit_state.json"

edit_state.json 的欄位說明見同資料夾的 edit_state.example.json。
這支程式把 SKILL.md 原本第5步 5a-5d 的手動 ffmpeg/ImageMagick 邏輯
原封不動地搬過來，差別只在於「參數統一從 JSON 讀，而不是每次臨時現
組 shell 指令」。
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from av_tools import DEFAULT_FONT_BOLD, find_ffmpeg_cmd, find_magick_cmd, ffprobe_duration, find_ffprobe_cmd, log, resolve_cli_path


# ---------------------------------------------------------------------------
# 1. clips[]：依 keep=true 的片段精剪＋拼接（涵蓋「剪停頓」跟「抓重點拼接」
#    兩種用法——停頓就是很多小段 keep=false，抓重點就是幾個不連續的
#    keep=true 片段，處理邏輯完全一樣）
# ---------------------------------------------------------------------------

_SILENCE_RE = re.compile(r"silence_duration: ([\d.]+)")
_DEAD_AIR_THRESHOLD_SEC = 1.5


def _warn_if_beat_has_dead_air(ffmpeg_cmd: str, beat_path: Path, beat_index: int) -> None:
    """
    自動安全網：每剪完一段就立刻用 ffmpeg 的 silencedetect 掃一次，抓「這段
    理論上該是連續講話，卻出現超過 1.5 秒靜音」的異常。踩過的教訓：曾經
    改過一版尋帶邏輯，主觀覺得應該修好了某個問題就直接出片給使用者，
    結果那版其實讓每段尾端整整少了近 5 秒的聲音——問題是用聽的才發現，
    而 Claude 自己聽不到render出來的影片。這裡不是要取代人耳確認，而是
    在「送出去給使用者看之前」先攔一層機械檢查，只要 clips[] 選的片段
    ideal 上都應該是連續講話，異常的長靜音幾乎都代表剪輯/尋帶出了問題，
    不是正常的語氣停頓（正常停頓在判斷剪輯點階段就該被抓掉，見
    SKILL.md 第4步）。這裡只警告不中止，因為極少數情況下（例如刻意
    留白的戲劇性停頓）長靜音是有意的。
    """
    # 這裡不能用 text=True——那會用 Windows 系統locale（這台是 cp950）
    # 去解碼 ffmpeg 的 stderr，ffmpeg 的輸出裡常有它解不動的位元組，會讓
    # subprocess 的背景讀取執行緒整個炸掉、stderr 變 None（實際踩過）。
    # 明確指定 utf-8 + errors="replace" 就不會再炸。
    result = subprocess.run(
        [ffmpeg_cmd, "-i", str(beat_path), "-af", "silencedetect=noise=-35dB:d=" + str(_DEAD_AIR_THRESHOLD_SEC),
         "-f", "null", "-"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    long_silences = [float(m) for m in _SILENCE_RE.findall(result.stderr)]
    if long_silences:
        # 這台機器的終端機是 cp950（繁中 Big5）codepage，emoji 會讓 print()
        # 直接丟 UnicodeEncodeError 炸掉——這裡不能用 emoji，純文字才安全。
        log(f"[警告] beat_{beat_index:03d}（{beat_path.name}）內偵測到"
            f"{len(long_silences)} 段超過 {_DEAD_AIR_THRESHOLD_SEC} 秒的靜音"
            f"（最長 {max(long_silences):.2f} 秒）。如果這段 clips[] 範圍內"
            f"應該是連續講話，這通常代表剪輯/尋帶出了問題（例如音訊被吃掉），"
            f"不要直接把成品交給使用者，先用 ffprobe/silencedetect 或重新聽過"
            f"確認再出片。")

def build_kept_video(ffmpeg_cmd: str, ffprobe_cmd: str, source_video: Path,
                      clips: list[dict], work_dir: Path) -> tuple[Path, list[dict]]:
    """
    回傳 (拼接後的影片路徑, 每段 keep 片段的真實時間資訊)。

    後面這個列表很重要：影片幀率如果不是能整除常見時間戳的值（例如這台
    機器很多素材是 16fps），ffmpeg 剪片時只能把長度無條件進位到最近的
    影格邊界，每段都會比 edit_state.json 寫的長度多出不到一影格的量。
    單段看不出來，但字幕/B-roll 的時間是「剪完拼接後」的累加時間軸，
    這個一影格內的誤差會一路累加下去，剪的段數一多，最後面的字幕就會
    明顯搶拍。修法不是去跟 ffmpeg 的幀率量化較勁（贏不了），而是剪完
    之後量測每段「真實剪出來多長」，讓後面算字幕/B-roll 的位移時用
    真實長度，不要相信 edit_state.json 裡「理論上該多長」的數字。
    """
    kept = [c for c in clips if c.get("keep")]
    if not kept:
        log("edit_state.clips 裡沒有任何 keep=true 的片段，無法產生影片。")
        sys.exit(1)

    work_dir.mkdir(parents=True, exist_ok=True)
    beat_paths = []
    # 每段尾端統一多留這麼多秒的緩衝，跟 edit_state.json 裡 clips[].end
    # 算得多準無關，一律都加。踩過的教訓：即使判斷層已經很小心地照
    # .words.json 算緩衝，Whisper/Groq 給的字尾時間戳本身就有系統性偏早
    # 的誤差（字音的自然衰減常常沒被算進那個字的時間戳裡），使用者實測
    # 交付的成品還是聽到「最後一個字被切一半」——這不是判斷層算得不夠
    # 仔細，是 ASR 時間戳這個資料來源本身的精度上限，光靠更小心地手算
    # 緩衝解決不了，要在執行層統一補一層保險。
    #
    # 這裡不會讓字幕/B-roll 的時間跟著跑掉：下面的 real_durations 是量測
    # 每段「真正剪出來多長」（已經含這段 pad），字幕時間換算用的是這個
    # 真實值，不是 clips[].end - clips[].start 這個理論值，兩者本來就有
    # 落差、也本來就有 _shift_to_real_timeline() 在處理，多出來的 pad
    # 只是讓每段尾端多出一小截不影響字幕顯示的「靜音緩衝尾巴」而已。
    END_PAD_SEC = 0.2
    for i, clip in enumerate(kept):
        beat_path = work_dir / f"beat_{i:03d}.mp4"
        ideal_duration = clip["end"] - clip["start"]
        duration = ideal_duration + END_PAD_SEC
        # 尾端音量淡出：時間點要落在「加了 pad 之後」的真正尾端，不是
        # ideal_duration 那個點——否則淡出反而會提早發生在 pad 那段
        # 補回來的音訊正中間，把剛救回來的字尾又蓋掉一次。
        fade_dur = 0.12
        af = f"afade=t=out:st={max(duration - fade_dur, 0):.3f}:d={fade_dur}" if duration > 0.3 else None
        # 曾經試過「粗略快轉＋精準微調」的兩段式尋帶（-ss 分別放在 -i 前後）
        # 想解決懷疑中的尋帶不精準問題，結果反而是誤診：實測用 silencedetect
        # 量過，單純 `-ss <start> -i source`（-ss 在 -i 之前，輸入端尋帶）
        # 剪出來的每一段音訊完全正常、內容也對得上逐字稿，兩段式尋帶那版
        # 才是真正壞掉的——它會讓每段尾端固定少掉接近「快轉緩衝秒數」長度
        # 的音訊（整段變成純靜音，不是淡出，是真的没聲音），因為 audio/video
        # 兩個 stream 對「快轉後再精準往前跳」的位移量沒有對齊。**不要再
        # 加這種二段式尋帶了**——這台工具鏈遇到的來源檔案，單純的輸入端
        # `-ss` 就已經是準確的，不需要、也不能再疊加第二個 `-ss`。
        cmd = [
            ffmpeg_cmd, "-y", "-ss", str(clip["start"]), "-i", str(source_video),
            "-t", str(duration),
        ]
        if af:
            cmd += ["-af", af]
        cmd += [
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "aac",
            str(beat_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        beat_paths.append(beat_path)
        _warn_if_beat_has_dead_air(ffmpeg_cmd, beat_path, i)

    real_durations = [ffprobe_duration(ffprobe_cmd, p) for p in beat_paths]
    ideal_acc = 0.0
    real_acc = 0.0
    real_clips = []
    for clip, real_dur in zip(kept, real_durations):
        ideal_dur = clip["end"] - clip["start"]
        real_clips.append({
            "ideal_start": ideal_acc, "ideal_end": ideal_acc + ideal_dur,
            "real_start": real_acc, "real_end": real_acc + real_dur,
        })
        ideal_acc += ideal_dur
        real_acc += real_dur

    if len(beat_paths) == 1:
        return beat_paths[0], real_clips

    list_path = work_dir / "concat_list.txt"
    # 用 newline='\n' 明確避免 Windows 文字模式把換行寫成 \r\n
    # （這個坑之前炸過一次：mapfile 會把 \r 黏進檔名參數，ffmpeg 開檔失敗）
    with list_path.open("w", encoding="utf-8", newline="\n") as f:
        for p in beat_paths:
            f.write(f"file '{p.name}'\n")

    concat_out = work_dir / "kept_concat.mp4"
    cmd = [
        ffmpeg_cmd, "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path), "-c", "copy", str(concat_out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return concat_out, real_clips


def _shift_to_real_timeline(t: float, real_clips: list[dict]) -> float:
    """
    subtitles[]/broll[] 裡的時間，是作者（AI 或人工）假設每段 clip 長度
    剛好等於 edit_state.json 裡 end-start 寫的那樣去累加算出來的「理想
    拼接時間軸」。但實際剪完的每一段可能因為幀率量化而多出不到一影格，
    這裡把「理想時間軸」的時間點換算成「真實拼接時間軸」的時間點，
    差距會隨著片段數量累加，越後面的段落換算後跟原本假設的差越多。

    EPS：ideal_start/ideal_end 是好幾段長度累加出來的浮點數，理論上
    剛好卡在兩段交界的時間點（例如某句字幕剛好從下一段的第一個字開始），
    實際算出來的邊界值可能因為浮點數誤差跟原始值差了 1e-14 那個等級，
    導致這句被誤判成屬於「前一段」而不是「後一段」，時間就會跟著算錯
    ——實測發生過，燒出來的字幕時間點跟真正該屬於的片段對不起來。
    """
    EPS = 1e-6
    for rc in real_clips:
        if rc["ideal_start"] - EPS <= t < rc["ideal_end"] - EPS:
            return rc["real_start"] + (t - rc["ideal_start"])
        if t < rc["ideal_start"] - EPS:
            return rc["real_start"]
    return real_clips[-1]["real_end"] if real_clips else t


# ---------------------------------------------------------------------------
# 2. 文字圖卡（標題／CTA／字幕）：全部先用 ImageMagick 產生透明背景 PNG，
#    再用 ffmpeg overlay 疊上去，理由跟 SKILL.md 講的一樣——字型/樣式
#    完全可控，不受播放器字幕渲染差異影響。
# ---------------------------------------------------------------------------

def render_text_card(magick_cmd: str, text: str, font_cfg: dict, out_path: Path,
                      box_w: int, box_h: int, bg_color: str | None = None) -> None:
    """
    字卡文字支援用 \\n 分成好幾行，每一行可以各自上色（例如標題兩句話，
    第一句白色、第二句紅色，像新聞標題那種樣式）——第一行用 `color`，
    第二行（以後）用 `color2`（沒指定就跟第一行同色）。每一行都疊黑色
    陰影（往右下偏移幾個像素、半透明黑）+ 黑色描邊 + 實色字三層，做出
    截圖那種粗黑框帶陰影的效果。
    """
    font = font_cfg.get("font_path", DEFAULT_FONT_BOLD)
    size = font_cfg.get("size", 48)
    color = font_cfg.get("color", "#FFFFFF")
    color2 = font_cfg.get("color2", color)
    border = font_cfg.get("border", {"color": "#000000", "width": 4})
    stroke_color = border.get("color", "#000000") if border else None
    stroke_width = border.get("width", 4) if border else 0
    shadow_offset = font_cfg.get("shadow_offset", 4)

    if bg_color:
        subprocess.run([
            magick_cmd, "-size", f"{box_w}x{box_h}", "xc:none", "-fill", bg_color,
            "-draw", f"roundrectangle 0,0,{box_w},{box_h},24,24", str(out_path),
        ], check=True, capture_output=True)
    else:
        subprocess.run([magick_cmd, "-size", f"{box_w}x{box_h}", "xc:none", str(out_path)],
                        check=True, capture_output=True)

    # 用 Center gravity 算每一行相對「整個字卡正中央」要偏移多少，不要
    # 自己去猜字型的 ascent/baseline 在哪裡（之前那版用 North + 手算
    # 基準線位置，算出來的置中不準，字卡看起來會歪掉、沒有上下置中——
    # Center gravity 是 ImageMagick 自己算文字置中，準確很多，多行的話
    # 只要用「這一行離中間第幾行」乘上行高去對稱偏移就好）。
    lines = text.split("\n")
    line_height = round(size * 1.25)
    n = len(lines)

    for i, line in enumerate(lines):
        if not line.strip():
            continue
        line_color = color if i == 0 else color2
        offset = round((i - (n - 1) / 2) * line_height)
        cmd = [magick_cmd, str(out_path), "-gravity", "Center", "-font", font, "-pointsize", str(size)]
        if shadow_offset:
            # 陰影：半透明黑色、往右下偏移
            cmd += ["-fill", "#00000080", "-stroke", "none",
                    "-annotate", f"+{shadow_offset}+{offset + shadow_offset}", line]
        if stroke_color and stroke_width:
            # 描邊 + 實色字疊兩次，才會又有黑框又有實色字
            cmd += ["-stroke", stroke_color, "-strokewidth", str(stroke_width),
                    "-fill", line_color, "-annotate", f"+0+{offset}", line]
            cmd += ["-stroke", "none", "-fill", line_color, "-annotate", f"+0+{offset}", line]
        else:
            cmd += ["-stroke", "none", "-fill", line_color, "-annotate", f"+0+{offset}", line]
        cmd.append(str(out_path))
        subprocess.run(cmd, check=True, capture_output=True)


def render_title(magick_cmd: str, title_cfg: dict, canvas_w: int, out_path: Path) -> None:
    box_w = min(canvas_w - 140, 940)
    render_text_card(magick_cmd, title_cfg["text"], title_cfg, out_path, box_w, 260,
                      bg_color=title_cfg.get("background"))


def render_cta(magick_cmd: str, cta_cfg: dict, canvas_w: int, out_path: Path) -> None:
    box_w = min(canvas_w - 180, 900)
    render_text_card(magick_cmd, cta_cfg["text"], cta_cfg, out_path, box_w, 140,
                      bg_color=cta_cfg.get("background", "#FF6D5A"))


# ---------------------------------------------------------------------------
# 3. 字幕：改走 .ass 檔 + libass 渲染（參考開源專案 opensource-clipping 的
#    做法），不再一個字一張 PNG 疊圖——整句寫成一個 Dialogue 事件，
#    animation="karaoke" 時用 \t() 時間軸標籤讓「當前字」瞬間變色、
#    下一刻變回原色，效能跟畫質都比疊圖好很多。
# ---------------------------------------------------------------------------

def _hex_to_ass_bgr(hex_color: str) -> str:
    """ASS 顏色格式是 &HBBGGRR&（藍綠紅，跟一般網頁 #RRGGBB 順序相反），
    這是最容易搞錯的地方，弄反了顏色會完全不對。"""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{b}{g}{r}&".upper()


def _hex_to_ass_bgr_with_alpha(hex_color: str, opacity: float) -> str:
    """BackColour 那類欄位是 &HAABBGGRR&，A 是「透明度」不是「不透明度」
    ——00 全不透明、FF 全透明，跟一般直覺相反，這裡直接吃 opacity(0~1) 換算。"""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    a = format(round((1 - opacity) * 255), "02X")
    return f"&H{a}{b}{g}{r}&".upper()


def _ass_time(seconds: float) -> str:
    cs = round(max(seconds, 0) * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    # { } 在 ASS 裡是覆寫標籤的括號，逐字文字裡若剛好出現要跳脫，
    # 否則會被誤判成標籤語法而整句跑版甚至消失。
    # 換行也要處理：ASS 的 Dialogue 一定要是檔案裡的單一實體行，文字裡的
    # 換行不能是真的按下 Enter（那樣會把這行 Dialogue 切成兩行，第二行沒
    # 有 "Dialogue:" 開頭會被當成無效資料整個消失）——真正的換行標籤是
    # 字面上的兩個字元 \N（反斜線+大寫N）。順序很重要：要先跳脫原本文字
    # 裡「既有」的反斜線，再把換行字元轉成 \N，不然剛插入的 \N 會被下一步
    # 誤當成「使用者自己打的反斜線」又跳脫一次，變成印出來的 \N 字面文字。
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return text.replace("\r\n", "\n").replace("\n", "\\N")


SUB_MARGIN_LR = 60  # 字幕卡左右各留的邊界（像素），跟畫布寬度一起決定每行能塞多少字
_CJK_CHAR_WIDTH_RATIO = 0.95  # 粗體中文字大約的「字寬 / 字級」比例，用來估算自動換行的斷點


def _auto_wrap_cjk(text: str, canvas_w: int, font_size: int, margin_lr: int = SUB_MARGIN_LR) -> str:
    """中文字幕沒有空白可以斷字，ASS/libass 的自動換行（WrapStyle）是設計給
    西方文字用的——判斷「單字」邊界靠空白，一整串連續中文字沒有空白就會被
    當成一個切不開的詞，不管 WrapStyle 設多少都不會自動換行，長句就會整行
    超出畫面（這裡實測到、也是使用者回報的 bug）。所以中文字幕的換行不能
    交給 ASS 引擎自動處理，得自己依畫面寬度跟字級反推「每行能放幾個字」，
    手動插入換行符號。已經有 \\n（使用者自己想換行的地方）就照原樣保留，
    只對還沒斷過的長段落自動補斷點。"""
    if "\n" in text or "\r" in text:
        return text  # 已經手動排版過，不要再自動插入換行
    max_chars = max(4, int((canvas_w - margin_lr * 2) / (font_size * _CJK_CHAR_WIDTH_RATIO)))
    if len(text) <= max_chars:
        return text
    # 優先在標點符號處斷行，找不到才硬斷在字數上限——標點斷行比較不會斷在
    # 語意中間，讀起來更順。
    break_chars = "，,。！？、；："
    lines = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = max((window.rfind(c) for c in break_chars), default=-1)
        if cut < max_chars // 2:  # 標點太早出現就不採用，寧可硬斷也不要行長度差太多
            cut = max_chars - 1
        lines.append(remaining[:cut + 1])
        remaining = remaining[cut + 1:]
    if remaining:
        lines.append(remaining)
    return "\n".join(lines)


def build_ass_file(subtitles: list[dict], style: dict, canvas_w: int, canvas_h: int,
                    sub_y_from_top: int, out_path: Path) -> None:
    """整句金句卡樣式（不是逐字 karaoke）：subtitles 是一句一筆的清單
    [{"text","start","end","emphasis"}]——emphasis 由讀逐字稿時的內容判斷
    決定（這句話夠不夠精簡有力、值不值得特別強調），不是跟著時間軸算的。
    文字本身粗黑描邊、沒有底色方塊；emphasis=true 的句子用更大字級、
    強調色（預設橘紅），其餘句子維持一般大小、白色——對應使用者提供的
    範例圖：金句卡是大字橘紅、一般字幕是白字，兩者都只有黑色描邊。"""
    size = style.get("size", 56)
    emphasis_size = style.get("emphasis_size", 68)
    color = _hex_to_ass_bgr(style.get("color", "#FFFFFF"))
    emphasis_color = _hex_to_ass_bgr(style.get("emphasis_color", "#FF3B30"))
    outline_color = _hex_to_ass_bgr(style.get("outline_color", "#000000"))
    font_family = style.get("ass_font_family", "Microsoft JhengHei Bold")
    outline_val = style.get("outline_width", 5)
    shadow_val = style.get("shadow", 0)

    header = (
        "[Script Info]\n"
        f"PlayResX: {canvas_w}\nPlayResY: {canvas_h}\nWrapStyle: 1\n"
        "ScriptType: v4.00+\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_family},{size},{color},{outline_color},&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline_val},{shadow_val},8,{SUB_MARGIN_LR},{SUB_MARGIN_LR},{sub_y_from_top},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    # BorderStyle=1：只有描邊（+可選陰影），沒有底色方塊，符合範例圖的「文字
    # 直接疊在畫面上、粗黑邊」樣式。Alignment=8：頂部置中，MarginV 從畫面
    # 「上緣」算，對應 sub_y_from_top。

    events = []
    prev_end = 0.0
    for line in subtitles:
        start = max(line["start"], prev_end)
        end = max(line["end"], start + 0.05)
        prev_end = end
        is_emphasis = bool(line.get("emphasis"))
        sz = emphasis_size if is_emphasis else size
        col = emphasis_color if is_emphasis else color
        italic = "\\i1" if is_emphasis else ""
        wrapped = _auto_wrap_cjk(line["text"], canvas_w, sz)
        text = f"{{\\fs{sz}\\c{col}{italic}}}" + _ass_escape(wrapped)
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}\n")

    out_path.write_text(header + "".join(events), encoding="utf-8", newline="\n")


def _escape_ffmpeg_filter_value(value: str) -> str:
    # ffmpeg filtergraph 語法裡 : 是 key=value 分隔符、' 是包字串用的，
    # 兩個都要跳脫，否則路徑（尤其 Windows 的碟符冒號）會把整串 filter 語法拆壞。
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


# ---------------------------------------------------------------------------
# 4. 組 ffmpeg filter_complex：背景 + 主影片 + 標題 + CTA + 所有字幕卡疊圖
# ---------------------------------------------------------------------------

def build_background(magick_cmd: str, canvas_w: int, canvas_h: int, bg_color: str,
                      title_png: Path | None, cta_png: Path | None, out_path: Path) -> None:
    cmd = [magick_cmd, "-size", f"{canvas_w}x{canvas_h}", f"xc:{bg_color}"]
    if title_png:
        cmd += [str(title_png), "-gravity", "North", "-geometry", "+0+90", "-composite"]
    if cta_png:
        cmd += [str(cta_png), "-gravity", "South", "-geometry", "+0+120", "-composite"]
    cmd.append(str(out_path))
    subprocess.run(cmd, check=True, capture_output=True)


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _crop_zoom_filter(crop: dict) -> str:
    """layout.crop = {x, y, zoom}：x/y 是 0~1 的錨點（0.5,0.5＝正中央），
    zoom>1 表示放大取樣範圍中間那一塊。輸出接原始輸入的 iw/ih 表達式，
    不用先 ffprobe 影片尺寸。"""
    zoom = crop.get("zoom", 1.0)
    x = crop.get("x", 0.5)
    y = crop.get("y", 0.5)
    if zoom <= 1.0:
        return ""
    return f"crop=iw/{zoom}:ih/{zoom}:(iw-iw/{zoom})*{x}:(ih-ih/{zoom})*{y},"


def render(edit_state_path: Path) -> Path:
    state = json.loads(edit_state_path.read_text(encoding="utf-8"))
    project_dir = edit_state_path.parent.parent  # 06_meta/edit_state.json -> 專案根目錄
    work_dir = project_dir / "04_cuts" / "render_work"
    render_dir = project_dir / "05_render"
    render_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_cmd = find_ffmpeg_cmd()
    ffprobe_cmd = find_ffprobe_cmd()
    magick_cmd = find_magick_cmd()

    source_video = project_dir / state["source"]["video_path"]
    log(f"讀取 edit_state：{edit_state_path.name}（專案：{state['project_name']}）")

    # 1. 剪片 + 拼接
    log("依 clips[] 精剪並拼接...")
    kept_video, real_clips = build_kept_video(ffmpeg_cmd, ffprobe_cmd, source_video, state["clips"], work_dir)

    # subtitles[]/broll[] 的時間是照「假設每段剪出來剛好等於 end-start」
    # 算出來的，實際剪出來的每段可能因為幀率量化多出不到一影格、越後面
    # 累積誤差越大（詳見 build_kept_video 的說明）。這裡統一換算成真實
    # 拼接後的時間軸，字幕/B-roll 才不會隨著片段數量增加越拍越搶快。
    for sub in state.get("subtitles", []):
        sub["start"] = _shift_to_real_timeline(sub["start"], real_clips)
        sub["end"] = _shift_to_real_timeline(sub["end"], real_clips)
    for b in state.get("broll", []):
        b["start"] = _shift_to_real_timeline(b["start"], real_clips)
        b["end"] = _shift_to_real_timeline(b["end"], real_clips)

    layout = state.get("layout", {})
    canvas_w, canvas_h = (1080, 1920) if layout.get("ratio", "9:16") == "9:16" else (1920, 1080)
    video_h = round(canvas_w * 9 / 16)  # 主影片區塊固定 16:9，貼在畫布中間
    video_y = (canvas_h - video_h) // 2
    crop_filter = _crop_zoom_filter(layout.get("crop", {})) if layout.get("crop") else ""

    # 2. 標題卡／CTA 卡
    assets_dir = work_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    title_png = None
    if state.get("title"):
        title_png = assets_dir / "title.png"
        render_title(magick_cmd, state["title"], canvas_w, title_png)
    cta_png = None
    if state.get("cta"):
        cta_png = assets_dir / "cta.png"
        render_cta(magick_cmd, state["cta"], canvas_w, cta_png)

    bg_color = layout.get("background", {}).get("color", "#1b1d29")
    background_png = assets_dir / "background.png"
    build_background(magick_cmd, canvas_w, canvas_h, bg_color, title_png, cta_png, background_png)

    # 3. 字幕（讀 subtitles[] 逐字時間戳 + subtitle_style，寫成一份 .ass 檔）
    ass_path = None
    if state.get("subtitles") and state.get("subtitle_style"):
        log("產生 .ass 字幕檔...")
        sub_y_from_top = video_y + video_h + 60
        ass_path = assets_dir / "subtitles.ass"
        build_ass_file(state["subtitles"], state["subtitle_style"], canvas_w, canvas_h,
                       sub_y_from_top, ass_path)

    # 4. B-roll：時間到了就疊在主影片畫面上面（同一個 16:9 區塊、同樣大小），
    #    蓋過主影片直到那段時間結束，跟 SKILL.md 講的「B-roll 蓋過主講畫面」
    #    是同一個做法，只是現在參數統一從 edit_state 讀。
    broll_entries = [b for b in state.get("broll", []) if b.get("asset")]

    # 5. 音樂：跟原始人聲混音，用 volume 欄位控制配樂音量，長度不足就整段循環。
    music = state.get("music") or {}
    music_asset = music.get("asset")

    # 6. 組 ffmpeg 指令：直接建 Python list 傳給 subprocess，不經過文字檔
    #    再切字串——路徑本身含空白（例如 OneDrive 資料夾名稱），用空白
    #    分割會被切壞，這是之前手動測試才需要的 bash 中介，render.py
    #    全程在 Python 裡執行完全不需要。
    input_args: list[str] = ["-loop", "1", "-i", str(background_png), "-i", str(kept_video)]
    video_scale = f"{crop_filter}scale={canvas_w}:{video_h}"
    lines = [f"[1:v]{video_scale}[vid];",
             f"[0:v][vid]overlay=x=0:y={video_y}[tmp0];"]
    prev = "tmp0"
    idx = 2

    for b in broll_entries:
        asset_path = project_dir / b["asset"]
        is_image = asset_path.suffix.lower() in IMAGE_EXTS
        # -itsoffset 把這個輸入的時間戳整體往後平移 b['start'] 秒，讓它自己
        # 的第 0 幀對齊到疊圖視窗真正開始的那一刻。沒有這個的話，B-roll 影片
        # 會跟著整支 ffmpeg 處理程序從 t=0 開始播，往往在疊圖視窗真正啟動
        # （例如 t=26s）之前就已經播完，之後只會疊上「最後一幀」的凍結畫面
        # ——實測就是這樣，使用者回報「素材是圖片」其實是影片被凍結成靜態
        # 畫面，不是真的用了圖片素材。
        input_args += ["-itsoffset", str(b["start"])]
        input_args += (["-loop", "1", "-i", str(asset_path)] if is_image else ["-i", str(asset_path)])
        nxt = f"tmp{idx-1}"
        lines.append(f"[{idx}:v]scale={canvas_w}:{video_h}:force_original_aspect_ratio=increase,"
                     f"crop={canvas_w}:{video_h}[broll{idx}];")
        lines.append(
            f"[{prev}][broll{idx}]overlay=x=0:y={video_y}:"
            f"enable='between(t,{b['start']},{b['end']})'[{nxt}];"
        )
        prev = nxt
        idx += 1

    if ass_path:
        # subtitles 濾鏡用 libass 直接吃 .ass 檔渲染，不用像 broll/字卡那樣
        # 額外開一個 -i 輸入——它是純粹的濾鏡運算，接在既有畫面 label 後面即可。
        # 用 cwd=assets_dir 讓這裡只需要傳「檔名」，不用處理 Windows 路徑裡
        # 磁碟機冒號、反斜線、OneDrive 資料夾裡的空白跟中文字全部混在一起的
        # 跳脫地獄——fontsdir 同理不傳絕對路徑，libass 在這台機器上就能透過
        # 系統字型列舉找到「Microsoft JhengHei Bold」這種安裝好的字型名稱。
        # 注意：subtitles 是純濾鏡運算，不像 broll/music 會新增一個 -i 輸入，
        # 所以這裡不能動到 idx（它是「下一個輸入檔的編號」），否則後面 music
        # 算出來的 [N:a] 編號會全部錯位一個——這個 bug 實際炸過一次。
        nxt = f"tmp{idx-1}_sub"
        lines.append(f"[{prev}]subtitles=filename={ass_path.name}[{nxt}];")
        prev = nxt

    # prev 永遠等於 lines[-1] 那一行的輸出 label（不管最後一段是字幕、B-roll、
    # 還是兩者都沒有時的 tmp0 本身），直接把它改名成 outv 給 -map 用。
    lines[-1] = lines[-1].rsplit("[", 1)[0] + "[outv];"

    audio_out = "1:a"
    if music_asset:
        music_path = project_dir / music_asset
        input_args += ["-stream_loop", "-1", "-i", str(music_path)]
        music_idx = idx
        idx += 1
        vol = music.get("volume", 0.15)
        lines.append("[1:a]volume=1.0[voice];")
        lines.append(f"[{music_idx}:a]volume={vol}[bgm];")
        lines.append("[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        audio_out = "[aout]"
    else:
        lines[-1] = lines[-1].rstrip(";")

    filter_complex = "\n".join(lines)

    # 順便存一份文字檔方便 debug（不參與實際指令執行）
    (assets_dir / "filter_complex.txt").write_text(filter_complex, encoding="utf-8", newline="\n")

    # 每次 render 都另存新檔（時間戳記命名），不要覆蓋掉上一版——
    # 這個 session 好幾次都需要拿「上一版正確的成品」來對照排查問題，
    # 如果每次都直接覆蓋，根本沒有上一版可以比對，等於自己把復原的
    # 退路砍掉。GUI 會自動抓最新那一版顯示，不用手動管理檔名。
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = render_dir / f"{state['project_name']}_{stamp}.mp4"
    log(f"合成輸出到 {out_path.name}...")

    # 背景圖是 -loop 1 的無限迴圈，overlay 疊上主影片之後，實測 -shortest
    # 沒有正確把輸出截斷到音軌（也就是真正內容）的長度——video track 會
    # 比 audio track 多拖出好幾秒（背景圖繼續輸出、疊上去的主影片畫面則是
    # 凍結最後一幀），字幕視覺上就會像「搶拍」，其實是尾巴多拖出一段沒有
    # 對應語音的畫面。不要只靠 -shortest，直接用剪完拼接後量到的真實總長
    # 明確指定輸出時間，保證影片長度精準對齊音軌／字幕。
    total_kept_duration = real_clips[-1]["real_end"] if real_clips else 0.0
    # -hide_banner -loglevel error：不加的話 ffmpeg 預設會把 codec/stream
    # banner 跟編碼中的逐幀進度統計（frame=... fps=... time=...）整包印到
    # stderr。這裡是輸出整支成品的合成指令，交給 agent（Codex/Claude Code）
    # 執行時這些逐幀輸出會被當成 shell 指令的輸出整段讀進上下文，一次合成
    # 可能就是幾百到上千行，很快把 context/usage 燒光。只壓 loglevel 到
    # error，不加 capture_output，是因為真的失敗時還是要讓 stderr 直接
    # 冒出來，不要連錯誤訊息都吞掉、變成看不出哪裡失敗。
    cmd = [ffmpeg_cmd, "-hide_banner", "-loglevel", "error", "-y", *input_args,
           "-filter_complex", filter_complex,
           "-map", "[outv]", "-map", audio_out, "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-crf", "20", "-preset", "medium", "-c:a", "aac", "-b:a", "128k",
           "-t", str(total_kept_duration), "-shortest",
           str(out_path)]
    # cwd=assets_dir 只是為了讓上面 subtitles=filename=xxx.ass 能用相對檔名
    # （見那段註解）；其餘輸入/輸出都用絕對路徑，不受 cwd 影響。
    subprocess.run(cmd, check=True, cwd=str(assets_dir))

    # 把「這支成品實際燒進去的字幕時間」（已經套用過 _shift_to_real_timeline，
    # 不是 edit_state.json 裡原始的理想時間）另存一份 sidecar 檔，讓
    # verify_render.py 拿來當比對基準用。這是修一個踩過的坑：
    # edit_state.json 裡 subtitles[] 寫的是「假設每段剛好等於 end-start
    # 長度」算出來的理想時間軸，跟真正剪出來、拼接後的時間軸幾乎一定有
    # 落差（幀率量化、或像現在這樣刻意加的尾端安全緩衝都會造成落差）。
    # 這支腳本自己在燒字幕前已經正確換算過（見上面 _shift_to_real_timeline
    # 那段），但 verify_render.py 過去是直接拿 edit_state.json 的原始理想
    # 時間去跟重新轉錄的真實語音比對——兩邊基準不一致，落差小的時候
    # 剛好還在 0.4 秒門檻內看不出來，直到某次改動（例如把尾端安全緩衝
    # 從幾乎 0 秒加大到 0.2 秒）讓累積落差變大，才讓一堆其實燒得正確的
    # 字幕被誤判成「偷跑」。不要再讓 verify 用理想時間去比對真實成品，
    # 這裡把真正燒進去的時間存下來，verify_render.py 優先讀這份。
    timeline_path = out_path.with_suffix(".timeline.json")
    timeline_path.write_text(
        json.dumps({"subtitles": state.get("subtitles", []), "broll": state.get("broll", [])},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"完成：{out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="讀 edit_state.json 輸出短影音成品")
    parser.add_argument("edit_state", help="edit_state.json 的路徑")
    args = parser.parse_args()
    render(resolve_cli_path(args.edit_state))


if __name__ == "__main__":
    main()
