from __future__ import annotations

import json

import pytest

from app.services import harvest
from tests.conftest import row, write_slice


def paste_rows(db):
    conn = db.connect()
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM layer_paste ORDER BY id").fetchall()
        ]
    finally:
        conn.close()


def watermark(db):
    conn = db.connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harvest_watermarks ORDER BY file_name"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_first_harvest_only_reads_directory_slices(env):
    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", [row("2026-11-10", "S01")])
    write_slice(env["fixtures"], "门店日销售", "2026-11-11.jsonl", [row("2026-11-11", "S02")])

    job = harvest.run_harvest(1)

    assert job["status"] == "成功"
    assert job["watermark_before"] == []
    assert job["watermark_after"] == ["2026-11-10.jsonl", "2026-11-11.jsonl"]
    assert job["success_files"] == ["2026-11-10.jsonl", "2026-11-11.jsonl"]
    assert job["attempted_files"] == job["success_files"]
    assert job["failed_files"] == []
    assert job["new_count"] == 2
    assert len(paste_rows(env["db"])) == 2
    assert [w["row_count"] for w in watermark(env["db"])] == [1, 1]


def test_second_harvest_appends_only_new_slices(env):
    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", [row("2026-11-10", "S01")])
    harvest.run_harvest(1)
    before = {r["id"]: r["raw_json"] for r in paste_rows(env["db"])}

    write_slice(env["fixtures"], "门店日销售", "2026-11-11.jsonl", [row("2026-11-11", "S02")])
    job = harvest.run_harvest(1)

    assert job["status"] == "成功"
    assert job["attempted_files"] == ["2026-11-11.jsonl"]
    assert job["success_files"] == ["2026-11-11.jsonl"]
    assert job["watermark_before"] == ["2026-11-10.jsonl"]
    assert job["watermark_after"] == ["2026-11-10.jsonl", "2026-11-11.jsonl"]
    assert job["new_count"] == 1

    after = {r["id"]: r["raw_json"] for r in paste_rows(env["db"])}
    assert all(after[k] == v for k, v in before.items())
    assert len(after) == 2


def test_no_new_slices_is_success_with_zero_rows(env):
    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", [row("2026-11-10", "S01")])
    harvest.run_harvest(1)
    job = harvest.run_harvest(1)
    assert job["status"] == "成功"
    assert job["attempted_files"] == []
    assert job["new_count"] == 0
    assert job["watermark_before"] == job["watermark_after"]


def test_bad_slice_rolls_back_whole_file_and_keeps_watermark(env):
    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", [row("2026-11-10", "S01")])
    harvest.run_harvest(1)
    paste_before = paste_rows(env["db"])
    mark_before = watermark(env["db"])

    # 第二行是坏 JSON，第一行必须同文件回滚
    write_slice(
        env["fixtures"],
        "门店日销售",
        "2026-11-11.jsonl",
        raw=json.dumps(row("2026-11-11", "S02"), ensure_ascii=False)
        + "\n{不是合法 JSON}\n",
    )
    job = harvest.run_harvest(1)

    assert job["status"] == "失败"
    assert job["attempted_files"] == ["2026-11-11.jsonl"]
    assert job["success_files"] == []
    assert job["failed_files"][0]["file"] == "2026-11-11.jsonl"
    assert "JSON" in job["failed_files"][0]["reason"]
    assert job["new_count"] == 0
    assert job["watermark_after"] == ["2026-11-10.jsonl"]
    assert paste_rows(env["db"]) == paste_before
    assert [w["file_name"] for w in watermark(env["db"])] == [
        w["file_name"] for w in mark_before
    ]


def test_failure_does_not_skip_to_later_slices(env):
    write_slice(
        env["fixtures"],
        "门店日销售",
        "2026-11-10.jsonl",
        raw='{"业务日":"2026-11-10","门店编码":"S01","金额":"不是数字"}\n',
    )
    write_slice(env["fixtures"], "门店日销售", "2026-11-11.jsonl", [row("2026-11-11", "S02")])

    job = harvest.run_harvest(1)

    assert job["status"] == "失败"
    assert job["attempted_files"] == ["2026-11-10.jsonl"]
    assert job["watermark_after"] == []
    assert job["new_count"] == 0
    assert paste_rows(env["db"]) == []
    assert "2026-11-10.jsonl" in job["message"]


def test_job_bookkeeping_derives_from_same_store(env):
    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", [row("2026-11-10", "S01")])
    write_slice(
        env["fixtures"],
        "门店日销售",
        "2026-11-11.jsonl",
        [row("2026-11-11", "S02"), row("2026-11-11", "S03")],
    )
    job = harvest.run_harvest(1)

    conn = env["db"].connect()
    try:
        paste_count = conn.execute("SELECT COUNT(*) AS c FROM layer_paste").fetchone()["c"]
        stored = conn.execute(
            "SELECT new_count, success_files_json FROM harvest_jobs WHERE id=?", (job["id"],)
        ).fetchone()
    finally:
        conn.close()

    assert int(paste_count) == job["new_count"]
    assert int(stored["new_count"]) == int(paste_count)
    assert json.loads(stored["success_files_json"]) == job["success_files"]
    assert sum(w["row_count"] for w in watermark(env["db"])) == int(paste_count)


