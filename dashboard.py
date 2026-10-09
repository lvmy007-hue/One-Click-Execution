# -*- coding: utf-8 -*-
"""本机作业监视台。流水只读；飞书对照与链路设置可在页面改。"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve().parent
CFG_PATH = HERE / "config.json"
HTML_PATH = HERE / "dashboard.html"
FUNDS_CACHE = HERE / "funds-board.json"
PORT = 8765
_FUNDS_LOCK = threading.Lock()
_FUNDS_RETRY_SEC = 180
_FUNDS_IDS = ("enhance", "email", "tracerfy", "precheck", "numbers")
SKIP = ("测试", "查邮箱", "查地址", "好评", "差评", "每日数据", "内部cyx",
        "链接查", "精准", "联系方式", "匹配成功", "匹配失败", "精简版")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_cfg() -> dict:
    cfg = {
        "dest_root": r"D:\桌面\地址匹配源文件\原始数据",
        "match_dest_root": r"D:\桌面\地址匹配源文件\匹配数据",
        "collect_cutoff": "09:30",
        "matching_url": "http://127.0.0.1:5000",
        "detect_url": "http://127.0.0.1:8848",
    }
    if CFG_PATH.is_file():
        cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
    return cfg


# 本机匹配/核验必须直连。系统开了 Clash 时 urllib 会走 HTTP_PROXY，
# 把 127.0.0.1:5000/8848 打到 7890，探测超时，页面刷新就会卡几秒。
_LOCAL = build_opener(ProxyHandler({}))


def get_json(url: str, timeout: float = 0.7) -> tuple[dict | None, int]:
    t0 = time.perf_counter()
    try:
        req = Request(url, headers={"Accept": "application/json"})
        with _LOCAL.open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        ms = int((time.perf_counter() - t0) * 1000)
        return (data if isinstance(data, dict) else None), ms
    except Exception:
        ms = int((time.perf_counter() - t0) * 1000)
        return None, ms


def http_up(url: str, timeout: float = 1.5) -> bool:
    try:
        with _LOCAL.open(Request(url), timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def tail_lines(path: Path, n: int = 40) -> list[str]:
    if n <= 0:
        return []
    return read_lines(path)[-n:]


def lines_on_day(path: Path, day: str) -> list[str]:
    prefix = f"{day} "
    return [ln for ln in read_lines(path) if ln.startswith(prefix)]


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")
DASH_PID = HERE / "dashboard.pid"
DETECT_PID = HERE / "detect.pid"


_PUBLIC_MASK = (
    (re.compile(r"Tracerfy", re.I), "剩余补全"),
    (re.compile(r"轻松邮"), "详细地址服务"),
    (re.compile(r"GeekSend", re.I), "邮箱预检"),
    (re.compile(r"CheckNumber", re.I), "号码检测"),
    (re.compile(r"scrape\.do", re.I), "邮箱通道"),
    (re.compile(r"EasyMail", re.I), "详细地址服务"),
    (re.compile(r"iMessage", re.I), "即时消息"),
    (re.compile(r"WhatsApp", re.I), "会话号码"),
    (re.compile(r"Apple ID", re.I), "账号邮箱"),
    (re.compile(r"apple-email", re.I), "账号邮箱"),
    (re.compile(r"apple_email", re.I), "账号邮箱"),
    (re.compile(r"飞书"), "推送"),
    (re.compile(r"微信收集"), "店表收集"),
    (re.compile(r"微信库"), "收集源"),
    (re.compile(r"微信"), "收集端"),
)


def mask_public(text: str) -> str:
    s = str(text or "")
    for pat, rep in _PUBLIC_MASK:
        s = pat.sub(rep, s)
    return s


def parse_log_line(line: str, src: str) -> dict:
    m = _TS.match(line)
    if m:
        return {"ts": m.group(1), "src": src, "text": mask_public(m.group(2)), "raw": line}
    return {"ts": "", "src": src, "text": mask_public(line), "raw": line}


def merged_logs(n: int = 160) -> list[dict]:
    pipe = [parse_log_line(x, "链路") for x in tail_lines(HERE / "pipeline.log", n)]
    col = [parse_log_line(x, "收集") for x in tail_lines(HERE / "collect.log", n)]
    rows = [r for r in (pipe + col) if r["raw"].strip()]
    rows.sort(key=lambda r: (r["ts"] or "0000", r["src"], r["raw"]))
    return rows[-n:]


def _pid_file_consoles() -> list[dict]:
    mapping = (
        (HERE / "collect_daemon.pid", "店表收集", "收集", "collect_wechat.py --daemon"),
        (HERE / "collect_supervise.pid", "收集守护", "收集", "collect_wechat.py --supervise"),
        (HERE / "pipeline.pid", "当日链路", "链路", "run_pipeline.py --daily"),
        (DETECT_PID, "核验闸门", "链路", "run_detect.py"),
        (HERE / "tray.pid", "窗口托盘", "监视", "tray_apps.py"),
        (DASH_PID, "作业监视", "监视", "dashboard.py"),
    )
    rows = []
    for path, name, kind, cmd in mapping:
        pid = read_pid(path)
        rows.append({
            "name": name, "kind": kind, "pid": pid or None,
            "alive": bool(pid) and pid_alive(pid), "cmd": cmd,
        })
    return rows


def list_project_procs() -> list[dict]:
    return _pid_file_consoles()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.GetLastError() == 5
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def write_pid(path: Path, pid: int | None = None) -> None:
    path.write_text(str(pid or os.getpid()), encoding="utf-8")


def clear_pid(path: Path, pid: int | None = None) -> None:
    cur = read_pid(path)
    if pid is not None and cur and cur != pid:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def day_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day)


def batch_day(arrived: datetime, hour: int, minute: int = 0) -> datetime:
    d = day_start(arrived)
    cut = d.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if arrived >= cut:
        return d + timedelta(days=1)
    return d


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f" {n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def list_files(folder: Path, exts: tuple[str, ...], extra_dirs: tuple[str, ...] = ()) -> list[dict]:
    out = []
    if not folder.is_dir():
        return out
    paths = list(folder.iterdir())
    for sub in extra_dirs:
        extra = folder / sub
        if extra.is_dir():
            paths.extend(extra.iterdir())
    for p in sorted(paths, key=lambda x: x.name.lower()):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        st = p.stat()
        blocked = any(b in p.name for b in SKIP)
        rel = p.name if p.parent == folder else f"{p.parent.name}/{p.name}"
        out.append({
            "name": rel,
            "size": st.st_size,
            "size_h": fmt_size(st.st_size).strip(),
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M:%S"),
            "skip": blocked,
        })
    return out


def log_age_sec(path: Path) -> float | None:
    if not path.is_file():
        return None
    return max(0.0, datetime.now().timestamp() - path.stat().st_mtime)


def item_day(it: dict) -> str:
    created = it.get("created_at") or 0
    if isinstance(created, (int, float)) and created > 0:
        ts = created / 1000.0 if created > 1e12 else created
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    if isinstance(created, str):
        raw = created.strip()
        if len(raw) >= 10 and raw[4] == "-":
            return raw[:10]
        try:
            md = raw.replace("/", "-").split()[0]
            month, day = int(md.split("-")[0]), int(md.split("-")[1])
            return datetime.now().replace(month=month, day=day).strftime("%Y-%m-%d")
        except (TypeError, ValueError, IndexError):
            if len(raw) >= 10:
                return raw[:10]
    return ""


def pick_today(items: list, today: str) -> dict | None:
    run = today_hit = None
    for it in items or []:
        st = str(it.get("status") or it.get("state") or "").lower()
        if st.startswith("run"):
            run = it
            break
        if item_day(it) == today and today_hit is None:
            today_hit = it
    return run or today_hit


def pct(cur, tot) -> int:
    try:
        tot = int(tot or 0)
        cur = int(cur or 0)
    except (TypeError, ValueError):
        return 0
    if tot <= 0:
        return 0
    return max(0, min(100, round(cur / tot * 100)))


def rate_per_min(cur, created) -> str:
    if not cur or not created:
        return ""
    try:
        if isinstance(created, str):
            t0 = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S")
        elif isinstance(created, (int, float)):
            ts = created / 1000.0 if created > 1e12 else created
            t0 = datetime.fromtimestamp(ts)
        else:
            return ""
        sec = max(1.0, (datetime.now() - t0).total_seconds())
        return f"{int(cur) / sec * 60:.1f} 条/分"
    except Exception:
        return ""


def map_state(raw: str, running_words=(), done_words=(), fail_words=()) -> str:
    s = (raw or "").lower()
    if any(s.startswith(w) or s == w for w in running_words):
        return "run"
    if s in done_words:
        return "done"
    if s in fail_words:
        return "fail"
    return "idle"


def _n(s: dict, *keys) -> int:
    for k in keys:
        v = s.get(k)
        if v is None or v == "":
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return 0


def engine_counts(name: str, s: dict) -> tuple[int, int, int]:
    """核验各引擎字段不同：邮箱用 valid/invalid，号码/Apple 用渠道计数字段。"""
    if name in ("即时消息", "iMessage"):
        return _n(s, "imessage"), _n(s, "sms"), _n(s, "unknown")
    if name in ("会话号码", "WhatsApp"):
        return _n(s, "whatsapp"), _n(s, "no"), _n(s, "unknown")
    if name in ("账号邮箱", "Apple ID"):
        return _n(s, "apple"), _n(s, "no"), _n(s, "unknown")
    return _n(s, "valid"), _n(s, "invalid"), _n(s, "unknown")


def engine_row(name: str, s: dict) -> dict:
    s = s or {}
    running = bool(s.get("running"))
    tot = _n(s, "total")
    ok, fail, unk = engine_counts(name, s)
    cur = _n(s, "current")
    if cur <= 0 and (ok + fail + unk):
        cur = ok + fail
    if tot <= 0 and (ok + fail + unk):
        tot = ok + fail + unk
    phase = str(s.get("phase") or s.get("progress") or "")
    log = str(s.get("log") or "")[:120]
    if running:
        state = "run"
    elif s.get("done") or ("完成" in phase and tot):
        state = "done"
    elif tot:
        state = "run" if running else "done"
    else:
        state = "idle"
    return {
        "name": name, "state": state, "phase": phase or "—", "log": log,
        "cur": cur, "tot": tot, "ok": ok, "fail": fail, "unk": unk,
        "pct": pct(cur, tot),
    }


def snapshot() -> dict:
    cfg = load_cfg()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    from collect_wechat import cutoff_hm, fmt_hm
    hour, minute = cutoff_hm(cfg)
    cal = day_start(now)
    nxt = cal + timedelta(days=1)
    live = batch_day(now, hour, minute)
    dest_root = Path(cfg["dest_root"])
    match_root = Path(cfg.get("match_dest_root") or r"D:\桌面\地址匹配源文件\匹配数据")
    cal_dir = dest_root / f"{cal.month}月" / f"{cal.month}.{cal.day:02d}"
    arc_dir = dest_root / f"{nxt.month}月" / f"{nxt.month}.{nxt.day:02d}"
    match_dir = match_root / f"{cal.month}月" / f"{cal.month}.{cal.day:02d}"
    stores = list_files(cal_dir, (".xlsx", ".xls"))
    stores_ok = [x for x in stores if not x["skip"]]
    afternoon = list_files(arc_dir, (".xlsx", ".xls"))
    found = list_files(match_dir / "收表汇总", (".xlsx", ".xls"))
    outputs = list_files(match_dir, (".xlsx", ".xls", ".zip"))

    daemon = read_pid(HERE / "collect_daemon.pid")
    supervise = read_pid(HERE / "collect_supervise.pid")
    pipe_pid = read_pid(HERE / "pipeline.pid")
    pipe_alive = bool(pipe_pid) and pid_alive(pipe_pid)
    pipe_age = log_age_sec(HERE / "pipeline.log")
    # 等 17:40 时日志可能几分钟不刷，不能只靠 pipe_age 判断日更是否还在
    chain_live = pipe_alive or (pipe_age is not None and pipe_age < 50)

    mbase = (cfg.get("matching_url") or "http://127.0.0.1:5000").rstrip("/")
    dbase = (cfg.get("detect_url") or "http://127.0.0.1:8848").rstrip("/")
    matching_up = http_up(f"{mbase}/modules", 1.5)
    match_ms = 0
    enh_raw = emi_raw = em_raw = tf_raw = None
    if matching_up:
        enh_raw, _ = get_json(f"{mbase}/address-enhance/tasks-list?page_size=8")
        emi_raw, _ = get_json(f"{mbase}/em-info/tasks?page_size=8")
        em_raw, _ = get_json(f"{mbase}/lookup/tasks-list?mode=email&page_size=8")
        tf_raw, _ = get_json(f"{mbase}/tracerfy/tasks")
    enhance = pick_today((enh_raw or {}).get("items") or [], today)
    emi_items = (emi_raw or {}).get("items") or []
    emi = next((it for it in emi_items if str(it.get("status") or "").lower() in ("em_running", "running")), None)
    if emi is None:
        emi = next((it for it in emi_items if item_day(it) == today), None)
    email = pick_today((em_raw or {}).get("items") or [], today)
    board, det_ms = get_json(f"{dbase}/api/board/summary", 0.6)
    detect_up = bool(board) and board.get("success") is not False
    smtp = geek = imsg = wa = apple = {}
    if detect_up:
        smtp, _ = get_json(f"{dbase}/api/email/progress?engine=smtp")
        geek, _ = get_json(f"{dbase}/api/email/progress?engine=geeksend")
        imsg, _ = get_json(f"{dbase}/api/imessage/progress")
        wa, _ = get_json(f"{dbase}/api/whatsapp/progress")
        apple, _ = get_json(f"{dbase}/api/apple-email/progress")
        smtp, geek, imsg, wa, apple = smtp or {}, geek or {}, imsg or {}, wa or {}, apple or {}

    em_live = None
    if matching_up and email and str(email.get("status") or "").lower().startswith("run"):
        tid = email.get("id") or email.get("task_id")
        if tid:
            em_live, _ = get_json(f"{mbase}/lookup/batch-status/{tid}")
            em_live = em_live or {}

    tf_list = (tf_raw or {}).get("tasks") or (tf_raw or {}).get("items") or []
    tracer = next((it for it in tf_list if str(it.get("status") or it.get("state") or "").lower()
                   in ("running", "queued", "processing", "submitted")), None)

    logs = tail_lines(HERE / "pipeline.log", 50)
    clog = tail_lines(HERE / "collect.log", 30)
    day_logs = lines_on_day(HERE / "pipeline.log", today)
    joined = "\n".join(day_logs)

    def hit(*keys: str) -> bool:
        return any(k in joined for k in keys)

    eh_state = "idle"
    eh = enhance or {}
    if eh:
        eh_state = map_state(
            str(eh.get("state") or ""),
            ("run", "poll", "queue", "export", "await"),
            ("done", "completed"),
            ("failed", "cancelled", "timeout"),
        )
        if str(eh.get("state") or "").lower() in ("polling", "queued", "exporting", "awaiting_names"):
            eh_state = "run"
    elif hit("详细地址数据库任务"):
        eh_state = "done"

    recent = day_logs[-8:]
    gate_wait = pipe_alive and any("再核验" in x for x in recent) and not any(
        "已到" in x and "开始核验" in x for x in recent
    )

    eh_fs_state = "idle"
    eh_fs_note = "等详细地址完成后推 ZIP"
    if hit("飞书已发送详细地址ZIP") or hit("已用最新详细地址 ZIP 覆盖"):
        eh_fs_state, eh_fs_note = "done", "已私聊 / 已归档"
    elif hit("详细地址飞书发送失败"):
        eh_fs_state, eh_fs_note = "fail", "失败，本地 ZIP 保留"
    elif eh_state == "done":
        eh_fs_state, eh_fs_note = "wait" if not hit("详细地址 ZIP 已保存") else "run", "下载并推送"
    if gate_wait and eh_fs_state == "run":
        eh_fs_state, eh_fs_note = "done", "匹配已完，等核验点"

    emi_state = "idle"
    emi = emi or {}
    if emi:
        emi_st = str(emi.get("status") or "").lower()
        if emi_st in ("em_running", "running"):
            emi_state = "run"
        else:
            emi_state = map_state(emi_st, ("run",), ("done",), ("failed", "cancelled", "error"))
    elif hit("联系方式已启动", "等待联系方式", "联系方式匹配"):
        emi_state = "run" if chain_live else "done"
    elif hit("联系方式已完成", "联系方式没有可提交行"):
        emi_state = "done"

    em_src = em_live or email or {}
    em_state = "idle"
    if email:
        em_state = map_state(str(email.get("status") or ""), ("run",), ("done",), ("failed", "cancelled", "error"))
    if emi_state == "run" and em_state == "idle":
        em_state = "wait"
    em_cur = int(em_src.get("processed_rows") or email.get("processed_rows") or 0) if email else 0
    em_tot = int(em_src.get("total_rows") or email.get("total_rows") or 0) if email else 0
    em_ok = int(em_src.get("success_rows") or email.get("success_rows") or 0) if email else 0
    em_fail = int(em_src.get("failed_rows") or email.get("failed_rows") or 0) if email else 0
    em_name = str((em_live or {}).get("current_name") or "")

    tf_state = "idle"
    if em_state == "run" or emi_state == "run":
        tf_state = "wait"
    elif tracer:
        tf_state = map_state(str(tracer.get("status") or tracer.get("state") or ""),
                             ("run", "queue", "process", "submit"),
                             ("done", "completed"),
                             ("failed", "error", "timeout"))
    elif hit("Tracerfy done", "Tracerfy 已完成"):
        tf_state = "done"
    elif hit("等待 Tracerfy", "Tracerfy 已提交"):
        tf_state = "run" if chain_live and not gate_wait else "done"
    if gate_wait and tf_state == "run":
        tf_state = "done"

    merge_state = "done" if hit("三源合并完成", "跳过三源合并", "成功包已在") else (
        "run" if hit("三源合并当日") else "idle"
    )
    if gate_wait and merge_state == "idle":
        merge_state = "done"
    collect_live = pid_alive(daemon)
    collect_state = "run" if collect_live else "idle"
    imp_state = "idle"
    if hit("导入核验系统"):
        imp_state = "done" if hit("导入完成") or hit("核验链路") else "run"
    zip_state = "done" if hit("汇总 ZIP 已下载", "汇总导出") else "idle"
    shop_map = cfg.get("feishu_shop_users") or {}
    shop_n = len(shop_map) if isinstance(shop_map, dict) else 0
    copy_n = len(_name_list(cfg.get("feishu_copy_to")))
    sum_a = _open_id_of(cfg, str(cfg.get("feishu_summary_a_to") or "").strip())
    push_on = bool(str(cfg.get("feishu_app_id") or "").strip())
    fs_extra = "私聊店员 + 抄送，不进群"
    if shop_n:
        fs_extra = f"{shop_n} 家店对照　抄送 {copy_n}　不进群"
    fs_state = "idle"
    if not push_on:
        fs_note = "未配置表格机器人"
    elif hit("整条链路完成", "核验链路完成"):
        if hit("按人已发送ZIP", "按店发送结束", "飞书已发送汇总A"):
            fs_state, fs_note = "done", "已私聊送达（含抄送）"
        elif hit("按店发送失败", "按店推送部分失败"):
            fs_state, fs_note = "fail", "私聊失败，本地 ZIP 保留"
        else:
            fs_state, fs_note = "wait", "核验完后按人私聊"
    elif zip_state == "done":
        fs_state, fs_note = "wait", "核验完后按人私聊"
    else:
        fs_note = "核验完后按人私聊，不进群"

    geek_row = engine_row("邮箱预检", geek)
    smtp_row = engine_row("发信核验", smtp)
    im_row = engine_row("即时消息", imsg)
    wa_row = engine_row("会话号码", wa)
    ap_row = engine_row("账号邮箱", apple)
    if hit("API预检") and geek_row["state"] == "idle" and chain_live:
        geek_row["state"] = "run"
    if hit("发信检测") and smtp_row["state"] == "idle" and chain_live:
        smtp_row["state"] = "run"
    detect_busy = any(r["state"] == "run" for r in (geek_row, smtp_row, im_row, wa_row, ap_row))
    detect_today = hit("导入核验系统", "核验链路", "开始核验", "API预检", "发信检测")
    if not detect_busy and not detect_today:
        for row in (geek_row, smtp_row, im_row, wa_row, ap_row):
            if row["state"] == "done":
                row["state"] = "idle"
                row["cur"] = row["tot"] = row["ok"] = row["fail"] = 0
                row["pct"] = 0

    jobs = [
        {"no": "01", "name": "店表收集", "state": collect_state,
         "id": f"PID {daemon}" if daemon else "—",
         "note": "登录收集端 + 文件自动下载",
         "cur": len(stores_ok), "tot": len(stores_ok),
         "ok": len(stores_ok), "fail": len(stores) - len(stores_ok),
         "extra": f"店表 {len(stores_ok)}　查出 {len(found)}　守护 {'在线' if pid_alive(supervise) else '未启动'}"},
        {"no": "02", "name": "详细地址", "state": eh_state,
         "id": str(eh.get("task_id") or eh.get("db_id") or "—"),
         "note": str(eh.get("original_filename") or "")[:80],
         "cur": int(eh.get("processed") or 0), "tot": int(eh.get("total") or 0),
         "ok": int(eh.get("success") or 0), "fail": int(eh.get("failed") or 0),
         "extra": str(eh.get("state") or "")},
        {"no": "02b", "name": "地址包推送", "state": eh_fs_state,
         "id": str(eh.get("task_id") or "—"),
         "note": eh_fs_note, "cur": 0, "tot": 0, "ok": 0, "fail": 0,
         "extra": "私聊 " + (str(cfg.get("feishu_enhance_to") or "17.").strip() or "17.") + "，不进群"},
        {"no": "02c", "name": "联系方式", "state": emi_state,
         "id": str(emi.get("id") or "—") if emi else "—",
         "note": mask_public(str(emi.get("message") or emi.get("filename") or "联系方式全量"))[:80],
         "cur": int(emi.get("phone") or 0) if emi else 0,
         "tot": int(emi.get("send") or emi.get("total") or 0) if emi else 0,
         "ok": int(emi.get("phone") or 0) + int(emi.get("email") or 0) if emi else 0,
         "fail": 0,
         "extra": str(emi.get("status_label") or emi.get("status") or "")},
        {"no": "03", "name": "邮箱匹配", "state": em_state,
         "id": str(email.get("id") or email.get("task_id") or "—") if email else "—",
         "note": em_name or str((email or {}).get("original_filename") or "")[:80],
         "cur": em_cur, "tot": em_tot, "ok": em_ok, "fail": em_fail,
         "extra": rate_per_min(em_cur, (email or {}).get("created_at")) or str((email or {}).get("status") or "")},
        {"no": "04", "name": "剩余补全", "state": tf_state,
         "id": str((tracer or {}).get("id") or (tracer or {}).get("task_id") or "—"),
         "note": "付费补全剩余" if tf_state != "idle" else "未开始",
         "cur": int((tracer or {}).get("hit_rows") or 0),
         "tot": int((tracer or {}).get("rows_submitted") or (tracer or {}).get("rows") or 0),
         "ok": int((tracer or {}).get("hit_rows") or 0), "fail": 0,
         "extra": "等待邮箱匹配结束" if em_state == "run" else str((tracer or {}).get("status") or "")},
        {"no": "05", "name": "三源合并", "state": merge_state,
         "id": "当日 overview", "note": match_dir.as_posix() if False else str(match_dir),
         "cur": len(outputs), "tot": len(outputs), "ok": len([x for x in outputs if x["name"].lower().endswith(".zip")]),
         "fail": 0, "extra": f"{len(outputs)} 个产出文件"},
        {"no": "06", "name": "核验导入", "state": imp_state, "id": "DetectSuite",
         "note": "三源 ZIP 导入拆分表", "cur": 0, "tot": 0, "ok": 0, "fail": 0, "extra": ""},
        {"no": "07", "name": geek_row["name"], "state": geek_row["state"], "id": "Aliyun",
         "note": geek_row["log"] or geek_row["phase"], "cur": geek_row["cur"], "tot": geek_row["tot"],
         "ok": geek_row["ok"], "fail": geek_row["fail"], "extra": geek_row["phase"]},
        {"no": "08", "name": smtp_row["name"], "state": smtp_row["state"], "id": "smtp",
         "note": smtp_row["log"] or smtp_row["phase"], "cur": smtp_row["cur"], "tot": smtp_row["tot"],
         "ok": smtp_row["ok"], "fail": smtp_row["fail"], "extra": smtp_row["phase"]},
        {"no": "09", "name": im_row["name"], "state": im_row["state"], "id": "imessage",
         "note": im_row["log"] or im_row["phase"], "cur": im_row["cur"], "tot": im_row["tot"],
         "ok": im_row["ok"], "fail": im_row["fail"], "extra": im_row["phase"]},
        {"no": "10", "name": wa_row["name"], "state": wa_row["state"], "id": "whatsapp",
         "note": wa_row["log"] or wa_row["phase"], "cur": wa_row["cur"], "tot": wa_row["tot"],
         "ok": wa_row["ok"], "fail": wa_row["fail"], "extra": wa_row["phase"]},
        {"no": "11", "name": ap_row["name"], "state": ap_row["state"], "id": "apple-email",
         "note": ap_row["log"] or ap_row["phase"], "cur": ap_row["cur"], "tot": ap_row["tot"],
         "ok": ap_row["ok"], "fail": ap_row["fail"], "extra": ap_row["phase"]},
        {"no": "12", "name": "汇总导出", "state": zip_state, "id": "summary/zip",
         "note": str(match_dir), "cur": 0, "tot": 0, "ok": 0, "fail": 0,
         "extra": "汇总A + 汇总B"},
        {"no": "13", "name": "消息推送", "state": fs_state, "id": "open_id 私聊",
         "note": fs_note, "cur": 0, "tot": 0, "ok": 0, "fail": 0,
         "extra": fs_extra + (f"　汇总A→{sum_a}" if sum_a else "")},
    ]
    for j in jobs:
        j["pct"] = pct(j["cur"], j["tot"])

    if gate_wait:
        jobs.insert(8, {
            "no": "05b", "name": "等待核验点", "state": "wait",
            "id": "found_cutoff", "note": "无收集端新查出，等到点再核验",
            "cur": 0, "tot": 0, "ok": 0, "fail": 0, "pct": 0,
            "extra": str(cfg.get("found_cutoff") or "17:40"),
        })

    # 微信收集是常驻守护，不算当前工序
    run_jobs = [j["name"] for j in jobs if j["state"] == "run" and j["name"] != "店表收集"]
    if run_jobs:
        active = " / ".join(run_jobs)
    elif gate_wait:
        active = "等待核验点"
    else:
        active = next((j["name"] for j in jobs if j["state"] == "wait"), None)
        if not active:
            active = next((j["name"] for j in jobs if j["state"] == "fail"), None)
        if not active:
            active = "收集待命" if collect_live else "空闲"
    if hit("整条链路完成") and not pipe_alive:
        active = "已完成"

    fw = None
    try:
        from funds import read_wait
        fw = read_wait()
    except Exception:
        fw = None
    if fw and fw.get("step"):
        step = str(fw.get("step") or "")
        aliases = {
            "详细地址匹配": ("详细地址",),
            "联系方式匹配": ("联系方式",),
            "邮箱匹配": ("邮箱匹配",),
            "剩余补全": ("剩余补全",),
            "Tracerfy": ("剩余补全",),
            "邮箱预检": ("邮箱预检",),
            "API预检": ("邮箱预检",),
            "号码检测": ("即时消息", "会话号码", "账号邮箱"),
        }
        extra = f"剩余 {fw.get('have')}，约需 {fw.get('need')}，等充值"
        for j in jobs:
            if j["name"] in aliases.get(step, (step,)):
                j["state"] = "wait"
                j["note"] = "余额不足，不跑，等充值"
                j["extra"] = extra
        active = "等待充值：" + step

    chain = "run" if pipe_alive or chain_live else "idle"
    if logs and "失败:" in logs[-1] and pipe_age is not None and pipe_age < 120:
        chain = "fail"
    if gate_wait or (fw and fw.get("step")):
        chain = "wait"
    elif hit("整条链路完成") and not pipe_alive:
        chain = "done"

    board_brief = {}
    if isinstance(board, dict):
        for k in ("shops", "emails", "phones", "waiting", "tasks", "today", "date",
                  "email_count", "phone_count", "order_count"):
            if k in board:
                board_brief[k] = board[k]

    return {
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": "一二三四五六日"[now.weekday()],
        "cutoff": fmt_hm(hour, minute),
        "calendar": f"{cal.month}.{cal.day:02d}",
        "archive": f"{live.month}.{live.day:02d}",
        "cal_path": str(cal_dir),
        "arc_path": str(arc_dir),
        "match_path": str(match_dir),
        "chain": chain,
        "active": active,
        "pipe_age": None if pipe_age is None else int(pipe_age),
        "matching": {"up": matching_up, "url": mbase, "ms": match_ms},
        "detect": {"up": detect_up, "url": dbase, "ms": det_ms, "board": board_brief},
        "collect": {
            "daemon": pid_alive(daemon), "daemon_pid": daemon or None,
            "supervise": pid_alive(supervise), "supervise_pid": supervise or None,
        },
        "feishu": {
            "app": push_on,
            "chat": False,
            "alert": bool(str(cfg.get("feishu_alert_app_id") or "").strip()),
            "shops": shop_n,
            "state": fs_state,
            "note": "私聊不进群" + ("　告警已接" if str(cfg.get("feishu_alert_app_id") or "").strip() else "　告警未接"),
        },
        "jobs": jobs,
        "stores": stores,
        "afternoon": afternoon,
        "found": found,
        "outputs": outputs,
        "engines": [geek_row, smtp_row, im_row, wa_row, ap_row],
        "email": {
            "id": (email or {}).get("id"),
            "status": (email or {}).get("status"),
            "file": (email or {}).get("original_filename"),
            "created": (email or {}).get("created_at"),
            "current": em_name,
            "ok": em_ok, "fail": em_fail, "cur": em_cur, "tot": em_tot,
            "pct": pct(em_cur, em_tot),
            "rate": rate_per_min(em_cur, (email or {}).get("created_at")),
        },
        "enhance": {
            "id": (enhance or {}).get("task_id"),
            "state": (enhance or {}).get("state"),
            "file": (enhance or {}).get("original_filename"),
            "ok": int((enhance or {}).get("success") or 0),
            "fail": int((enhance or {}).get("failed") or 0),
            "tot": int((enhance or {}).get("total") or 0),
        },
        "log": logs,
        "collect_log": clog,
        "terminals": list_project_procs(),
        "console": merged_logs(160),
        "concurrency": cfg.get("email_sd_concurrency") or 50,
    }


def _name_list(raw) -> list[str]:
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _split_cells(raw) -> list[str]:
    """店名/姓名格支持逗号分隔：一人多店、一店多人。"""
    import re
    if isinstance(raw, list):
        out: list[str] = []
        for x in raw:
            out.extend(_split_cells(x))
        return out
    text = str(raw or "").strip()
    if not text:
        return []
    return [x.strip() for x in re.split(r"[,，;；、\s]+", text) if x.strip()]


def _open_id_of(cfg: dict, name: str) -> str:
    n = str(name or "").strip()
    if not n:
        return ""
    if n.startswith("ou_") or n.startswith("on_"):
        return n
    oids = cfg.get("feishu_open_ids") or {}
    if isinstance(oids, dict):
        hit = str(oids.get(n) or "").strip()
        if hit:
            return hit
    return n


def _store_receiver(cfg: dict, raw: str) -> str:
    n = str(raw or "").strip()
    if not n:
        return ""
    if n.startswith("ou_") or n.startswith("on_"):
        oids = cfg.get("feishu_open_ids") or {}
        if isinstance(oids, dict):
            for name, oid in oids.items():
                if str(oid or "").strip() == n:
                    return str(name)
        return n
    return n


def feishu_settings() -> dict:
    cfg = load_cfg()
    shops = []
    raw = cfg.get("feishu_shop_users") or {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            shop = str(k).strip()
            users = v if isinstance(v, list) else [v]
            for u in users:
                name = str(u).strip()
                if shop and name:
                    shops.append({"shop": shop, "user": name})
    elif isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict):
                shop = str(row.get("shop") or "").strip()
                user = str(row.get("user") or "").strip()
                if shop and user:
                    shops.append({"shop": shop, "user": user})
    return {
        "shops": shops,
        "enhance_to": str(cfg.get("feishu_enhance_to") or ""),
        "summary_a_to": _open_id_of(cfg, str(cfg.get("feishu_summary_a_to") or "")),
        "copy_to": _name_list(cfg.get("feishu_copy_to")),
        "alert_to": _name_list(cfg.get("feishu_alert_to")),
        "alert_app_id": str(cfg.get("feishu_alert_app_id") or ""),
        "alert_on": bool(str(cfg.get("feishu_alert_app_id") or "").strip()
                         and str(cfg.get("feishu_alert_app_secret") or "").strip()),
        "push_on": bool(str(cfg.get("feishu_app_id") or "").strip()),
    }


def save_feishu_settings(body: dict) -> dict:
    data = json.loads(CFG_PATH.read_text(encoding="utf-8")) if CFG_PATH.is_file() else {}
    mapping: dict[str, list[str]] = {}
    for row in body.get("shops") or []:
        if not isinstance(row, dict):
            continue
        shops = _split_cells(row.get("shop"))
        users = _split_cells(row.get("user"))
        for shop in shops:
            bucket = mapping.setdefault(shop, [])
            for user in users:
                if user not in bucket:
                    bucket.append(user)
    data["feishu_shop_users"] = mapping
    data["feishu_enhance_to"] = str(body.get("enhance_to") or "").strip()
    data["feishu_summary_a_to"] = _store_receiver(data, str(body.get("summary_a_to") or "").strip())
    data["feishu_copy_to"] = _name_list(body.get("copy_to"))
    data["feishu_alert_to"] = _name_list(body.get("alert_to"))
    CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return feishu_settings()


def _as_bool(v, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "是"):
        return True
    if s in ("0", "false", "no", "off", "否", ""):
        return False
    return default


def pipeline_settings() -> dict:
    cfg = load_cfg()
    headers = cfg.get("need_headers") or []
    if isinstance(headers, list):
        headers_text = "，".join(str(x) for x in headers)
    else:
        headers_text = str(headers)
    return {
        "wechat_root": str(cfg.get("wechat_root") or ""),
        "drop_dir": str(cfg.get("drop_dir") or ""),
        "dest_root": str(cfg.get("dest_root") or ""),
        "enhance_archive_root": str(cfg.get("enhance_archive_root") or ""),
        "match_dest_root": str(cfg.get("match_dest_root") or ""),
        "collect_cutoff": str(cfg.get("collect_cutoff") or "09:30"),
        "found_cutoff": str(cfg.get("found_cutoff") or "17:40"),
        "found_noon_hour": int(cfg.get("found_noon_hour") or 12),
        "found_rule_start": str(cfg.get("found_rule_start") or ""),
        "poll_seconds": float(cfg.get("poll_seconds") or 2),
        "stable_seconds": float(cfg.get("stable_seconds") or 1.2),
        "matching_url": str(cfg.get("matching_url") or ""),
        "detect_url": str(cfg.get("detect_url") or ""),
        "matching_exe": str(cfg.get("matching_exe") or ""),
        "detect_exe": str(cfg.get("detect_exe") or ""),
        "matching_mode": str(cfg.get("matching_mode") or "2"),
        "email_sd_concurrency": int(cfg.get("email_sd_concurrency") or 50),
        "need_headers": headers_text,
        "feishu_push_after_detect": _as_bool(cfg.get("feishu_push_after_detect", True), True),
        "feishu_shop_as_zip": _as_bool(cfg.get("feishu_shop_as_zip", True), True),
        "skip_tri_if_done": _as_bool(cfg.get("skip_tri_if_done", True), True),
    }


def save_pipeline_settings(body: dict) -> dict:
    data = json.loads(CFG_PATH.read_text(encoding="utf-8")) if CFG_PATH.is_file() else {}
    for key in (
        "wechat_root", "drop_dir", "dest_root", "enhance_archive_root", "match_dest_root",
        "collect_cutoff", "found_cutoff", "found_rule_start",
        "matching_url", "detect_url", "matching_exe", "detect_exe", "matching_mode",
    ):
        if key in body:
            data[key] = str(body.get(key) or "").strip()
    try:
        data["found_noon_hour"] = int(body.get("found_noon_hour") or 12)
    except Exception:
        data["found_noon_hour"] = 12
    try:
        data["poll_seconds"] = float(body.get("poll_seconds") or 2)
    except Exception:
        data["poll_seconds"] = 2
    try:
        data["stable_seconds"] = float(body.get("stable_seconds") or 1.2)
    except Exception:
        data["stable_seconds"] = 1.2
    try:
        data["email_sd_concurrency"] = max(1, int(body.get("email_sd_concurrency") or 50))
    except Exception:
        data["email_sd_concurrency"] = 50
    headers = _split_cells(body.get("need_headers"))
    if headers:
        data["need_headers"] = headers
    data["feishu_push_after_detect"] = _as_bool(body.get("feishu_push_after_detect"), True)
    data["feishu_shop_as_zip"] = _as_bool(body.get("feishu_shop_as_zip"), True)
    data["skip_tri_if_done"] = _as_bool(body.get("skip_tri_if_done"), True)
    CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return pipeline_settings()


def _kill_run_pipeline_pids() -> list[int]:
    import subprocess
    killed: list[int] = []
    pid_file = HERE / "pipeline.pid"
    old = read_pid(pid_file)
    if old:
        subprocess.run(["taskkill", "/PID", str(old), "/T", "/F"], capture_output=True)
        killed.append(old)
    try:
        # /V 带命令行；比 CIM 快，避免监视页点「重启日更」卡住
        out = subprocess.check_output(
            ["wmic", "process", "where",
             "CommandLine like '%run_pipeline.py%'", "get", "ProcessId", "/value"],
            text=True, stderr=subprocess.DEVNULL, timeout=8,
        )
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("ProcessId="):
                continue
            pid_s = line.split("=", 1)[-1].strip()
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            if pid in killed:
                continue
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            killed.append(pid)
    except Exception:
        pass
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass
    return killed


_DAY_FOLDER_RE = re.compile(r"^\d{1,2}\.\d{2}$")
_DAY_KEY_RE = re.compile(r"^(\d{1,2}月)/(\d{1,2}\.\d{2})$")


def _match_root() -> Path:
    return Path(load_cfg().get("match_dest_root") or r"D:\桌面\地址匹配源文件\匹配数据")


def _summary_zips(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.glob("*.zip") if p.is_file() and "汇总" in p.name),
        key=lambda p: p.name,
    )


def _history_day_folder(key: str) -> Path:
    m = _DAY_KEY_RE.match(str(key or "").replace("\\", "/").strip())
    if not m:
        raise ValueError("日期无效")
    folder = _match_root() / m.group(1) / m.group(2)
    if not folder.is_dir():
        raise ValueError("没有这一天的匹配目录")
    return folder


def history_push_days() -> dict:
    root = _match_root()
    days: list[dict] = []
    if root.is_dir():
        folders: list[Path] = []
        for month in root.iterdir():
            if not month.is_dir() or not month.name.endswith("月"):
                continue
            for d in month.iterdir():
                if d.is_dir() and _DAY_FOLDER_RE.match(d.name) and _summary_zips(d):
                    folders.append(d)
        folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for d in folders[:60]:
            zips = _summary_zips(d)
            latest = max(p.stat().st_mtime for p in zips)
            try:
                mon = int(d.parent.name.replace("月", ""))
                dayn = int(d.name.split(".")[1])
                year = datetime.fromtimestamp(latest).year
                iso = datetime(year, mon, dayn).strftime("%Y-%m-%d")
            except ValueError:
                iso = datetime.fromtimestamp(latest).strftime("%Y-%m-%d")
            days.append({
                "key": f"{d.parent.name}/{d.name}",
                "label": f"{d.parent.name} {d.name}",
                "iso": iso,
                "mtime": datetime.fromtimestamp(latest).strftime("%m-%d %H:%M"),
                "zips": [
                    {"name": p.name, "size_h": fmt_size(p.stat().st_size).strip()}
                    for p in zips
                ],
            })
    return {"days": days}


def history_push_preview(key: str) -> dict:
    from send_feishu import _zip_xlsx_bytes, extract_shop, match_shop_users
    cfg = load_cfg()
    folder = _history_day_folder(key)
    zips = _summary_zips(folder)
    shops: dict[str, dict] = {}
    for zp in zips:
        for name, _raw in _zip_xlsx_bytes(zp):
            shop = extract_shop(name) or name
            rec = shops.setdefault(shop, {"shop": shop, "files": [], "users": [], "zip": zp.name})
            if name not in rec["files"]:
                rec["files"].append(name)
            for _s, user in match_shop_users(cfg, name):
                if user not in rec["users"]:
                    rec["users"].append(user)
    mapped = [x for x in shops.values() if x["users"]]
    mapped.sort(key=lambda x: x["shop"])
    return {
        "key": f"{folder.parent.name}/{folder.name}",
        "zips": [{"name": p.name, "size_h": fmt_size(p.stat().st_size).strip()} for p in zips],
        "shops": mapped,
        "skipped": sum(1 for x in shops.values() if not x["users"]),
        "summary_a_to": _open_id_of(cfg, str(cfg.get("feishu_summary_a_to") or "")),
        "copy_to": _name_list(cfg.get("feishu_copy_to")),
    }


def _pick_history_zips(folder: Path, packs: list, *, require_pack: bool = True) -> list[Path]:
    zips = _summary_zips(folder)
    tags = {str(x).strip().upper() for x in (packs or []) if str(x).strip()}
    if not tags:
        if require_pack:
            raise ValueError("请至少勾选汇总A或汇总B")
        picked = zips
    elif tags >= {"A", "B"}:
        picked = zips
    else:
        picked = []
        if "A" in tags:
            picked.extend(p for p in zips if "汇总A" in p.name)
        if "B" in tags:
            picked.extend(p for p in zips if "汇总B" in p.name)
    if not picked:
        raise ValueError("这一天没有符合条件的汇总 ZIP")
    return picked


def history_push(body: dict) -> dict:
    from send_feishu import notify_shop_zips, notify_zip
    key = str(body.get("day") or "").strip()
    mode = str(body.get("mode") or "shops").strip()
    folder = _history_day_folder(key)
    zips = _pick_history_zips(
        folder, body.get("packs") or [],
        require_pack=(mode != "shops"),
    )
    cfg = load_cfg()
    label = f"{folder.parent.name} {folder.name}"
    if mode == "shops":
        only = [str(x).strip() for x in (body.get("shops") or []) if str(x).strip()]
        if not only:
            raise ValueError("请勾选要补发的店")
        notify_shop_zips(cfg, zips, only_shops=only)
        return {"ok": True, "msg": f"已补发 {label} 按店私聊（{len(only)} 家店，含抄送，不进群）"}
    to = str(body.get("to") or "").strip()
    if mode == "summary_a":
        to = to or str(cfg.get("feishu_summary_a_to") or "").strip()
        a_zips = [p for p in zips if "汇总A" in p.name]
        if not a_zips:
            raise ValueError("没有汇总A")
        if not to:
            raise ValueError("请填写汇总A接收人")
        for p in a_zips:
            notify_zip(cfg, p, text=f"核验汇总A（补发 {label}）：{p.name}",
                       log_ok="飞书已补发汇总A", to=to)
        return {"ok": True, "msg": f"已把 {label} 汇总A发给 {to}"}
    if mode == "zip":
        if not to:
            raise ValueError("整包补发请填写接收人姓名")
        for p in zips:
            notify_zip(cfg, p, text=f"核验汇总补发（{label}）：{p.name}",
                       log_ok="飞书已补发ZIP", to=to)
        return {"ok": True, "msg": f"已把 {label} 所选 ZIP 发给 {to}"}
    raise ValueError("未知推送方式")


_ROW_COST = {
    "enhance": 0.25,
    "contact": 0.25,
    "email": 40,
    "tracerfy": 1,
    "precheck": 1,
    "numbers": 0.0002,
}


def _remain_rows(total, per_row):
    try:
        t = float(total)
        p = float(per_row)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return max(0, int(t / p))


def _public_fund_item(it: dict) -> dict:
    row = dict(it) if isinstance(it, dict) else {}
    row["name"] = mask_public(row.get("name") or "")
    accts = []
    for a in row.get("accounts") or []:
        if not isinstance(a, dict):
            continue
        one = dict(a)
        one["label"] = str(one.get("label") or "账号")
        one.pop("path", None)
        one.pop("token", None)
        one.pop("error_raw", None)
        if one.get("error"):
            one["error"] = "查询失败"
        accts.append(one)
    row["accounts"] = accts
    if row.get("error"):
        row["error"] = "未查到" if row["error"] not in ("未配置", "未使用付费接口") else row["error"]
    fid = str(row.get("id") or "")
    if row.get("remain_rows") is None and row.get("ok") and row.get("total") is not None:
        per = row.get("per_row") if row.get("per_row") not in (None, "") else _ROW_COST.get(fid)
        row["remain_rows"] = _remain_rows(row.get("total"), per)
    return row


def _funds_today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fund_item_ok(it: dict) -> bool:
    return bool(it.get("ok")) and it.get("total") is not None


def _read_funds_cache() -> dict:
    try:
        data = json.loads(FUNDS_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_funds_cache(data: dict) -> None:
    tmp = FUNDS_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FUNDS_CACHE)


def _funds_pending(items: list) -> list[str]:
    have = {str(it.get("id") or "") for it in items if isinstance(it, dict)}
    pending = [str(it.get("id") or "") for it in items
               if isinstance(it, dict) and str(it.get("id") or "") and not _fund_item_ok(it)]
    for fid in _FUNDS_IDS:
        if fid not in have:
            pending.append(fid)
    out = []
    for fid in pending:
        if fid and fid not in out:
            out.append(fid)
    return out


def _merge_fund_items(old: list, new: list) -> list:
    by_id = {}
    order = []
    for it in list(old or []) + list(new or []):
        if not isinstance(it, dict):
            continue
        fid = str(it.get("id") or "")
        if not fid:
            continue
        prev = by_id.get(fid)
        if prev and _fund_item_ok(prev) and not _fund_item_ok(it):
            continue
        if fid not in by_id:
            order.append(fid)
        by_id[fid] = it
    ranked = [fid for fid in _FUNDS_IDS if fid in by_id]
    ranked += [fid for fid in order if fid not in ranked]
    return [by_id[fid] for fid in ranked]


def _fetch_funds_live() -> dict:
    cfg = load_cfg()
    mbase = (cfg.get("matching_url") or "http://127.0.0.1:5000").rstrip("/")
    dbase = (cfg.get("detect_url") or "http://127.0.0.1:8848").rstrip("/")
    m, _ = get_json(f"{mbase}/api/funds", 25)
    d, _ = get_json(f"{dbase}/api/funds", 25)
    items = []
    if isinstance(m, dict):
        items.extend(_public_fund_item(x) for x in (m.get("items") or []) if isinstance(x, dict))
    if isinstance(d, dict):
        items.extend(_public_fund_item(x) for x in (d.get("items") or []) if isinstance(x, dict))
    merged = []
    for it in items:
        fid = str(it.get("id") or "")
        if fid == "contact":
            continue
        if fid == "enhance":
            it["name"] = "详细地址 / 联系方式"
        merged.append(it)
    merged = _fill_easymail_from_web(merged)
    return {
        "items": merged,
        "matching_up": bool(m),
        "detect_up": bool(d),
    }


def _matching_data_dir() -> str:
    exe = Path((load_cfg().get("matching_exe") or r"E:\数据匹配系统\DataMatching.exe"))
    sidecar = exe.parent / "DataMatching.datadir"
    try:
        if sidecar.is_file():
            d = sidecar.read_text(encoding="utf-8-sig").strip().strip('"').strip("'")
            if d:
                return d
    except Exception:
        pass
    return str(exe.parent / "data")


def _fill_easymail_from_web(items: list) -> list:
    if any(str(it.get("id") or "") == "enhance" and _fund_item_ok(it) for it in items):
        return items
    data_dir = _matching_data_dir()
    os.environ["DATAMATCHING_DATA_DIR"] = data_dir
    src = str((HERE.parent / "Data Matching").resolve())
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import api_keys
        from easymail_client import EasyMailClient, get_web_balance
        accts = api_keys._accounts("easymail") or []
        a = accts[0] if accts else {}
        url = str(a.get("base_url") or "").strip() or "https://api.easymail-sz.com/api/v1"
        client = EasyMailClient(base_url=url, api_key=str(a.get("api_key") or "web"))
        bal = get_web_balance(client.web_origin(), str(a.get("web_account") or "").strip())
    except Exception:
        return items
    if not bal.get("ok"):
        return items
    row = _public_fund_item({
        "id": "enhance",
        "name": "详细地址 / 联系方式",
        "unit": "元",
        "ok": True,
        "total": bal.get("balance"),
        "per_row": 0.25,
        "remain_rows": _remain_rows(bal.get("balance"), 0.25),
        "accounts": [{"label": "账号1", "ok": True, "balance": bal.get("balance")}],
        "error": "",
    })
    row["name"] = "详细地址 / 联系方式"
    out, done = [], False
    for it in items:
        if str(it.get("id") or "") == "enhance":
            out.append(row)
            done = True
        else:
            out.append(it)
    if not done:
        out.insert(0, row)
    return out


def refresh_funds_cache() -> dict:
    live = _fetch_funds_live()
    with _FUNDS_LOCK:
        cache = _read_funds_cache()
        day = _funds_today()
        old = cache.get("items") or [] if cache.get("day") == day else []
        items = _merge_fund_items(old, live.get("items") or [])
        out = {
            "ok": True,
            "day": day,
            "items": items,
            "matching_up": live.get("matching_up"),
            "detect_up": live.get("detect_up"),
            "queried_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pending": _funds_pending(items),
        }
        _write_funds_cache(out)
        return out


def funds_board(refresh: bool = False) -> dict:
    from funds import RERUN_STEPS, read_wait
    day = _funds_today()
    if refresh:
        cache = refresh_funds_cache()
    else:
        with _FUNDS_LOCK:
            cache = _read_funds_cache()
        if cache.get("day") != day:
            cache = {
                "ok": True,
                "day": day,
                "items": [],
                "matching_up": False,
                "detect_up": False,
                "queried_at": "",
                "pending": list(_FUNDS_IDS),
            }
    wait = read_wait()
    if isinstance(wait, dict) and wait.get("step"):
        wait = dict(wait)
        wait["step"] = mask_public(wait["step"])
    return {
        "ok": True,
        "items": cache.get("items") or [],
        "waiting": wait,
        "steps": [{"id": k, "name": n} for k, n in RERUN_STEPS],
        "matching_up": bool(cache.get("matching_up")),
        "detect_up": bool(cache.get("detect_up")),
        "day": cache.get("day") or day,
        "queried_at": cache.get("queried_at") or "",
        "pending": cache.get("pending") or _funds_pending(cache.get("items") or []),
    }


def _funds_worker():
    while True:
        try:
            cache = _read_funds_cache()
            day = _funds_today()
            stale = cache.get("day") != day or not cache.get("queried_at")
            pending = True if stale else bool(cache.get("pending"))
            if stale or pending:
                refresh_funds_cache()
                cache = _read_funds_cache()
                pending = bool(cache.get("pending"))
            time.sleep(_FUNDS_RETRY_SEC if pending else 300)
        except Exception:
            time.sleep(60)


def rerun_from_step(body: dict) -> dict:
    from funds import RERUN_STEPS
    key = str(body.get("from") or body.get("step") or "").strip().lower()
    names = dict(RERUN_STEPS)
    if key not in names:
        raise ValueError("请选择从哪一步重跑")
    (HERE / "rerun-from.txt").write_text(key, encoding="utf-8")
    msg = restart_daily_pipeline()
    return {
        "ok": True,
        "from": key,
        "name": names[key],
        "msg": f"将从「{names[key]}」重跑后面整条。" + (msg.get("msg") or ""),
    }


def _pythonw() -> Path:
    py = Path(sys.executable)
    if py.name.lower() == "python.exe":
        alt = py.with_name("pythonw.exe")
        if alt.is_file():
            return alt
    return py


def _port_busy(port: int = PORT) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def restart_dashboard() -> dict:
    """立刻回包，再拉起新看板并退出当前进程。"""
    import subprocess
    import threading

    py = _pythonw()
    script = str(HERE / "dashboard.py")
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000

    def work():
        time.sleep(0.35)
        try:
            subprocess.Popen(
                [str(py), script, "--relaunch"],
                cwd=str(HERE),
                close_fds=True,
                creationflags=flags,
            )
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "msg": "进度看板正在重启，几秒后自动刷新"}


def restart_daily_pipeline() -> dict:
    """立刻返回，后台杀旧进程再拉起，避免 HTTP 线程卡住把页面拖死。"""
    import subprocess
    import threading

    killed_box: dict = {"killed": []}

    def work():
        killed_box["killed"] = _kill_run_pipeline_pids()
        pyw = Path(sys.executable)
        if pyw.name.lower() == "python.exe":
            alt = pyw.with_name("pythonw.exe")
            if alt.is_file():
                pyw = alt
        try:
            subprocess.Popen(
                [str(pyw), str(HERE / "tray_apps.py"), "--launch-daily"],
                cwd=str(HERE),
                close_fds=True,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        except Exception:
            return

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "started": True, "msg": "已在后台重启日更，几秒后看作业页进程"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/snapshot":
            self._json(200, snapshot())
            return
        if path == "/api/feishu-settings":
            self._json(200, feishu_settings())
            return
        if path == "/api/feishu-history":
            try:
                qs = parse_qs(urlparse(self.path).query)
                day = (qs.get("day") or [""])[0].strip()
                self._json(200, history_push_preview(day) if day else history_push_days())
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if path == "/api/pipeline-settings":
            self._json(200, pipeline_settings())
            return
        if path == "/api/funds-board":
            try:
                qs = parse_qs(urlparse(self.path).query)
                fresh = (qs.get("refresh") or [""])[0].strip().lower() in ("1", "true", "yes")
                self._json(200, funds_board(refresh=fresh))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        html = HTML_PATH.read_bytes() if HTML_PATH.is_file() else b"<p>missing dashboard.html</p>"
        self._send(200, html, "text/html; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/feishu-settings":
            try:
                self._json(200, save_feishu_settings(self._read_json()))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if path == "/api/pipeline-settings":
            try:
                self._json(200, save_pipeline_settings(self._read_json()))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if path == "/api/restart-dashboard":
            try:
                self._json(200, restart_dashboard())
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if path == "/api/restart-daily":
            try:
                self._json(200, restart_daily_pipeline())
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if path == "/api/rerun":
            try:
                self._json(200, rerun_from_step(self._read_json()))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if path == "/api/feishu-history-push":
            try:
                self._json(200, history_push(self._read_json()))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if path == "/api/feishu-test-alert":
            try:
                from send_feishu import send_alert_text
                ok = send_alert_text(
                    load_cfg(),
                    "告警机器人测试\n报错将发到这个会话，不进业务群。",
                    log_ok="飞书已发送告警测试",
                )
                if not ok:
                    self._json(400, {"error": "发送失败。把接收人加进告警应用可用范围。"})
                    return
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})


def main():
    import atexit
    if "--relaunch" in sys.argv:
        for _ in range(50):
            if not _port_busy(PORT):
                break
            time.sleep(0.2)
        time.sleep(0.2)
    write_pid(DASH_PID)
    atexit.register(lambda: clear_pid(DASH_PID, os.getpid()))
    threading.Thread(target=_funds_worker, name="funds-daily", daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"作业监视  http://127.0.0.1:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        clear_pid(DASH_PID, os.getpid())


if __name__ == "__main__":
    main()
