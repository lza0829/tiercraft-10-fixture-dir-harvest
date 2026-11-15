from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from app import db
from app.db import connect, rows_to_dicts
from app.services import pipeline

SLICE_SUFFIXES = {".jsonl", ".json"}
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

JOB_JSON_FIELDS = (
    "watermark_before",
    "watermark_after",
    "attempted_files",
    "success_files",
    "failed_files",
)


class HarvestError(Exception):
    """采集过程中可预期的业务错误。"""


class HarvestFileError(HarvestError):
    """单个切片无法完整入位。"""


class HarvestBusy(HarvestError):
    """同一主题域已有采集任务在运行。"""


# ---------------------------------------------------------------------------
# 夹具目录
# ---------------------------------------------------------------------------


def fixture_dir(domain: dict) -> Path:
    raw = str(domain.get("fixture_dir") or "").strip() or str(domain.get("name") or "").strip()
    root = db.FIXTURES_DIR.resolve()
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise HarvestError("夹具目录超出允许范围")
    return target


def _biz_day_from_name(file_name: str) -> str:
    match = _DATE_RE.search(file_name)
    return match.group(1) if match else ""


def scan_slice_files(directory: Path) -> list[dict]:
    """列出目录下按业务日命名的切片文件，按业务日+文件名排序。"""
    if not directory.is_dir():
        raise HarvestError(f"夹具目录不存在：{directory}")
    items: list[dict] = []
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise HarvestError(f"读取夹具目录失败：{exc}") from exc
    for path in entries:
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix.lower() not in SLICE_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise HarvestError(f"读取切片 {path.name} 失败：{exc}") from exc
        items.append(
            {
                "file_name": path.name,
                "path": path,
                "biz_day": _biz_day_from_name(path.name),
                "size_bytes": int(size),
            }
        )
    items.sort(key=lambda it: (it["biz_day"] or "￿", it["file_name"]))
    return items


# ---------------------------------------------------------------------------
# 位点与任务读取
# ---------------------------------------------------------------------------