def test_watermark_state_lists_pending_files(env):
    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", [row("2026-11-10", "S01")])
    write_slice(env["fixtures"], "门店日销售", "2026-11-11.jsonl", [row("2026-11-11", "S02")])
    write_slice(env["fixtures"], "门店日销售", "备注.txt", raw="忽略我")

    state = harvest.watermark_state(1)
    assert state["watermark_files"] == []
    assert [p["file_name"] for p in state["pending_files"]] == [
        "2026-11-10.jsonl",
        "2026-11-11.jsonl",
    ]
    assert state["pending_files"][0]["biz_day"] == "2026-11-10"

    harvest.run_harvest(1)
    state = harvest.watermark_state(1)
    assert state["watermark_files"] == ["2026-11-10.jsonl", "2026-11-11.jsonl"]
    assert state["pending_files"] == []


def test_recover_interrupted_job_clears_unbookmarked_rows(env):
    conn = env["db"].connect()
    try:
        conn.execute(
            """
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty,
              raw_json, source_file, harvest_job_id
            ) VALUES (1, '2026-11-10', 'S01', '城东店', '饮料', 1, 1, '{}', '2026-11-10.jsonl', 99)
            """
        )
        conn.execute(
            """
            INSERT INTO harvest_jobs(domain_id, status, message, attempted_files_json)
            VALUES (1, '运行中', '', '["2026-11-10.jsonl"]')
            """
        )
        job_id = int(conn.execute("SELECT MAX(id) AS i FROM harvest_jobs").fetchone()["i"])
        conn.execute(
            "UPDATE layer_paste SET harvest_job_id=? WHERE harvest_job_id=99", (job_id,)
        )
        conn.commit()
    finally:
        conn.close()

    harvest.recover_interrupted_jobs(env["db"].connect())

    conn = env["db"].connect()
    try:
        job = harvest._get_job(conn, job_id)
        remaining = conn.execute("SELECT COUNT(*) AS c FROM layer_paste").fetchone()["c"]
    finally:
        conn.close()
    assert remaining == 0
    assert job["status"] == "失败"
    assert job["failed_files"][0]["file"] == "2026-11-10.jsonl"
    assert job["watermark_after"] == []


def test_legacy_single_file_data_is_cleared_once(env):
    """旧版种子直接写贴源，升级启动时清掉无法归属的来源行，避免采集后重复。"""
    from app import seed

    conn = env["db"].connect()
    try:
        conn.execute(
            """
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty, raw_json
            ) VALUES (1, '2026-11-10', 'S01', '城东店', '饮料', 1, 1, '{}')
            """
        )
        conn.commit()
    finally:
        conn.close()

    # 模拟升级重启：迁移在尚无采集史时清除旧种子行
    seed.init_db()
    assert paste_rows(env["db"]) == []

    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", [row("2026-11-10", "S01")])
    job = harvest.run_harvest(1)
    assert job["new_count"] == 1
    rows = paste_rows(env["db"])
    assert len(rows) == 1
    assert rows[0]["source_file"] == "2026-11-10.jsonl"

    # 已有采集史后，重启不会再次删除贴源行
    seed.init_db()
    assert len(paste_rows(env["db"])) == 1


def test_harvest_rejects_running_job(env):
    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", [row("2026-11-10", "S01")])
    conn = env["db"].connect()
    try:
        conn.execute(
            "INSERT INTO harvest_jobs(domain_id, status, message) VALUES (1, '运行中', '')"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(harvest.HarvestBusy):
        harvest.run_harvest(1)


def test_empty_slice_is_bookmarked_without_blocking(env):
    write_slice(env["fixtures"], "门店日销售", "2026-11-10.jsonl", raw="")
    write_slice(env["fixtures"], "门店日销售", "2026-11-11.jsonl", [row("2026-11-11", "S01")])

    job = harvest.run_harvest(1)

    assert job["status"] == "成功"
    assert job["watermark_after"] == ["2026-11-10.jsonl", "2026-11-11.jsonl"]
    assert job["new_count"] == 1
    assert [w["row_count"] for w in watermark(env["db"])] == [0, 1]


def test_truncated_tail_is_rejected_as_incomplete(env):
    """目录被边写边读时，最后一行没有换行说明文件还没写完。"""
    write_slice(
        env["fixtures"],
        "门店日销售",
        "2026-11-10.jsonl",
        raw=json.dumps(row("2026-11-10", "S01"), ensure_ascii=False),
    )
    job = harvest.run_harvest(1)

    assert job["status"] == "失败"
    assert "末尾不完整" in job["failed_files"][0]["reason"]
    assert job["watermark_after"] == []
    assert paste_rows(env["db"]) == []


def test_missing_directory_fails_without_dirtying_watermark(env):
    job = harvest.run_harvest(1)
    assert job["status"] == "失败"
    assert "夹具目录不存在" in job["message"]
    assert job["watermark_after"] == []
