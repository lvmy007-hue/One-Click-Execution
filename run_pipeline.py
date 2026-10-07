# -*- coding: utf-8 -*-
"""当日店表 → 详细地址 → 联系方式全量 → 邮箱全量 → Tracerfy 剩余 → 三源当日合并。"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DEFAULT_CFG = HERE / "config.json"
LOG_FILE = HERE / "pipeline.log"
PIPELINE_PID = HERE / "pipeline.pid"
SKIP_NAME = ("测试", "查邮箱", "查地址", "好评", "差评", "每日数据", "内部cyx",
             "链接查", "精准", "联系方式", "匹配成功", "匹配失败", "精简版")

try:
    from collect_wechat import dest_dir, load_cfg, month_dir_name, date_dir_name
except ImportError:
    dest_dir = load_cfg = month_dir_name = date_dir_name = None  # type: ignore


def log(*args):
    line = " ".join(str(a) for a in args)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = f"{stamp} {line}"
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


class PipelineError(RuntimeError):
    pass


def set_console_title(title: str = "一键执行") -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    s.trust_env = False
    return s


def _json(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        raise PipelineError(f"HTTP {resp.status_code} 非 JSON: {resp.text[:300]}")
    if not isinstance(data, dict):
        raise PipelineError(f"HTTP {resp.status_code} 返回异常: {data!r}"[:300])
    if resp.status_code >= 400:
        raise PipelineError(data.get("error") or f"HTTP {resp.status_code}")
    if data.get("error"):
        raise PipelineError(str(data["error"]))
    return data


def pick_col(columns: list, keywords: list[str]) -> str:
    cols = [str(c) for c in columns]
    for k in keywords:
        for c in cols:
            if c == k or c.lower() == k.lower():
                return c
    for k in keywords:
        kl = k.lower()
        for c in cols:
            if kl in c.lower():
                return c
    return ""


def stem_key(name: str) -> str:
    stem = Path(str(name)).stem
    stem = re.sub(r"\(\d+\)$", "", stem).strip()
    return stem.lower()


def name_ymd(name: str) -> str:
    raw = Path(str(name)).stem
    m = re.search(r"(20\d{6})", raw)
    if m:
        return m.group(1)
    m = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", raw)
    if m:
        return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    m = re.search(r"(?<!\d)(\d{1,2})[-./](\d{1,2})(?!\d)", raw)
    if m:
        return f"{datetime.now().year:04d}{int(m.group(1)):02d}{int(m.group(2)):02d}"
    m = re.search(r"(?<!\d)(\d{2})(\d{2})(?!\d)", raw)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{datetime.now().year:04d}{month:02d}{day:02d}"
    return ""


def dest_keep_dates(cfg: dict) -> set[str]:
    days = {datetime.now().strftime("%Y%m%d")}
    for p in dest_xlsx(cfg):
        ymd = name_ymd(p.name)
        if ymd:
            days.add(ymd)
    return days


def dest_xlsx(cfg: dict) -> list[Path]:
    # 匹配用自然日当天目录（截止后当天目录已收齐，新表进次日）
    now = datetime.now()
    if dest_dir:
        from collect_wechat import day_start, is_store_table
        folder = dest_dir(cfg, day_start(now))
    else:
        from collect_wechat import is_store_table
        folder = Path(cfg["dest_root"]) / f"{now.month}月" / f"{now.month}.{now.day:02d}"
    need = cfg.get("need_headers") or ["店铺", "买家全名", "城市", "州", "邮编"]
    files = []
    for p in sorted(folder.glob("*.xlsx")):
        if any(b in p.name for b in SKIP_NAME):
            continue
        ok, _ = is_store_table(p, need)
        if not ok:
            continue
        files.append(p)
    return files


def already_in_enhance(http: requests.Session, base: str, files: list[Path]) -> tuple[list[Path], list[Path]]:
    try:
        data = _json(http.get(f"{base}/address-enhance/tasks-list", params={"page_size": 30}, timeout=30))
    except Exception as e:
        log("读取详细地址任务失败，按全部新文件处理:", e)
        return files, []
    today = datetime.now().strftime("%Y-%m-%d")
    seen: set[str] = set()
    for it in data.get("items") or []:
        created = it.get("created_at") or 0
        if isinstance(created, (int, float)) and created > 1e12:
            created = created / 1000.0
        day = ""
        if isinstance(created, (int, float)) and created > 0:
            day = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
        elif isinstance(created, str):
            day = created[:10]
        if day != today:
            continue
        name = it.get("original_filename") or ""
        # EXE 用「 + 」拼接多文件；E2店+2026.10.6 这种文件名里的 + 不是分隔符
        for part in re.split(r"\s+\+\s+", name):
            k = stem_key(part.strip())
            if k:
                seen.add(k)
    fresh, old = [], []
    for p in files:
        if stem_key(p.name) in seen:
            old.append(p)
        else:
            fresh.append(p)
    return fresh, old


def wait_app(http: requests.Session, base: str, exe: str, timeout: int = 90) -> None:
    def up() -> bool:
        try:
            r = http.get(f"{base}/modules", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    try:
        from tray_apps import ensure_running
        ensure_running()
    except Exception:
        pass
    if up():
        log("数据匹配系统已在运行", base)
        return
    if not exe or not Path(exe).is_file():
        raise PipelineError(f"数据匹配系统未打开，且找不到 {exe}")
    log("正在启动", exe)
    try:
        from tray_apps import launch_exe
        launch_exe(exe)
    except Exception:
        subprocess.Popen([exe], cwd=str(Path(exe).parent), close_fds=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if up():
            log("数据匹配系统已就绪")
            return
        time.sleep(2)
    raise PipelineError("数据匹配系统启动超时")


def guess_enhance_map(columns: list) -> dict:
    return {
        "name_col": pick_col(columns, ["买家全名", "fullname", "full name", "recipient", "name", "姓名"]),
        "city_col": pick_col(columns, ["city", "城市"]),
        "state_col": pick_col(columns, ["state", "州", "province"]),
        "postalcode_col": pick_col(columns, ["postal", "zip", "邮编"]),
        "country_col": pick_col(columns, ["country", "国家"]),
        "order_col": pick_col(columns, ["order", "订单"]),
        "asin_col": pick_col(columns, ["asin"]),
        "title_col": pick_col(columns, ["title", "标题"]),
        "store_col": pick_col(columns, ["store", "shop", "店铺", "店名", "storename"]),
    }


def guess_email_map(columns: list) -> dict:
    return {
        "name_col": pick_col(columns, ["匹配姓名", "[查询]匹配姓名", "买家全名", "name", "姓名", "full_name", "fullname"]),
        "state_col": pick_col(columns, ["state", "州", "province"]),
        "city_col": pick_col(columns, ["city", "城市", "town"]),
        "zip_col": pick_col(columns, ["zip", "zipcode", "邮编", "postal"]),
        "addr_col": pick_col(columns, ["匹配地址", "address", "地址", "street", "addr"]),
        "email_col": pick_col(columns, ["email", "邮箱", "e-mail", "mail"]),
        "store_col": pick_col(columns, ["store", "shop", "店铺", "店名", "seller", "卖家"]),
    }


def guess_tf_map(columns: list) -> dict:
    cols = [str(c) for c in columns]
    addr = ""
    for rule in (
        lambda c: c == "匹配地址",
        lambda c: c.lower() in ("address", "地址", "详细地址", "街道地址"),
        lambda c: ("地址" in c or "address" in c.lower()) and not re.search(r"状态|城市|州|city|state|zip|postal|status", c, re.I),
    ):
        hit = next((c for c in cols if rule(c)), "")
        if hit:
            addr = hit
            break
    m = {
        "name": pick_col(cols, ["匹配姓名", "买家全名", "name", "姓名", "buyer"]),
        "address": addr,
        "city": pick_col(cols, ["city", "城市"]),
        "state": pick_col(cols, ["state", "州"]),
        "zip": pick_col(cols, ["zip", "邮编"]),
        "store": pick_col(cols, ["店铺", "store", "店铺名称"]),
    }
    return m


def _problem_files(problems: list, files: list[Path]) -> list[Path]:
    names = []
    for item in problems or []:
        raw = str(item.get("file") or item.get("filename") or "")
        if raw:
            names.append(Path(raw).name)
    hit = []
    for p in files:
        if p.name in names or stem_key(p.name) in {stem_key(n) for n in names}:
            hit.append(p)
    return hit


def _problem_reason(item: dict) -> str:
    n = item.get("rows") or 0
    bits = []
    if item.get("empty_name"):
        bits.append(f"买家全名为空 {item['empty_name']}/{n} 行")
    if item.get("empty_store"):
        bits.append(f"店铺为空 {item['empty_store']}/{n} 行")
    extra = str(item.get("message") or item.get("reason") or "").strip()
    if extra and extra not in bits:
        bits.append(extra)
    return "，".join(bits) or "店铺或买家全名空值超过 90%，无法导入"


def _reason_for_file(path: Path, problems: list) -> str:
    key = stem_key(path.name)
    for item in problems or []:
        raw = str(item.get("file") or item.get("filename") or "")
        if Path(raw).name == path.name or stem_key(raw) == key:
            return _problem_reason(item)
    return "店铺或买家全名空值超过 90%，无法导入"


def notify_bad_table(cfg: dict | None, path: Path, reason: str) -> None:
    from send_feishu import notify_alert
    notify_alert(
        cfg, "店表导入失败，已跳过", reason,
        filename=path.name, extra="其余合格店表继续匹配。", path=path,
    )


def start_enhance(http: requests.Session, base: str, files: list[Path], matching_mode: str,
                  cfg: dict | None = None) -> str:
    remaining = list(files)
    while remaining:
        log("导入详细地址匹配:", "、".join(p.name for p in remaining))
        opened = []
        try:
            payload = []
            for p in remaining:
                fh = p.open("rb")
                opened.append(fh)
                payload.append(("files", (p.name, fh, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")))
            prev = _json(http.post(f"{base}/address-enhance/batch-preview", files=payload, timeout=180))
        finally:
            for fh in opened:
                fh.close()
        log(f"预览 {prev.get('total_rows')} 行  temp={prev.get('temp_file')}")
        cmap = guess_enhance_map(prev.get("columns") or [])
        need = ("name_col", "city_col", "state_col", "postalcode_col", "store_col")
        missing = [k for k in need if not cmap.get(k)]
        if missing:
            raise PipelineError("列映射失败: " + "、".join(missing) + " 列=" + str(prev.get("columns")))
        chk = _json(http.post(f"{base}/address-enhance/batch-check", data={
            "temp_file": prev["temp_file"],
            "name_col": cmap["name_col"],
            "store_col": cmap["store_col"],
        }, timeout=60))
        problems = chk.get("problems") or []
        if problems:
            bad = _problem_files(problems, remaining)
            if not bad:
                raise PipelineError(chk.get("message") or "店铺或买家全名为空，无法导入")
            for p in bad:
                reason = _reason_for_file(p, problems)
                log("跳过不合格店表", p.name, reason)
                try:
                    notify_bad_table(cfg, p, reason)
                except Exception as e:
                    log("错误店表飞书发送失败:", e)
            remaining = [p for p in remaining if p not in bad]
            if not remaining:
                raise PipelineError("全部店表不合格，无法导入详细地址")
            continue
        fd = {
            "temp_file": prev["temp_file"],
            "original_filename": prev.get("original_filename") or remaining[0].name,
            "matching_mode": matching_mode or "2",
            "order_label": f"一键-{datetime.now().strftime('%m.%d')}",
            **cmap,
        }
        started = _json(http.post(f"{base}/address-enhance/batch-execute", data=fd, timeout=60))
        tid = started.get("task_id")
        if not tid:
            raise PipelineError("详细地址任务未返回 task_id")
        log("详细地址任务已启动", tid)
        return str(tid)
    raise PipelineError("没有可导入的店表")


def wait_enhance(http: requests.Session, base: str, task_id: str) -> dict:
    log("等待详细地址匹配完成（轻松邮通常要十几到几十分钟）")
    last = ""
    while True:
        data = _json(http.get(f"{base}/address-enhance/task-status/{task_id}", timeout=30))
        t = data.get("task") or {}
        state = t.get("state") or ""
        msg = f"{state} {t.get('processed', 0)}/{t.get('total', 0)} 成功{t.get('success', 0)} 失败{t.get('failed', 0)}"
        if msg != last:
            log("详细地址", msg)
            last = msg
        if state == "done":
            return t
        if state in ("failed", "cancelled", "timeout", "test_acked"):
            raise PipelineError(f"详细地址任务结束: {state} {t.get('error') or ''}")
        time.sleep(15)


def _export_filename(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition") or resp.headers.get("content-disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
    if m:
        return Path(unquote(m.group(1).strip().strip('"'))).name
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return Path(m.group(1).strip()).name
    return fallback


def enhance_export_task_id(task_id: str = "", db_id="") -> str:
    tid = str(task_id or "").strip()
    if tid.startswith("db-") or (tid and not str(db_id or "").strip()):
        return tid
    did = str(db_id or "").strip()
    if did.startswith("db-"):
        return did
    if did:
        return f"db-{did}"
    return tid


def download_enhance_zip(http: requests.Session, base: str, task_id: str, dest: Path) -> Path:
    tid = enhance_export_task_id(task_id)
    if not tid:
        raise PipelineError("没有详细地址任务号，无法下载 ZIP")
    dest.mkdir(parents=True, exist_ok=True)
    url = f"{base}/address-enhance/task-export/{tid}"
    log("下载详细地址历史任务 ZIP", tid)
    r = http.get(url, timeout=180, headers={"Accept": "*/*"})
    ctype = (r.headers.get("Content-Type") or "").lower()
    if r.status_code >= 400 or "application/json" in ctype:
        try:
            data = r.json()
        except Exception:
            data = {}
        raise PipelineError(data.get("error") or data.get("message") or f"下载详细地址 ZIP 失败 HTTP {r.status_code}")
    name = f"详细地址_合并_{datetime.now().strftime('%Y%m%d')}.zip"
    path = dest / name
    path.write_bytes(r.content)
    log(f"详细地址 ZIP 已保存 {path}  {path.stat().st_size} 字节")
    return path


def enhance_archive_dir(cfg: dict) -> Path:
    root = Path(cfg.get("enhance_archive_root") or r"D:\桌面\地址匹配源文件\原始地址")
    root.mkdir(parents=True, exist_ok=True)
    return root


def archive_enhance_zip(cfg: dict, path: Path) -> Path:
    target = enhance_archive_dir(cfg) / path.name
    if target.resolve() != path.resolve():
        shutil.copy2(path, target)
    log("详细地址 ZIP 已覆盖归档", target)
    return target


def remove_match_dir_enhance_zips(cfg: dict) -> None:
    """匹配数据当天目录不删文件，详细地址 ZIP 只下到原始地址。"""
    return


def push_enhance_zip(cfg: dict, http: requests.Session, base: str,
                     task_id: str = "", db_id="") -> Path | None:
    tid = enhance_export_task_id(task_id, db_id)
    dest = enhance_archive_dir(cfg)
    path = download_enhance_zip(http, base, tid, dest)
    remove_match_dir_enhance_zips(cfg)
    from send_feishu import notify_zip
    day = datetime.now().strftime("%m.%d")
    to = str(cfg.get("feishu_enhance_to") or "17.").strip()
    notify_zip(
        cfg, path,
        text=f"详细地址匹配完成（{day}）：{path.name}",
        log_ok="飞书已发送详细地址ZIP",
        fail_log="详细地址飞书发送失败（本地 ZIP 已保存）:",
        to=to,
    )
    return path


def _created_today(it: dict) -> bool:
    today = datetime.now()
    created = it.get("created_at") or 0
    if isinstance(created, (int, float)) and created > 0:
        ts = created / 1000.0 if created > 1e12 else created
        return datetime.fromtimestamp(ts).date() == today.date()
    if not isinstance(created, str):
        return False
    raw = created.strip()
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10] == today.strftime("%Y-%m-%d")
    try:
        md = raw.replace("/", "-").split()[0]
        month, day = md.split("-")[:2]
        return int(month) == today.month and int(day) == today.day
    except (TypeError, ValueError, IndexError):
        return False


def _same_enhance(it: dict, enhance_db_id) -> bool:
    want = str(enhance_db_id or "").strip()
    if want.startswith("db-"):
        want = want[3:]
    got = str(it.get("enhance_id") or "").strip()
    if got.startswith("db-"):
        got = got[3:]
    if not want:
        return True
    if got and got == want:
        return True
    fn = str(it.get("filename") or "")
    return fn.startswith("一键") or fn.startswith("emi")


def _as_enhance_id(enhance_db_id) -> str:
    eid = str(enhance_db_id or "").strip()
    if eid.isdigit():
        return f"db-{eid}"
    return eid


def download_contact_master(http: requests.Session, base: str, enhance_db_id, dest: Path) -> Path:
    eid = _as_enhance_id(enhance_db_id)
    dest.mkdir(parents=True, exist_ok=True)
    r = http.get(f"{base}/address-enhance/task-contact-master/{eid}", timeout=180, headers={"Accept": "*/*"})
    ctype = (r.headers.get("Content-Type") or "").lower()
    if r.status_code >= 400 or "application/json" in ctype:
        try:
            data = r.json()
        except Exception:
            data = {}
        raise PipelineError(data.get("error") or f"下载联系方式总表失败 HTTP {r.status_code}")
    name = f"一键{datetime.now().strftime('%m%d')}.xlsx"
    path = dest / name
    path.write_bytes(r.content)
    log("联系方式总表已保存", path, path.stat().st_size, "字节")
    return path


def start_em_info(http: requests.Session, base: str, enhance_db_id, cfg: dict | None = None) -> int | None:
    """从当天详细地址任务提交全量。页面名沿用详细地址那份原表拼接名；
    轻松邮 EXE 下单名由匹配系统自己缩短，和页面名不冲突。"""
    eid = _as_enhance_id(enhance_db_id)
    log("联系方式匹配：导入详细地址全量", eid)
    try:
        started = _json(http.post(
            f"{base}/em-info/submit",
            json={"enhance_id": eid, "force": ""},
            timeout=180,
        ))
    except PipelineError as e:
        if "没有可提交的行" in str(e):
            log("联系方式没有可提交行，跳过")
            return None
        raise
    tid = started.get("task_id")
    if not tid:
        raise PipelineError("联系方式匹配未返回 task_id")
    summary = started.get("summary") or {}
    shown = started.get("filename") or ""
    log("联系方式已启动", tid, f"提交{summary.get('send') or 0}行", shown)
    return int(tid)


def overwrite_file(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / src.name
    if target.resolve() != src.resolve():
        shutil.copy2(src, target)
    log("已覆盖", target)
    return target


def download_em_info_file(http: requests.Session, base: str, task_id: int, kind: str, dest: Path) -> Path | None:
    r = http.get(f"{base}/em-info/download/{task_id}/{kind}", timeout=180, headers={"Accept": "*/*"})
    ctype = (r.headers.get("Content-Type") or "").lower()
    if r.status_code >= 400 or "application/json" in ctype:
        return None
    name = _export_filename(r, f"联系方式_{kind}_{datetime.now().strftime('%m.%d')}")
    if not Path(name).suffix:
        name = name + (".zip" if kind != "filled" else ".xlsx")
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / Path(name).name
    path.write_bytes(r.content)
    log(f"联系方式{kind}已下载", path, path.stat().st_size, "字节")
    return path


def _clear_today_stale_exports(dest: Path, archive: Path) -> None:
    """匹配数据当天目录不删文件。原始地址只覆盖当天导出。"""
    return


def push_latest_after_contact(cfg: dict, http: requests.Session, base: str,
                              contact_id, enhance_db_id) -> None:
    from run_detect import match_day_dir
    dest = match_day_dir(cfg)
    archive = enhance_archive_dir(cfg)
    _clear_today_stale_exports(dest, archive)
    download_enhance_zip(http, base, _as_enhance_id(enhance_db_id), archive)
    remove_match_dir_enhance_zips(cfg)
    log("已用最新详细地址 ZIP 覆盖原始地址，匹配数据不放这份")


def wait_em_info(http: requests.Session, base: str, task_id: int) -> dict:
    log("等待联系方式匹配完成")
    last = ""
    while True:
        data = _json(http.get(f"{base}/em-info/task/{task_id}", timeout=30))
        t = data.get("task") or data
        st = (t.get("status") or "").lower()
        msg = (f"{st} {t.get('message') or t.get('status_label') or ''} "
               f"电话{t.get('phone') or 0} 邮箱{t.get('email') or 0}")
        if msg != last:
            log("联系方式", msg)
            last = msg
        if st == "done":
            return t
        if st in ("failed", "cancelled", "error"):
            raise PipelineError(f"联系方式结束: {st} {t.get('message') or ''}")
        time.sleep(20)


def today_em_info(http: requests.Session, base: str, enhance_db_id="") -> tuple[dict | None, dict | None]:
    try:
        data = _json(http.get(f"{base}/em-info/tasks", params={"page_size": 20}, timeout=30))
    except Exception as e:
        log("读取联系方式任务失败:", e)
        return None, None
    live = done = None
    for it in data.get("items") or []:
        if not _created_today(it) or not _same_enhance(it, enhance_db_id):
            continue
        st = (it.get("status") or "").lower()
        if st in ("em_running", "running") and live is None:
            live = it
        if st == "done" and done is None:
            done = it
    return live, done


def start_email(http: requests.Session, base: str, enhance_db_id, cfg: dict | None = None) -> int | None:
    log("邮箱匹配：导入详细地址全量（有街道）")
    try:
        pack = _json(http.post(
            f"{base}/lookup/enhance-leftover-upload",
            json={"task_id": enhance_db_id},
            timeout=120,
        ))
    except PipelineError as e:
        text = str(e)
        if "没有剩余" in text or "没有带街道" in text:
            log("没有带街道的行，跳过邮箱匹配")
            return None
        raise
    cols = pack.get("columns") or []
    cmap = guess_email_map(cols)
    if not cmap["name_col"] or not cmap["state_col"] or not cmap["addr_col"] or not cmap["store_col"]:
        raise PipelineError("邮箱列映射失败: " + str(cols))
    files = pack.get("files") or []
    conc = str((cfg or {}).get("email_sd_concurrency") or "50")
    log(f"全量 {pack.get('total_rows')} 行，{len(files)} 个店表，SD并发 {conc}")
    fd = {
        "temp_file": pack.get("temp_file") or "",
        "original_filename": pack.get("original_filename") or "详细地址全量.xlsx",
        "match_mode": "email",
        "files_json": json.dumps(files, ensure_ascii=False),
        "data_source": "wp_tps_uspb_fps",
        "proxy_channel": "scrapedo",
        "bd_account": "auto",
        "sd_account": "auto",
        "sd_concurrency": conc,
        **cmap,
    }
    started = _json(http.post(f"{base}/lookup/batch-execute", data=fd, timeout=120))
    tid = started.get("task_id")
    if not tid:
        raise PipelineError("邮箱匹配未返回 task_id")
    log("邮箱匹配已启动", tid)
    return int(tid)


def wait_lookup(http: requests.Session, base: str, task_id: int) -> dict:
    log("等待邮箱匹配完成")
    last = ""
    while True:
        data = _json(http.get(f"{base}/lookup/batch-status/{task_id}", timeout=30))
        st = data.get("status") or ""
        msg = (f"{st} {data.get('processed_rows', 0)}/{data.get('total_rows', 0)} "
               f"成功{data.get('success_rows', 0)} 失败{data.get('failed_rows', 0)}")
        if msg != last:
            log("邮箱匹配", msg)
            last = msg
        if st == "done":
            return data
        if st in ("failed", "cancelled", "error"):
            raise PipelineError(f"邮箱匹配结束: {st}")
        time.sleep(20)


def _tf_stamp_path() -> Path:
    return HERE / f"tracerfy-{datetime.now().strftime('%Y%m%d')}.ok"


def _write_tf_stamp(task_id: str) -> None:
    try:
        _tf_stamp_path().write_text(str(task_id), encoding="utf-8")
    except OSError:
        pass


def start_tracerfy(http: requests.Session, base: str, email_task_id: int) -> str | None:
    existing = today_tracerfy_task(http, base)
    if existing:
        tid, st = existing
        log("今日 Tracerfy 已有任务，不再重交", tid, st)
        return tid
    log("Tracerfy：上传邮箱剩余付费")
    try:
        leftover = _json(http.post(
            f"{base}/tracerfy/leftover-upload",
            json={"task_id": email_task_id},
            timeout=120,
        ))
    except PipelineError as e:
        if "没有剩余" in str(e):
            log("没有付费剩余，跳过 Tracerfy")
            return None
        raise
    files = leftover.get("files") or []
    if not files:
        log("没有付费剩余，跳过 Tracerfy")
        return None
    specs = []
    for f in files:
        cmap = guess_tf_map(f.get("columns") or [])
        miss = [k for k in ("name", "address", "city", "state", "store") if not cmap.get(k)]
        if miss:
            raise PipelineError(f"{f.get('filename')}: Tracerfy 列映射缺 " + "、".join(miss))
        specs.append({
            "temp_file": f.get("temp_file"),
            "filename": f.get("filename") or f.get("orig_name"),
            "col_map": cmap,
        })
        log(f"  {specs[-1]['filename']} {f.get('rows')}行")
    started = None
    while specs:
        try:
            started = _json(http.post(
                f"{base}/tracerfy/submit",
                json={"trace_type": "normal", "files": specs},
                timeout=120,
            ))
            break
        except PipelineError as e:
            msg = str(e)
            if "无有效数据行" not in msg:
                raise
            bad = [s for s in specs if s["filename"] and s["filename"] in msg]
            if not bad:
                raise
            for s in bad:
                log("Tracerfy 跳过无有效行", s["filename"])
            skip = {s["filename"] for s in bad}
            specs = [s for s in specs if s["filename"] not in skip]
    if not started:
        log("Tracerfy 剩余表都无有效行，跳过")
        return None
    tid = started.get("task_id")
    log(f"Tracerfy 已提交 {tid}  行数{started.get('rows')}  预计{started.get('estimated_wait')}秒")
    if tid:
        _write_tf_stamp(str(tid))
    return str(tid)


def wait_tracerfy(http: requests.Session, base: str, task_id: str) -> dict:
    log("等待 Tracerfy 完成")
    last = ""
    while True:
        try:
            data = _json(http.get(f"{base}/tracerfy/task/{task_id}", timeout=30))
            t = data.get("task") or {}
        except PipelineError:
            listed = _json(http.get(f"{base}/tracerfy/tasks", timeout=30))
            t = {}
            for item in listed.get("tasks") or listed.get("items") or []:
                if str(item.get("id") or item.get("task_id") or "") == task_id:
                    t = item
                    break
            if not t:
                time.sleep(15)
                continue
        st = t.get("status") or t.get("state") or ""
        msg = f"{st} hit={t.get('hit_rows', 0)} submitted={t.get('rows_submitted', 0)}"
        if msg != last:
            log("Tracerfy", msg)
            last = msg
        if st in ("done", "completed"):
            return t
        if st in ("failed", "error", "timeout", "cancelled"):
            raise PipelineError(f"Tracerfy 结束: {st}")
        time.sleep(15)


def _is_tri_success_zip(name: str) -> bool:
    """匹配系统成功包：20261007成功27+21份.zip。不含服务商查邮箱成功包。"""
    return bool(re.match(r"^\d{8}成功\d+\+\d+份", name)) and "三源合并" not in name and "服务商" not in name


def keep_one_tri_zip(dest: Path) -> Path | None:
    """只认规则名成功包，不删目录里其它文件。"""
    if not dest.is_dir():
        return None
    success = [p for p in dest.glob("*.zip") if _is_tri_success_zip(p.name)]
    if not success:
        return None
    return max(success, key=lambda p: p.stat().st_mtime)


def cfg_bool(cfg: dict | None, key: str, default: bool = True) -> bool:
    if not cfg or key not in cfg:
        return default
    v = cfg.get(key)
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


def skip_tri_if_done(dest: Path, cfg: dict | None = None) -> Path | None:
    """当天已有成功包则跳过再合并、再下载。"""
    if cfg is not None and not cfg_bool(cfg, "skip_tri_if_done", True):
        return None
    found = keep_one_tri_zip(dest)
    if found:
        log("当天成功包已在，跳过三源合并和下载", found.name)
    return found


def tri_keep_name(item: dict, n_sheets: int = 0) -> str:
    mid = item.get("id") or ""
    stores = item.get("stores") or n_sheets or 0
    raw = (item.get("day") or "")[:10]
    day = raw.replace("-", "") if raw else datetime.now().strftime("%Y%m%d")
    if len(day) != 8:
        day = datetime.now().strftime("%Y%m%d")
    return f"{day}成功{mid}+{stores}份.zip"


def promote_tri_zip(path: Path, item: dict) -> Path:
    """按规则名落下成功包。只动这一份下载，不删目录里其它文件。"""
    n = 0
    if path.is_file():
        try:
            import zipfile
            with zipfile.ZipFile(path) as zf:
                n = sum(1 for name in zf.namelist() if name.lower().endswith((".xlsx", ".xls")))
        except Exception:
            n = 0
    want = path.parent / tri_keep_name(item, n)
    if path.resolve() != want.resolve():
        shutil.copy2(path, want)
        log("已按规则保存成功包", want.name)
        path = want
    return path


def tri_merge_today(http: requests.Session, base: str, dest: Path | None = None) -> dict:
    day = datetime.now().strftime("%Y-%m-%d")
    log("匹配总览：三源合并当日", day)
    data = _json(http.post(
        f"{base}/overview/tri-merge",
        json={"today": True, "date": day},
        timeout=180,
    ))
    item = data.get("item") or data
    log("三源合并完成", item.get("zip_name") or item.get("id") or item)
    if dest is not None:
        keep_one_tri_zip(dest)
    return item


def run(cfg: dict, all_today: bool) -> None:
    base = (cfg.get("matching_url") or "http://127.0.0.1:5000").rstrip("/")
    http = _session()
    wait_app(http, base, cfg.get("matching_exe") or "")
    files = dest_xlsx(cfg)
    if not files:
        raise PipelineError("当天目录没有店表")
    if all_today:
        log("模式: 当天全部店表")
        chosen = files
    else:
        chosen, old = already_in_enhance(http, base, files)
        if old:
            log("已在今日详细地址任务中，跳过", len(old), "个")
        if not chosen:
            raise PipelineError("没有尚未导入的新店表（今日批次已跑过）。需要重跑请加 --all")
    log("本轮文件:")
    for p in chosen:
        log(" ", p.name)
    enhance_tid = start_enhance(http, base, chosen, str(cfg.get("matching_mode") or "2"), cfg)
    enhance = wait_enhance(http, base, enhance_tid)
    db_id = enhance.get("db_id")
    if not db_id:
        listed = _json(http.get(f"{base}/address-enhance/tasks-list", params={"page_size": 5}, timeout=30))
        for it in listed.get("items") or []:
            if it.get("task_id") == enhance_tid or str(it.get("task_id", "")).endswith(str(enhance_tid)):
                db_id = str(it.get("task_id") or "").replace("db-", "")
                break
    if not db_id:
        raise PipelineError("详细地址完成但没有数据库任务号，无法继续联系方式/邮箱匹配")
    log("详细地址数据库任务", db_id, "成功", enhance.get("success"), "失败", enhance.get("failed"))
    try:
        push_enhance_zip(cfg, http, base, enhance_tid, db_id)
    except Exception as e:
        log("详细地址飞书推送跳过:", e)
    run_after_enhance(cfg, http, base, db_id)


def latest_enhance_db_id(http: requests.Session, base: str) -> str:
    listed = _json(http.get(f"{base}/address-enhance/tasks-list", params={"page_size": 20}, timeout=30))
    today = datetime.now().strftime("%Y-%m-%d")
    for it in listed.get("items") or []:
        created = it.get("created_at") or 0
        if isinstance(created, (int, float)) and created > 1e12:
            created = created / 1000.0
        day = ""
        if isinstance(created, (int, float)) and created > 0:
            day = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
        if day != today:
            continue
        st = (it.get("state") or "").lower()
        if st not in ("done", "completed"):
            continue
        tid = str(it.get("task_id") or "")
        if tid.startswith("db-"):
            return tid[3:]
        if tid.isdigit():
            return tid
        db_id = it.get("db_id")
        if db_id:
            return str(db_id)
        try:
            st = _json(http.get(f"{base}/address-enhance/task-status/{tid}", timeout=20))
            t = st.get("task") or {}
            if t.get("db_id"):
                return str(t["db_id"])
        except Exception:
            continue
    raise PipelineError("找不到今日已完成的详细地址任务，无法从邮箱匹配续跑")


def stop_lookup(http: requests.Session, base: str, task_id: int) -> None:
    try:
        r = http.post(f"{base}/lookup/batch-stop/{task_id}", timeout=20)
        log("已停止邮箱匹配", task_id, r.text[:120])
    except Exception as e:
        log("停止邮箱匹配时出错（可忽略）:", e)


def running_email_task_id(http: requests.Session, base: str) -> int | None:
    try:
        data = _json(http.get(f"{base}/lookup/tasks-list", params={"mode": "email", "page_size": 10}, timeout=30))
    except Exception:
        return None
    for it in data.get("items") or data.get("tasks") or []:
        st = (it.get("status") or it.get("state") or "").lower()
        if st not in ("running", "running_tps", "running_tt", "running_fps_addr", "running_addr_sup"):
            continue
        mode = (it.get("match_mode") or "").lower()
        if mode and mode not in ("email", ""):
            continue
        tid = it.get("id") or it.get("task_id")
        if tid:
            return int(tid)
    return None


_TF_DONE = ("done", "completed")


def today_tracerfy_task(http: requests.Session, base: str) -> tuple[str, str] | None:
    """当天已提交过就返回 (id, status)，失败任务不算。"""
    stamp = _tf_stamp_path()
    if stamp.is_file():
        tid = stamp.read_text(encoding="utf-8").strip()
        if tid:
            return tid, "stamped"
    try:
        listed = _json(http.get(f"{base}/tracerfy/tasks", timeout=30))
    except Exception:
        listed = {}
    for item in listed.get("tasks") or listed.get("items") or []:
        if not _created_today(item):
            continue
        st = (item.get("status") or item.get("state") or "").lower()
        if st in ("failed", "error", "timeout", "cancelled"):
            continue
        tid = item.get("id") or item.get("task_id")
        if tid:
            _write_tf_stamp(str(tid))
            return str(tid), st or "unknown"
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-400:]
    except OSError:
        lines = []
    day = datetime.now().strftime("%Y-%m-%d")
    for line in reversed(lines):
        if not line.startswith(day) or "Tracerfy 已提交" not in line:
            continue
        m = re.search(r"tf-\d+", line)
        if m:
            _write_tf_stamp(m.group(0))
            return m.group(0), "logged"
    return None


def today_done_tracerfy_id(http: requests.Session, base: str) -> str | None:
    found = today_tracerfy_task(http, base)
    return found[0] if found else None


def today_done_email_id(http: requests.Session, base: str) -> int | None:
    try:
        data = _json(http.get(f"{base}/lookup/tasks-list", params={"mode": "email", "page_size": 10}, timeout=30))
    except Exception:
        return None
    for it in data.get("items") or data.get("tasks") or []:
        if not _created_today(it):
            continue
        st = (it.get("status") or it.get("state") or "").lower()
        if st != "done":
            continue
        mode = (it.get("match_mode") or "").lower()
        if mode and mode not in ("email", ""):
            continue
        tid = it.get("id") or it.get("task_id")
        if tid:
            return int(tid)
    return None


def run_from_email(cfg: dict, http: requests.Session, base: str, db_id, restart_email: bool = True) -> None:
    log("从邮箱匹配续跑，详细地址任务", db_id, "并发", cfg.get("email_sd_concurrency") or 50)
    _run_email_and_after(cfg, http, base, db_id, restart_email=restart_email)
    from run_detect import match_day_dir, run_detect
    dest = match_day_dir(cfg)
    existing = skip_tri_if_done(dest, cfg)
    if existing:
        run_detect(cfg, zip_path=existing)
    else:
        item = tri_merge_today(http, base, dest)
        run_detect(cfg, tri_item=item)
    log("整条链路完成")


def _run_email_and_after(cfg: dict, http: requests.Session, base: str, db_id,
                         restart_email: bool = False) -> int | None:
    running = running_email_task_id(http, base)
    email_id = None
    if running and not restart_email:
        log("邮箱匹配仍在跑，接着等", running)
        wait_lookup(http, base, running)
        email_id = running
    elif not restart_email:
        email_id = today_done_email_id(http, base)
        if email_id:
            log("今日邮箱匹配已完成，跳过重跑", email_id)
        else:
            email_id = start_email(http, base, db_id, cfg)
            if email_id:
                wait_lookup(http, base, email_id)
    else:
        if running:
            stop_lookup(http, base, running)
            time.sleep(2)
        email_id = start_email(http, base, db_id, cfg)
        if email_id:
            wait_lookup(http, base, email_id)
    if email_id:
        existing = today_tracerfy_task(http, base)
        if existing:
            tf_id, st = existing
            if st in _TF_DONE:
                log("今日 Tracerfy 已完成，跳过重跑", tf_id)
            else:
                log("今日 Tracerfy 已提交，接着等", tf_id, st)
                wait_tracerfy(http, base, tf_id)
        else:
            tf_id = start_tracerfy(http, base, email_id)
            if tf_id:
                wait_tracerfy(http, base, tf_id)
    return email_id


def run_after_enhance(cfg: dict, http: requests.Session, base: str, db_id) -> None:
    log("详细地址之后：联系方式全量 → 邮箱全量，任务", db_id,
        "并发", cfg.get("email_sd_concurrency") or 50)
    live, done = today_em_info(http, base, db_id)
    contact_id = None
    if live:
        contact_id = int(live.get("id") or live.get("task_id"))
        log("联系方式仍在跑，接着等", contact_id)
        wait_em_info(http, base, contact_id)
    elif done:
        contact_id = int(done.get("id") or done.get("task_id"))
        log("联系方式已完成", contact_id)
    else:
        contact_id = start_em_info(http, base, db_id, cfg)
        if contact_id:
            wait_em_info(http, base, contact_id)
    if contact_id:
        try:
            push_latest_after_contact(cfg, http, base, contact_id, db_id)
        except Exception as e:
            log("联系方式完成后覆盖导出跳过:", e)
    _run_email_and_after(cfg, http, base, db_id, restart_email=False)
    from run_detect import match_day_dir, run_detect
    dest = match_day_dir(cfg)
    existing = skip_tri_if_done(dest, cfg)
    if existing:
        run_detect(cfg, zip_path=existing)
    else:
        item = tri_merge_today(http, base, dest)
        run_detect(cfg, tri_item=item)
    log("整条链路完成")


def today_enhance_tasks(http: requests.Session, base: str) -> tuple[dict | None, dict | None]:
    listed = _json(http.get(f"{base}/address-enhance/tasks-list", params={"page_size": 20}, timeout=30))
    today = datetime.now().strftime("%Y-%m-%d")
    live = done = None
    for it in listed.get("items") or []:
        created = it.get("created_at") or 0
        if isinstance(created, (int, float)) and created > 1e12:
            created = created / 1000.0
        day = ""
        if isinstance(created, (int, float)) and created > 0:
            day = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
        if day != today:
            continue
        st = (it.get("state") or "").lower()
        if st in ("queued", "running", "awaiting_names", "polling", "exporting"):
            live = live or it
        if st in ("done", "completed"):
            done = done or it
    return live, done


def enhance_db_id_of(http: requests.Session, base: str, item: dict) -> str:
    tid = str(item.get("task_id") or "")
    if tid.startswith("db-"):
        return tid[3:]
    if item.get("db_id"):
        return str(item["db_id"])
    if tid.isdigit():
        return tid
    try:
        st = _json(http.get(f"{base}/address-enhance/task-status/{tid}", timeout=20))
        t = st.get("task") or {}
        if t.get("db_id"):
            return str(t["db_id"])
    except Exception:
        pass
    raise PipelineError("详细地址任务没有数据库号")


def today_chain_done() -> bool:
    if not LOG_FILE.is_file():
        return False
    day = datetime.now().strftime("%Y-%m-%d")
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-300:]
    except OSError:
        return False
    return any(line.startswith(day) and "整条链路完成" in line for line in lines)


def _match_cutoff_hm(cfg: dict) -> tuple[int, int]:
    try:
        from collect_wechat import cutoff_hm
        return cutoff_hm(cfg)
    except Exception:
        return 9, 30


def past_new_enhance_window(cfg: dict) -> bool:
    """09:30 开当天详细地址；其后一小时内仍算这轮续跑，再之后不再开新单。"""
    h, m = _match_cutoff_hm(cfg)
    now = datetime.now()
    cut = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return now > cut + timedelta(minutes=60)


def run_daily_once(cfg: dict) -> None:
    """按当天进度续跑：崩了再拉起也不会从头把已完成的详细地址再导一遍。"""
    base = (cfg.get("matching_url") or "http://127.0.0.1:5000").rstrip("/")
    http = _session()
    wait_app(http, base, cfg.get("matching_exe") or "")
    files = dest_xlsx(cfg)
    chosen, old = already_in_enhance(http, base, files) if files else ([], [])
    live, done = today_enhance_tasks(http, base)
    if not live and not chosen and today_chain_done():
        log("当天链路已完成，不再重跑")
        return

    if live:
        tid = str(live.get("task_id") or "")
        log("详细地址仍在跑，接着等", tid)
        enhance = wait_enhance(http, base, tid)
        db_id = enhance.get("db_id") or enhance_db_id_of(http, base, live)
        try:
            push_enhance_zip(cfg, http, base, tid, db_id)
        except Exception as e:
            log("详细地址飞书推送跳过:", e)
        run_after_enhance(cfg, http, base, db_id)
        return

    if done:
        if chosen:
            log("今日详细地址已跑过，下列表不导入（过点进次日）:",
                "、".join(p.name for p in chosen))
        db_id = enhance_db_id_of(http, base, done)
        log("详细地址已完成，从联系方式续跑", db_id)
        run_after_enhance(cfg, http, base, db_id)
        return

    if chosen and past_new_enhance_window(cfg):
        h, m = _match_cutoff_hm(cfg)
        log("已过", f"{h:02d}:{m:02d}",
            "匹配窗口，新表不导入详细地址:", "、".join(p.name for p in chosen))
        from run_detect import run_detect
        run_detect(cfg)
        return

    if chosen:
        run(cfg, all_today=False)
        return

    if not files:
        log("当天没有店表，跳过匹配；核验只认当天三源或当天查出")
        from run_detect import run_detect
        run_detect(cfg)
        return

    run(cfg, all_today=False)


def run_daily(cfg: dict) -> None:
    from collect_wechat import pid_alive, read_pid, write_pid, clear_pid
    old = read_pid(PIPELINE_PID)
    if old and old != os.getpid() and pid_alive(old):
        log("当日链路已在运行 PID", old, "本轮退出以免重入")
        return
    write_pid(PIPELINE_PID)
    crashes = 0
    try:
        while True:
            try:
                run_daily_once(cfg)
                return
            except Exception as e:
                crashes += 1
                log(f"链路中断（第 {crashes} 次），60 秒后按进度续跑:", e)
                try:
                    from send_feishu import notify_alert
                    notify_alert(
                        cfg, "匹配链路中断", str(e),
                        extra=f"第 {crashes} 次，60 秒后按进度续跑。",
                    )
                except Exception:
                    pass
                if crashes >= 30:
                    raise PipelineError(f"连续中断 {crashes} 次，停止续跑") from e
                time.sleep(60)
    finally:
        clear_pid(PIPELINE_PID, os.getpid())


def main():
    set_console_title()
    ap = argparse.ArgumentParser(description="跑通当日匹配链路")
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--all", action="store_true", help="当天目录全部店表（含已跑过的）")
    ap.add_argument("--from-email", action="store_true", help="跳过详细地址和联系方式，从今日邮箱匹配重跑（命中走缓存）")
    ap.add_argument("--push-enhance", action="store_true", help="只下载今日详细地址 ZIP 并推飞书")
    ap.add_argument("--daily", action="store_true",
                    help="无人值守日跑：按进度续跑，中断后自动接着跑")
    args = ap.parse_args()
    cfg_path = Path(args.config)
    if load_cfg:
        cfg = load_cfg(cfg_path)
    else:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    os.chdir(HERE)
    try:
        if args.daily:
            run_daily(cfg)
        elif args.push_enhance:
            base = (cfg.get("matching_url") or "http://127.0.0.1:5000").rstrip("/")
            http = _session()
            wait_app(http, base, cfg.get("matching_exe") or "")
            db_id = latest_enhance_db_id(http, base)
            push_enhance_zip(cfg, http, base, db_id=db_id)
        elif args.from_email:
            base = (cfg.get("matching_url") or "http://127.0.0.1:5000").rstrip("/")
            http = _session()
            wait_app(http, base, cfg.get("matching_exe") or "")
            db_id = latest_enhance_db_id(http, base)
            run_from_email(cfg, http, base, db_id)
        else:
            run(cfg, all_today=args.all)
    except PipelineError as e:
        log("失败:", e)
        try:
            from send_feishu import notify_alert
            notify_alert(cfg, "匹配链路报错", str(e))
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        log("失败:", e)
        try:
            from send_feishu import notify_alert
            notify_alert(cfg, "匹配链路报错", str(e))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