def _watermark_rows(conn: sqlite3.Connection, domain_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT file_name, biz_day, row_count, size_bytes, sha256, harvest_job_id, ingested_at
        FROM harvest_watermarks
        WHERE domain_id=?
        ORDER BY biz_day, file_name
        """,
        (domain_id,),
    ).fetchall()
    return rows_to_dicts(rows)


def _watermark_names(conn: sqlite3.Connection, domain_id: int) -> list[str]:
    return [str(r["file_name"]) for r in _watermark_rows(conn, domain_id)]


def _job_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for field in JOB_JSON_FIELDS:
        raw = data.pop(f"{field}_json", "[]") or "[]"
        try:
            data[field] = json.loads(raw)
        except (TypeError, ValueError):
            data[field] = []
    return data


def _get_job(conn: sqlite3.Connection, job_id: int) -> dict:
    row = conn.execute("SELECT * FROM harvest_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HarvestError("采集任务不存在")
    return _job_dict(row)


def list_harvest_jobs(domain_id: int | None = None, limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute(
                "SELECT * FROM harvest_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM harvest_jobs WHERE domain_id=? ORDER BY id DESC LIMIT ?",
                (domain_id, limit),
            ).fetchall()
        return [_job_dict(r) for r in rows]
    finally:
        conn.close()


def get_harvest_job(job_id: int) -> dict:
    conn = connect()
    try:
        return _get_job(conn, job_id)
    finally:
        conn.close()


def watermark_state(domain_id: int) -> dict:
    domain = pipeline.get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    directory = fixture_dir(domain)
    conn = connect()
    try:
        watermark = _watermark_rows(conn, domain_id)
    finally:
        conn.close()
    bookmarked = {str(w["file_name"]) for w in watermark}
    pending: list[dict] = []
    available: list[dict] = []
    directory_error = ""
    try:
        for item in scan_slice_files(directory):
            available.append(
                {
                    "file_name": item["file_name"],
                    "biz_day": item["biz_day"],
                    "size_bytes": item["size_bytes"],
                    "in_watermark": item["file_name"] in bookmarked,
                }
            )
            if item["file_name"] not in bookmarked:
                pending.append(
                    {
                        "file_name": item["file_name"],
                        "biz_day": item["biz_day"],
                        "size_bytes": item["size_bytes"],
                    }
                )
    except HarvestError as exc:
        directory_error = str(exc)
    return {
        "domain_id": domain_id,
        "domain_name": domain.get("name", ""),
        "fixture_dir": str(directory),
        "fixture_dir_error": directory_error,
        "watermark": watermark,
        "watermark_files": [str(w["file_name"]) for w in watermark],
        "available_files": available,
        "pending_files": pending,
    }


# ---------------------------------------------------------------------------
# 采集执行
# ---------------------------------------------------------------------------


def _create_job(conn: sqlite3.Connection, domain_id: int, before: list[str]) -> int:
    cur = conn.execute(
        """
        INSERT INTO harvest_jobs(
          domain_id, status, message, watermark_before_json, watermark_after_json,
          attempted_files_json, success_files_json, failed_files_json, new_count
        ) VALUES (?, '运行中', '', ?, ?, '[]', '[]', '[]', 0)
        """,
        (domain_id, json.dumps(before, ensure_ascii=False), json.dumps(before, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _mark_attempted(conn: sqlite3.Connection, job_id: int, file_name: str) -> None:
    row = conn.execute(
        "SELECT attempted_files_json FROM harvest_jobs WHERE id=?", (job_id,)
    ).fetchone()
    attempted = json.loads(row["attempted_files_json"] or "[]") if row else []
    if file_name not in attempted:
        attempted.append(file_name)
        conn.execute(
            "UPDATE harvest_jobs SET attempted_files_json=? WHERE id=?",
            (json.dumps(attempted, ensure_ascii=False), job_id),
        )
    conn.commit()


def _record_failure(conn: sqlite3.Connection, job_id: int, failure: dict) -> None:
    row = conn.execute(
        "SELECT failed_files_json FROM harvest_jobs WHERE id=?", (job_id,)
    ).fetchone()
    failed = json.loads(row["failed_files_json"] or "[]") if row else []
    if failure not in failed:
        failed.append(failure)
    name = failure.get("file") or ""
    reason = failure.get("reason") or ""
    message = f"切片 {name} 采集失败：{reason}" if name else f"采集失败：{reason}"
    conn.execute(
        """
        UPDATE harvest_jobs
        SET status='失败', message=?, failed_files_json=?
        WHERE id=?
        """,
        (message, json.dumps(failed, ensure_ascii=False), job_id),
    )
    conn.commit()


def _ingest_slice(
    conn: sqlite3.Connection, domain_id: int, job_id: int, item: dict
) -> None:
    """整文件事务：全部行写入贴源并在同事务内登记位点，否则整体回滚。

    调用方负责在异常时回滚连接。
    """
    path: Path = item["path"]
    file_name: str = item["file_name"]
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise HarvestFileError(f"读取失败：{exc}") from exc
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    size_bytes = len(raw_bytes)

    # 目录被边写边读时，末尾没有换行的最后一行通常是半行；拒收而不是把半行当完整数据入位。
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        raise HarvestFileError("文件末尾不完整（最后一行未以换行结束），疑似仍在写入")

    conn.execute("BEGIN IMMEDIATE")
    existed = conn.execute(
        "SELECT 1 FROM harvest_watermarks WHERE domain_id=? AND file_name=?",
        (domain_id, file_name),
    ).fetchone()
    if existed:
        raise HarvestFileError("该切片已收录在位点中")

    rows_written = 0
    first_biz_day = ""
    for lineno, line in enumerate(raw_bytes.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarvestFileError(f"第 {lineno} 行 JSON 解析失败：{exc.msg}") from exc
        if not isinstance(obj, dict):
            raise HarvestFileError(f"第 {lineno} 行不是 JSON 对象")
        try:
            amount = float(obj.get("金额") or 0)
            qty = int(obj.get("件数") or 0)
        except (TypeError, ValueError):
            raise HarvestFileError(f"第 {lineno} 行金额或件数不是数字") from exc
        biz_day = str(obj.get("业务日") or item["biz_day"] or "")
        if not first_biz_day:
            first_biz_day = biz_day
        conn.execute(
            """
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty,
              raw_json, source_file, harvest_job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                biz_day,
                str(obj.get("门店编码") or ""),
                str(obj.get("门店名称") or ""),
                str(obj.get("品类") or ""),
                amount,
                qty,
                json.dumps(obj, ensure_ascii=False),
                file_name,
                job_id,
            ),
        )
        rows_written += 1

    if rows_written == 0:
        # 空切片视为完整摄入：位点可收录，不阻塞后续切片。
        row_count = 0
    else:
        row_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS c FROM layer_paste
                WHERE domain_id=? AND harvest_job_id=? AND source_file=?
                """,
                (domain_id, job_id, file_name),
            ).fetchone()["c"]
        )
    biz_day = item["biz_day"] or first_biz_day
    conn.execute(
        """
        INSERT INTO harvest_watermarks(
          domain_id, file_name, biz_day, row_count, size_bytes, sha256, harvest_job_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (domain_id, file_name, biz_day, row_count, size_bytes, sha256, job_id),
    )

    # 成功清单与位点集合同事务落库，二者永远同源。
    success = [
        str(r["file_name"])
        for r in conn.execute(
            """
            SELECT file_name FROM harvest_watermarks
            WHERE domain_id=? AND harvest_job_id=?
            ORDER BY biz_day, file_name
            """,
            (domain_id, job_id),
        ).fetchall()
    ]
    after = _watermark_names(conn, domain_id)
    conn.execute(
        """
        UPDATE harvest_jobs
        SET success_files_json=?, watermark_after_json=?
        WHERE id=?
        """,
        (
            json.dumps(success, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
            job_id,
        ),
    )
    conn.commit()


def _finalize_job(
    conn: sqlite3.Connection, domain_id: int, job_id: int, failure: dict | None
) -> dict:
    success = [
        str(r["file_name"])
        for r in conn.execute(
            """
            SELECT file_name FROM harvest_watermarks
            WHERE domain_id=? AND harvest_job_id=?
            ORDER BY biz_day, file_name
            """,
            (domain_id, job_id),
        ).fetchall()
    ]
    after = _watermark_names(conn, domain_id)
    new_count = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=? AND harvest_job_id=?",
            (domain_id, job_id),
        ).fetchone()["c"]
    )
    if failure:
        status = "失败"
        if failure.get("file"):
            message = (
                f"切片 {failure['file']} 采集失败：{failure['reason']}；"
                f"本次已成功入位 {len(success)} 个切片、写入 {new_count} 行，"
                "失败切片及后续切片未采集，失败切片未收录进位点"
            )
        else:
            message = f"采集失败：{failure['reason']}；位点未推进"
        failed = [failure]
    else:
        status = "成功"
        failed = []
        if success:
            message = f"新增切片 {len(success)} 个，写入 {new_count} 行"
        else:
            message = "位点之外没有新切片，贴源未变化"

    conn.execute(
        """
        UPDATE harvest_jobs
        SET status=?, message=?, watermark_after_json=?, success_files_json=?,
            failed_files_json=?, new_count=?, finished_at=datetime('now')
        WHERE id=?
        """,
        (
            status,
            message,
            json.dumps(after, ensure_ascii=False),
            json.dumps(success, ensure_ascii=False),
            json.dumps(failed, ensure_ascii=False),
            new_count,
            job_id,
        ),
    )
    conn.commit()
    return _get_job(conn, job_id)


