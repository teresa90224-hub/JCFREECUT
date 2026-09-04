#!/usr/bin/env python3
"""
caption_editor/server.py
-------------------------
edit_state.json 的本機網頁編輯器。取代原本綁在 SRT 上的舊版原型
（已刪除），改成直接讀寫跟 render.py 共用的同一份 edit_state.json
schema，改完直接呼叫 render.py 出片，不用另外維護一套燒錄邏輯。

範圍（對應三個手動調整需求）：
    1. 字幕 subtitles[]：文字（含專有名詞校正）、起訖時間（斷句/重切）、
       金句 emphasis 開關、新增/刪除/排序。
    2. 排版 layout：畫面比例、背景色。
    3. 字卡內容：title.text / cta.text（樣式沿用 edit_state.json 既有
       設定，這一版先只給文字內容可編輯）。

用法：
    python server.py
    （不用帶專案路徑，啟動後在網頁裡選專案 + 選哪個 edit_state*.json）

    啟動後開瀏覽器 http://localhost:8770
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    from flask import Flask, jsonify, request, send_from_directory, abort
except ImportError:
    print("找不到 flask，請先安裝：pip install flask")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # 短影音編輯器/
PROJECTS_DIR = ROOT / "projects"
STATIC_DIR = HERE / "static"
RENDER_SCRIPT = ROOT / "render.py"

app = Flask(__name__, static_folder=None)


def _env_with_registry_keys() -> dict:
    """
    背景轉錄子行程用的環境變數：以目前 process 的 os.environ 為底，
    但 GROQ_API_KEY 一律直接從 Windows 使用者登錄檔重新讀一次再覆蓋
    上去。這支伺服器本身可能是在 GROQ_API_KEY 用 setx 設定「之前」
    就啟動的（例如從沒重開過的終端機視窗啟動），這種情況繼承到的
    os.environ 會沒有這個變數，導致轉錄悄悄掉到本機 CPU 的
    faster-whisper（慢很多、準確度也較低）而不是預期的 Groq。
    跟專案裡其他地方讀 API key 的作法一致：只在子行程環境裡出現，
    不會被 Claude 看到。
    """
    env = dict(os.environ)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "[Environment]::GetEnvironmentVariable('GROQ_API_KEY','User')"],
            capture_output=True, text=True, timeout=10,
        )
        key = result.stdout.strip()
        if key:
            env["GROQ_API_KEY"] = key
    except (OSError, subprocess.TimeoutExpired):
        pass
    return env

# 背景 render job 狀態（in-memory，夠用——這是單人本機工具，不需要持久化）
JOBS = {}
JOBS_LOCK = threading.Lock()


def _safe_resolve(rel_path: str) -> Path:
    """把前端傳來的相對路徑（相對於 ROOT）解析成絕對路徑，並確認沒有跳出 projects/ 範圍。"""
    candidate = (ROOT / rel_path).resolve()
    try:
        candidate.relative_to(PROJECTS_DIR.resolve())
    except ValueError:
        abort(403, description="路徑必須在 projects/ 底下")
    return candidate


@app.route("/api/states")
def list_states():
    """
    掃描所有專案底下的 06_meta/edit_state*.json，給前端做下拉選單——
    但只列出 status == "approved" 的檔案。

    這個 GUI 是「微調」用的，不是「從頭選片」用的：Claude 用 short-
    video-cut 技能剪出一版、算出 edit_state.json（初始 status 是
    "draft"）之後，要先讓使用者看過算好的成片，使用者明確說可以了，
    Claude 才把 status 改成 "approved"，這個檔案才會出現在這裡讓使用
    者自己微調字幕/排版/字卡。中途 Claude 自己測試、實驗用的
    edit_state 檔案永遠是 draft，不會混進使用者要挑的清單裡。
    """
    results = []
    if not PROJECTS_DIR.exists():
        return jsonify(results)
    for meta_dir in sorted(PROJECTS_DIR.glob("*/06_meta")):
        for f in sorted(meta_dir.glob("edit_state*.json")):
            try:
                state = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if state.get("status") != "approved":
                continue
            rel = f.relative_to(ROOT).as_posix()
            results.append({
                "rel_path": rel,
                "project_dir": meta_dir.parent.name,
                "filename": f.name,
                "project_name": state.get("project_name", f.stem),
                "topic": state.get("topic", ""),
            })
    return jsonify(results)


@app.route("/api/state/<path:rel_path>", methods=["GET"])
def get_state(rel_path):
    path = _safe_resolve(rel_path)
    if not path.exists():
        abort(404)
    state = json.loads(path.read_text(encoding="utf-8"))
    source_video = (path.parent.parent / state["source"]["video_path"]).relative_to(ROOT).as_posix()
    return jsonify({"state": state, "source_video_rel": source_video})


def _backup_before_overwrite(path: Path) -> None:
    """
    存檔前先把目前的檔案內容備份一份，保留最近 10 份。逐字編輯器的
    「套用」+「存檔」是使用者自己在瀏覽器裡操作、沒有 Claude 在旁邊
    核對內容的流程，實際發生過一次誤觸按鈕把整份字幕時間搞亂又直接
    存檔覆蓋掉的情況——那次是靠 Claude 自己還記得上一版內容才救回來，
    不是長久可靠的復原方式。有自動備份的話，之後同樣的情況使用者可以
    自己從 .backups/ 資料夾把最近一份正常的檔案複製回來，不用碰運氣。
    """
    if not path.exists():
        return
    backup_dir = path.parent / ".backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{path.stem}.{stamp}.json"
    shutil.copy2(path, backup_path)
    backups = sorted(backup_dir.glob(f"{path.stem}.*.json"))
    for stale in backups[:-10]:
        stale.unlink()


@app.route("/api/state/<path:rel_path>", methods=["POST"])
def save_state(rel_path):
    path = _safe_resolve(rel_path)
    if not path.exists():
        abort(404)
    new_state = request.get_json(force=True)
    if not isinstance(new_state, dict) or "clips" not in new_state or "subtitles" not in new_state:
        abort(400, description="送來的資料看起來不是完整的 edit_state 物件")
    _backup_before_overwrite(path)
    # Windows 上文字模式寫檔預設會把 \n 轉成 \r\n，後面 render.py 用
    # json.loads 讀回來不受影響，但統一用 \n 避免跟專案裡其他檔案不一致
    path.write_text(
        json.dumps(new_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    return jsonify({"status": "saved"})


def _run_render_job(job_id: str, rel_path: str):
    path = _safe_resolve(rel_path)
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    cmd = [sys.executable, str(RENDER_SCRIPT), str(path)]
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    log_lines = []
    for line in proc.stdout:
        log_lines.append(line.rstrip("\n"))
        with JOBS_LOCK:
            JOBS[job_id]["log"] = "\n".join(log_lines[-200:])
    proc.wait()
    with JOBS_LOCK:
        if proc.returncode == 0:
            state = json.loads(path.read_text(encoding="utf-8"))
            project_dir = path.parent.parent
            out_path = _latest_render(project_dir, state["project_name"])
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["output_rel"] = out_path.relative_to(ROOT).as_posix() if out_path else None
        else:
            JOBS[job_id]["status"] = "error"


@app.route("/api/render/<path:rel_path>", methods=["POST"])
def start_render(rel_path):
    _safe_resolve(rel_path)  # 驗證路徑合法性，結果不需要
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": "", "output_rel": None, "started": time.time()}
    threading.Thread(target=_run_render_job, args=(job_id, rel_path), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/render_status/<job_id>")
def render_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


def _latest_render(project_dir: Path, project_name: str) -> Path | None:
    """
    render.py 現在每次都另存新檔（`<project_name>_<時間戳記>.mp4`），
    這裡找同一個專案名底下最新的那一份，給 GUI 預覽用。找不到就回傳
    None（例如這個 edit_state 還沒 render 過）。
    """
    render_dir = project_dir / "05_render"
    if not render_dir.exists():
        return None
    candidates = sorted(render_dir.glob(f"{project_name}_*.mp4"))
    return candidates[-1] if candidates else None


@app.route("/api/latest_render/<path:rel_path>")
def latest_render(rel_path):
    state_path = _safe_resolve(rel_path)
    if not state_path.exists():
        abort(404)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    project_dir = state_path.parent.parent
    out_path = _latest_render(project_dir, state["project_name"])
    return jsonify({"exists": out_path is not None,
                     "output_rel": out_path.relative_to(ROOT).as_posix() if out_path else None})


def _words_path_for(project_dir: Path, source_video: Path) -> Path | None:
    """
    找逐字時間戳檔案。同一個 <stem>.words.json 可能躺在兩個地方：
        - source_video 旁邊（這支編輯器自己轉錄時存的位置）
        - 專案的 02_transcript/（new_project.py 建專案，或之前手動轉錄
          某個中間檔時存的位置——例如教育版的 topicAB_final.words.json
          就是這樣來的）
    兩邊都找不到才回傳 None，表示真的要轉錄一次。找到了就直接用，
    不要無腦重轉——重轉除了浪費 API 額度，也可能因為兩次轉錄結果
    有些微差異而讓 GUI 顯示的字跟已經存的 subtitles[] 對不太起來。
    """
    stem = source_video.stem
    candidates = [
        source_video.parent / f"{stem}.words.json",
        project_dir / "02_transcript" / f"{stem}.words.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


@app.route("/api/words/<path:rel_path>", methods=["GET"])
def get_words(rel_path):
    """
    逐字編輯分頁用。rel_path 是 edit_state*.json 的路徑，逐字時間戳
    要對應 state.source.video_path 這個檔案自己的時間軸（不是原始
    錄影檔的時間軸——教育版這類專案的 source 本身就已經是剪過一次
    的中間檔，兩者時間軸不同，不能借用建專案時對原始錄影做的那份
    words.json；但如果之前已經對「這個檔案自己」轉錄過、存在
    02_transcript/ 或影片旁邊，就直接沿用，不重轉）。
    """
    state_path = _safe_resolve(rel_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    project_dir = state_path.parent.parent
    source_video = project_dir / state["source"]["video_path"]
    words_path = _words_path_for(project_dir, source_video)
    if words_path is None:
        return jsonify({"exists": False})
    words = json.loads(words_path.read_text(encoding="utf-8"))
    # Groq/Whisper 轉錄偶爾會吐出「有時間戳、但文字是空字串」的詞（通常是
    # 氣音/雜音被判斷成一個詞卻辨識不出內容），逐字編輯器會照樣把它畫成
    # 一個 word-tok 方塊，看起來就像一格空白——這格不是使用者編輯造成的，
    # 過濾掉最單純，反正空字串本來就不該佔一個可編輯的字詞位置。
    words = [w for w in words if w.get("word", "").strip()]
    words = _filter_words_to_kept_clips(words, state.get("clips", []))
    return jsonify({"exists": True, "words": words})


def _filter_words_to_kept_clips(words: list[dict], clips: list[dict]) -> list[dict]:
    """
    只保留落在某個 keep=true clip 範圍內的字。逐字編輯器是拿來微調
    「已經剪出來的內容」用的，不是拿來重新從整支原始影片挑段落——
    像行銷短影音那種從 17 分鐘裡挑 6 段不連續片段的專案，來源逐字稿
    本來就是整支影片的，不濾掉的話畫面上會混進一堆完全用不到的文字，
    而且下面 initWordState 的斷句點比對邏輯也會失準（見該處註解）。
    """
    kept = [c for c in clips if c.get("keep")]
    if not kept:
        return words
    return [w for w in words if any(c["start"] <= w["start"] <= c["end"] for c in kept)]


def _run_words_job(job_id: str, rel_path: str):
    state_path = _safe_resolve(rel_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    source_video = state_path.parent.parent / state["source"]["video_path"]
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from pathlib import Path; from new_project import run_whisper; "
        "run_whisper(Path(sys.argv[2]), Path(sys.argv[3]))"
    )
    cmd = [sys.executable, "-c", code, str(ROOT), str(source_video), str(source_video.parent)]
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=_env_with_registry_keys(),
    )
    log_lines = []
    for line in proc.stdout:
        log_lines.append(line.rstrip("\n"))
        with JOBS_LOCK:
            JOBS[job_id]["log"] = "\n".join(log_lines[-200:])
    proc.wait()
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "done" if proc.returncode == 0 else "error"


@app.route("/api/words/<path:rel_path>", methods=["POST"])
def start_words_job(rel_path):
    _safe_resolve(rel_path)
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": "", "started": time.time()}
    threading.Thread(target=_run_words_job, args=(job_id, rel_path), daemon=True).start()
    return jsonify({"job_id": job_id})


def _merge_clips(clips):
    """把相鄰、keep 值相同的 clips 合併，避免逐字刪字後 clips[] 過度破碎。"""
    if not clips:
        return clips
    clips = sorted(clips, key=lambda c: c["start"])
    merged = [dict(clips[0])]
    for c in clips[1:]:
        last = merged[-1]
        if c["keep"] == last["keep"] and abs(c["start"] - last["end"]) < 0.005:
            last["end"] = c["end"]
        else:
            merged.append(dict(c))
    return merged


def _carve_cuts(clips, cut_ranges):
    """在既有 clips[] 的 keep=true 區段裡，把 cut_ranges（逐字刪除產生的時間段）挖成 keep=false。"""
    result = list(clips)
    for cs, ce in cut_ranges:
        next_result = []
        for c in result:
            if not c["keep"] or c["end"] <= cs or c["start"] >= ce:
                next_result.append(c)
                continue
            if c["start"] < cs:
                next_result.append({"start": c["start"], "end": cs, "keep": True, "reason": c.get("reason", "")})
            next_result.append({"start": max(c["start"], cs), "end": min(c["end"], ce), "keep": False, "reason": "GUI 逐字編輯：刪除"})
            if c["end"] > ce:
                next_result.append({"start": ce, "end": c["end"], "keep": True, "reason": c.get("reason", "")})
        result = next_result
    return _merge_clips(result)


def _shift_fn(clips):
    """算出「原始時間軸時間 -> 剪完之後（kept 影片）時間軸時間」的映射函式。"""
    kept_clips = sorted([c for c in clips if c["keep"]], key=lambda c: c["start"])
    breakpoints = []  # (原始start, 原始end, 該片段開始時對應的輸出時間)
    acc = 0.0
    for c in kept_clips:
        breakpoints.append((c["start"], c["end"], acc))
        acc += c["end"] - c["start"]

    def shift(t):
        for cs, ce, out_start in breakpoints:
            if t < cs:
                return out_start  # 落在被剪掉的區段之前，夾到最近的保留段開頭
            if cs <= t <= ce:
                return out_start + (t - cs)
        return acc  # 超過最後一段，夾到結尾

    return shift


def _kept_to_source_fn(clips):
    """
    跟 _shift_fn 方向相反：把「剪完之後（kept 影片）時間軸」的時間換算回
    「原始來源影片」的時間軸。broll[] 存的時間是舊的 kept 時間軸（跟
    render.py 疊圖用的是同一份，不是來源時間），要先用這個換回來源時間，
    才能再用新的 clips 算出的 shift() 換算成新的 kept 時間——直接把
    broll 的舊 kept 時間當成來源時間丟進 shift() 是錯的（實測發生過：
    broll 的時間通常是個位數到幾十秒的小數字，遠小於真正的來源時間戳
    動輒兩三百秒，會被 shift() 誤判成「落在第一段之前」，全部退化成 0，
    算出來的長度變成 0 就被當成無效片段整批刪掉）。
    """
    kept_clips = sorted([c for c in clips if c["keep"]], key=lambda c: c["start"])
    breakpoints = []  # (該片段開始時對應的輸出時間, 片段長度, 原始start)
    acc = 0.0
    for c in kept_clips:
        breakpoints.append((acc, c["end"] - c["start"], c["start"]))
        acc += c["end"] - c["start"]

    def to_source(t):
        for out_start, dur, src_start in breakpoints:
            if t < out_start + dur:
                return src_start + max(0.0, t - out_start)
        return kept_clips[-1]["end"] if kept_clips else t

    return to_source


@app.route("/api/apply_word_edits/<path:rel_path>", methods=["POST"])
def apply_word_edits(rel_path):
    """
    逐字編輯分頁按「套用」時呼叫。前端把目前逐字編輯畫面的狀態整包送
    來（哪些字被刪、斷句點在哪、哪些卡是金句），這裡統一在「原始時間
    軸」上運算：
        1. 被刪除的字 -> 在 clips[] 挖出新的 keep=false 洞
        2. 依斷句點把剩下的字重新分組成 subtitles[]
        3. 用新的 clips[] 算出時間平移函式，把 subtitles/broll 的時間
           一次性換算成剪完之後的（kept 影片）時間軸
    只回傳算好的結果，不落地存檔——存檔還是走既有的 /api/state。
    """
    payload = request.get_json(force=True)
    words = payload["words"]  # [{word,start,end,deleted,merged}]
    boundaries = set(payload["boundaries"])  # word 索引集合，代表「這個字是新一句的開頭」
    emphasis_map = {int(k): v for k, v in payload.get("emphasis", {}).items()}
    base_clips = payload["clips"]
    broll = payload.get("broll", [])

    # deleted=True 代表使用者真的按了 × 剪掉這個字（連影片一起剪）；
    # merged=True 是「整段編輯」把好幾個字收斂成一個 token 時，被收進去
    # 的那些原始字——文字上不再單獨顯示，但對應的影片／聲音沒有被剪掉，
    # 千萬不能跟 deleted 混在一起算進 cut_ranges，不然「整段編輯」會把
    # 那整段話從影片裡剪掉。
    cut_ranges = [(w["start"], w["end"]) for w in words if w.get("deleted")]
    new_clips = _carve_cuts(base_clips, cut_ranges) if cut_ranges else list(base_clips)
    shift = _shift_fn(new_clips)

    cards = []
    current = None
    for i, w in enumerate(words):
        if w.get("deleted") or w.get("merged"):
            continue
        if current is None or i in boundaries:
            if current is not None:
                cards.append(current)
            current = {"text": w["word"], "start": w["start"], "end": w["end"],
                       "emphasis": bool(emphasis_map.get(i, False))}
        else:
            current["text"] += w["word"]
            current["end"] = w["end"]
    if current is not None:
        cards.append(current)

    new_subtitles = []
    for c in cards:
        ns, ne = shift(c["start"]), shift(c["end"])
        if ne - ns <= 0.02 or not c["text"].strip():
            continue
        # source_start/source_end：這句字幕在「原始來源影片」裡精確對應
        # 的時間範圍，直接來自逐字時間戳（words.json），不是猜的。有這
        # 兩個欄位，下次打開逐字編輯器就能直接查表切出正確的字範圍，不
        # 用再靠「時間最接近」去反推、猜錯邊界（實測發生過：猜的斷句點
        # 少算了開頭一個字，刪除範圍因此比使用者要刪的短，剪完還聽得到
        # 殘留的字）。
        new_subtitles.append({
            "text": c["text"], "start": round(ns, 3), "end": round(ne, 3), "emphasis": c["emphasis"],
            "source_start": round(c["start"], 3), "source_end": round(c["end"], 3),
        })

    # broll[] 的時間是「套用前」那份 edit_state.json 的 kept 時間軸，
    # 要先用 base_clips（套用前的 clips）換回來源時間，再用新 clips
    # 算出的 shift() 換成套用後的新 kept 時間——兩段轉換都要做，不能
    # 直接拿 broll 的時間當來源時間丟進 shift()。
    kept_to_source = _kept_to_source_fn(base_clips)
    new_broll = []
    for b in broll:
        src_start, src_end = kept_to_source(b["start"]), kept_to_source(b["end"])
        bs, be = shift(src_start), shift(src_end)
        if be - bs <= 0.02:
            continue
        nb = dict(b)
        nb["start"], nb["end"] = round(bs, 3), round(be, 3)
        new_broll.append(nb)

    return jsonify({"clips": new_clips, "subtitles": new_subtitles, "broll": new_broll})


@app.route("/media/<path:rel_path>")
def media(rel_path):
    path = _safe_resolve(rel_path)
    if not path.exists():
        abort(404)
    return send_from_directory(path.parent, path.name)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


def main():
    port = 8770
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    print(f"edit_state 編輯器啟動：http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
