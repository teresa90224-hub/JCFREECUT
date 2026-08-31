#!/usr/bin/env python3
"""
verify_render.py
-----------------
出片後的字幕同步自動驗證：把渲染好的成品重新轉錄一次，拿「真正聽得到」
的逐字時間戳，去跟這支成品「實際燒進去」的 subtitles[] start/end 比對
（優先讀 render.py 產生的 <成品檔名>.timeline.json sidecar，那才是真正
換算過真實時間軸的版本；找不到才退回讀 edit_state.json 的原始理想時間），
超過門檻的差距就印出來。

背景（別再犯這個坑）：這支工具的原始逐字稿／.words.json 偶爾會在局部
出現時間戳損壞——可能是一個詞被標了異常長的時間（吃掉了好幾秒的其他
內容），也可能是兩個詞的時間戳互相重疊（後一個詞的 start 比前一個詞的
end 還早）。這種損壞單靠人工檢查很容易漏掉，寫進 edit_state.json 的
subtitles[] 就會出現「字幕比語音早跳出來」的偷跑現象。SKILL.md 裡雖然
教了怎麼判斷剪輯點時預先檢查，但那是「判斷層」的人工紀律，本身不會
自動執行——這支腳本才是真正的機械化安全網：出片後一定要跑一次，
不是「建議跑」，是流程的必要步驟（見 SKILL.md 第 5.5 步）。

用法：
    python verify_render.py "projects/<專案名稱>/06_meta/edit_state.json"

會自動找 05_render/ 底下最新的一支成品來驗證。需要 GROQ_API_KEY
（沒有設定的話會直接跳過驗證並印出提醒，不會擋住其他流程）。
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from av_tools import log, resolve_cli_path

DRIFT_WARN_SEC = 0.4
MATCH_PREFIX_LEN = 3
# 前綴比對在游標後面找不到時的備援搜尋範圍：只在「宣稱時間」前後這麼多
# 秒之內找，不要整篇亂找（見下面比對迴圈裡的說明）。
MATCH_FALLBACK_WINDOW_SEC = 6.0

# 逐字轉錄稿是純語音辨識輸出，幾乎不會有標點符號（Whisper/Groq 偶爾
# 會夾雜一兩個，但不穩定，不能依賴）；subtitles[] 的文字為了可讀性常常
# 會加逗號、頓號斷句。兩邊標點符號的有無不一致，用字面前 3 個字做比對
# 時只要標點剛好落在前 3 字內就永遠找不到（踩過這個坑：「對，這是」
# 這種字幕因為逗號卡在第 2 個字，被誤判成「完全找不到」，其實內容跟
# 時間都正常）。比對前把兩邊的標點都濾掉，只留有語音內容的文字。
_PUNCTUATION_RE = re.compile(
    r"[，。、！？：；「」『』〈〉《》（）,.!?:;()\"'‘’“”…—\-\s]"
)


def _strip_punctuation(text: str) -> str:
    return _PUNCTUATION_RE.sub("", text)


def _latest_render(project_dir: Path) -> Path | None:
    render_dir = project_dir / "05_render"
    candidates = sorted(render_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _transcribe_words(groq_client, media_path: Path) -> list[dict]:
    with media_path.open("rb") as f:
        resp = groq_client.audio.transcriptions.create(
            file=f,
            model="whisper-large-v3",  # 不用 turbo：時間戳精準度較差，見 new_project.py 的說明
            response_format="verbose_json",
            language="zh",
            timestamp_granularities=["word"],
        )
    words = getattr(resp, "words", None) or []
    # groq SDK 回傳的 words 有時是物件（.word/.start/.end），有時是
    # dict（["word"]/["start"]/["end"]），視版本而定，兩種都要能吃。
    def _get(w, key):
        return w[key] if isinstance(w, dict) else getattr(w, key)

    # Whisper/Groq 的中文輸出預設是簡體字，但 edit_state.json 裡的
    # subtitles[] 文字是繁體（new_project.py 產生逐字稿時已經用 opencc
    # 轉過）。這裡沒轉的話，繁簡不同字形會讓後面的子字串比對整批找不到
    # （踩過這個坑：第一次跑測試 189 個詞裡 16 句字幕都被誤判成「找不到」，
    # 其實內容完全正確，純粹是簡繁不同字形比對不到）。
    try:
        from opencc import OpenCC
        cc = OpenCC("s2twp")
    except ImportError:
        cc = None

    result = [{"word": _get(w, "word"), "start": _get(w, "start"), "end": _get(w, "end")} for w in words]
    if cc:
        for w in result:
            w["word"] = cc.convert(w["word"])
    return result


def _flatten_words_to_text_index(words: list[dict]) -> tuple[str, list[float]]:
    """
    把逐字詞流接成一整串純文字，同時記錄「文字裡每個字元對應到的真實
    開始時間」，讓後面可以用 subtitles[] 的文字內容去做子字串比對、
    直接查出那段文字實際被念出來的時間，不用自己另外寫對齊演算法。
    """
    text_chars = []
    char_times = []
    for w in words:
        word_text = w["word"]
        # 一個詞可能有好幾個字元，時間戳只有整個詞的起訖，這裡簡單假設
        # 詞內每個字元平均分攤時間（對抓「這段文字大概何時開始」已經
        # 夠精準，不需要逐字元精確時間）。
        n = max(len(word_text), 1)
        span = (w["end"] - w["start"]) / n
        for i, ch in enumerate(word_text):
            if _PUNCTUATION_RE.match(ch):
                continue
            text_chars.append(ch)
            char_times.append(w["start"] + i * span)
    return "".join(text_chars), char_times


def verify(edit_state_path: Path) -> int:
    project_dir = edit_state_path.parent.parent
    with edit_state_path.open(encoding="utf-8") as f:
        state = json.load(f)

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        log("[提醒] 沒有設定 GROQ_API_KEY，無法自動驗證字幕跟語音是否同步。")
        log("       出片前建議先設定好再跑一次這支腳本，不要跳過驗證直接交付。")
        return 1

    try:
        from groq import Groq
    except ImportError:
        log("[提醒] 沒有安裝 groq 套件（pip install groq），無法自動驗證，跳過。")
        return 1

    render_path = _latest_render(project_dir)
    if not render_path:
        log(f"在 {project_dir / '05_render'} 找不到任何成品，先跑 render.py。")
        return 1

    # edit_state.json 裡 subtitles[] 的時間是「假設每段剪出來剛好等於
    # end-start 長度」算出來的理想時間軸，跟真正剪出來、拼接後的時間軸
    # 幾乎一定有落差（幀率量化、片段尾端的安全緩衝等都會造成落差）。
    # render.py 出片時已經正確換算成真實時間軸去燒字幕，並把這份「真正
    # 燒進去的時間」存成同名的 .timeline.json sidecar 檔。這裡優先讀
    # 那份當比對基準，才是跟這支特定成品公平的比較；只有找不到 sidecar
    # 檔（例如這支成品是修這個機制之前的舊版 render.py 產生的）才退回
    # 用 edit_state.json 的原始理想時間，並提醒使用者精準度可能較低
    # ——不要用「理想時間」去對一支「已經被換算成真實時間」的成品，
    # 這正是這支腳本曾經被誤判一整批假警報的成因。
    timeline_path = render_path.with_suffix(".timeline.json")
    if timeline_path.exists():
        with timeline_path.open(encoding="utf-8") as f:
            timeline = json.load(f)
        subtitles = timeline.get("subtitles", [])
    else:
        log(f"[提醒] 找不到 {timeline_path.name}（可能是舊版 render.py 產生的成品，"
            f"沒有存這份 sidecar），改用 edit_state.json 的原始時間比對，"
            f"精準度可能較低——建議用目前版本的 render.py 重新出片一次。")
        subtitles = state.get("subtitles", [])

    if not subtitles:
        log("沒有 subtitles[] 可以比對，跳過驗證。")
        return 0

    log(f"重新轉錄成品做字幕同步驗證：{render_path.name} ...")
    client = Groq(api_key=api_key)
    words = _transcribe_words(client, render_path)
    if not words:
        log("[警告] 重新轉錄沒有拿到任何逐字時間戳，無法驗證，請人工聽過確認。")
        return 1

    full_text, char_times = _flatten_words_to_text_index(words)

    problems = []
    search_from = 0  # 字幕跟語音都是照時間順序排列，比對游標只往前走，
    # 不要每次都從頭找——不然像「然後」「我們」這種常見詞在全片裡出現
    # 好幾次，從頭找永遠只會抓到「最早出現的那一次」，跟這句字幕實際
    # 對應的那次完全無關，會製造出一堆假的「偷跑」警告
    # （實測踩過：第一版沒設游標，8 句完全正常的字幕被誤判成偷跑
    # 0.7-1.3 秒，其實只是抓到前面某句話裡同樣開頭字的舊位置）。
    for sub in subtitles:
        text = _strip_punctuation(sub.get("text", "").replace("\n", ""))
        declared_start = sub.get("start")
        if not text or declared_start is None:
            continue
        prefix = text[:MATCH_PREFIX_LEN]
        idx = full_text.find(prefix, search_from)
        if idx == -1:
            # 游標後面找不到最常見的原因不是「游標卡住」，是這句話前幾個
            # 字剛好被這次 ASR 轉錄成不同用字（實測踩過：「然後我把它
            # 貼上來」某次重新轉錄漏掉了「我」，變成「然後把它」，前3字
            # 的比對就完全找不到）。這種情況**不能整篇從頭找**——常見字
            # （「然後」「我們」之類）在全片裡出現好幾次，從頭找很容易
            # 撿到內容完全不相關、但剛好前3字一樣的其他位置，比對出離譜
            # 的秒數差（實測踩過：一句字幕被誤判成「延遲 10 秒」，其實
            # 只是抓到片頭另一句話裡同樣開頭的位置，這句話本身時間完全
            # 正常）。改成只在「宣稱時間附近一段合理範圍」內找同樣的前綴，
            # 範圍外真的找不到才算「找不到」，這樣就算某次轉錄用字跟
            # 預期的不完全一樣，也不會因為文字比對失敗而牽連出一個離譜
            # 的假秒數差。
            window_candidates = [
                i for i, t in enumerate(char_times)
                if abs(t - declared_start) <= MATCH_FALLBACK_WINDOW_SEC
                and full_text.startswith(prefix, i)
            ]
            idx = window_candidates[0] if window_candidates else -1
            if idx == -1:
                problems.append({
                    "text": text, "declared_start": declared_start,
                    "issue": "重新轉錄的成品裡，宣稱時間附近"
                             f"±{MATCH_FALLBACK_WINDOW_SEC:.0f} 秒範圍內完全找不到這句話的開頭幾個字"
                             "——可能是剪輯點選錯位置、這段內容根本沒被剪進最終影片，"
                             "或只是這次 ASR 剛好把開頭幾個字轉錄成不同用字，"
                             "務必人工核對（可以把這段單獨抓出來重新轉錄一次確認）。",
                })
                continue
        search_from = idx + len(prefix)
        real_start = char_times[idx]
        drift = declared_start - real_start
        if abs(drift) > DRIFT_WARN_SEC:
            # drift = 宣稱時間 - 真實時間。drift>0 代表宣稱的時間點比真實
            # 語音晚，也就是字幕會等到語音講完一陣子才跳出來（延遲）；
            # drift<0 代表宣稱的時間點比真實語音早，字幕會搶在語音講出來
            # 之前就跳出來（偷跑）——這裡曾經寫反過，測試時人工核對才抓到。
            direction = "延遲（字幕比語音晚出現）" if drift > 0 else "偷跑（字幕比語音早出現）"
            problems.append({
                "text": text, "declared_start": declared_start, "real_start": round(real_start, 2),
                "issue": f"{direction} {abs(drift):.2f} 秒，超過 {DRIFT_WARN_SEC} 秒門檻。",
            })

    if not problems:
        log(f"[通過] {len(subtitles)} 句字幕都跟重新轉錄的實際語音時間對得上（誤差在 {DRIFT_WARN_SEC} 秒內）。")
        return 0

    log(f"[警告] 發現 {len(problems)} 句字幕可能跟語音對不上，不要直接交付，先處理：")
    for p in problems:
        log(f"  - 「{p['text']}」（宣稱 start={p['declared_start']}）：{p['issue']}")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="出片後驗證字幕跟語音是否同步")
    parser.add_argument("edit_state", help="edit_state.json 的路徑")
    args = parser.parse_args()
    sys.exit(verify(resolve_cli_path(args.edit_state)))


if __name__ == "__main__":
    main()