def run_harvest(domain_id: int) -> dict:
    domain = pipeline.get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")

    directory = fixture_dir(domain)
    conn = connect()
    try:
        # 候选清单必须在同一次即时事务里扫描：并发触发时后到者要么被判定为运行中，
        # 要么看到已推进的位点，不会拿着过期清单去重复摄入。
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute(
            "SELECT id FROM harvest_jobs WHERE domain_id=? AND status='运行中' ORDER BY id DESC",
            (domain_id,),
        ).fetchone()
        if running:
            conn.rollback()
            raise HarvestBusy(f"主题域正在采集（任务 {running['id']}），请稍后再试")

        before = _watermark_names(conn, domain_id)
        try:
            candidates = [
                item
                for item in scan_slice_files(directory)
                if item["file_name"] not in set(before)
            ]
            directory_error = ""
        except HarvestError as exc:
            candidates = []
            directory_error = str(exc)

        job_id = _create_job(conn, domain_id, before)
        conn.commit()

        failure: dict | None = None
        if directory_error:
            failure = {"file": "", "reason": directory_error}

        for item in candidates:
            if failure:
                break
            _mark_attempted(conn, job_id, item["file_name"])
            try:
                _ingest_slice(conn, domain_id, job_id, item)
            except HarvestFileError as exc:
                conn.rollback()
                failure = {"file": item["file_name"], "reason": str(exc)}
                _record_failure(conn, job_id, failure)
            except sqlite3.Error as exc:
                conn.rollback()
                failure = {"file": item["file_name"], "reason": f"写入贴源失败：{exc}"}
                _record_failure(conn, job_id, failure)
            except Exception as exc:  # noqa: BLE001 - 落库失败也必须留下任务记录
                conn.rollback()
                failure = {"file": item["file_name"], "reason": f"采集异常：{exc}"}
                _record_failure(conn, job_id, failure)

        if failure and not failure.get("file"):
            _record_failure(conn, job_id, failure)

        return _finalize_job(conn, domain_id, job_id, failure)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 启动恢复：进程中断时位点只保留已完整提交的切片
# ---------------------------------------------------------------------------


def recover_interrupted_jobs(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM harvest_jobs WHERE status='运行中'").fetchall()
    for row in rows:
        job = _job_dict(row)
        job_id = int(job["id"])
        # 防御性清除：任何没有同事务位点登记的行都不允许留在贴源。
        conn.execute(
            """
            DELETE FROM layer_paste
            WHERE harvest_job_id=?
              AND source_file NOT IN (
                SELECT file_name FROM harvest_watermarks WHERE harvest_job_id=?
              )
            """,
            (job_id, job_id),
        )
        success = [
            str(r["file_name"])
            for r in conn.execute(
                "SELECT file_name FROM harvest_watermarks WHERE harvest_job_id=?",
                (job_id,),
            ).fetchall()
        ]
        pending = [f for f in job.get("attempted_files", []) if f not in success]
        failed = [
            {"file": name, "reason": "采集进程中断，切片未完整入位"}
            for name in pending
        ]
        after = _watermark_names(conn, int(job["domain_id"]))
        new_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=? AND harvest_job_id=?",
                (int(job["domain_id"]), job_id),
            ).fetchone()["c"]
        )
        message = (
            f"采集进程中断；位点仅保留已完整入位的 {len(success)} 个切片，"
            f"贴源保留 {new_count} 行"
        )
        conn.execute(
            """
            UPDATE harvest_jobs
            SET status='失败', message=?, watermark_after_json=?, success_files_json=?,
                failed_files_json=?, new_count=?, finished_at=datetime('now')
            WHERE id=?
            """,
            (
                message,
                json.dumps(after, ensure_ascii=False),
                json.dumps(success, ensure_ascii=False),
                json.dumps(failed, ensure_ascii=False),
                new_count,
                job_id,
            ),
        )
    conn.commit()
