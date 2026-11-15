from __future__ import annotations

from pathlib import Path

from app import db


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  biz_key TEXT NOT NULL,
  fixture_file TEXT NOT NULL DEFAULT '',
  fixture_dir TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS harvest_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  watermark_before_json TEXT NOT NULL DEFAULT '[]',
  watermark_after_json TEXT NOT NULL DEFAULT '[]',
  attempted_files_json TEXT NOT NULL DEFAULT '[]',
  success_files_json TEXT NOT NULL DEFAULT '[]',
  failed_files_json TEXT NOT NULL DEFAULT '[]',
  new_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS harvest_watermarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  file_name TEXT NOT NULL,
  biz_day TEXT NOT NULL DEFAULT '',
  row_count INTEGER NOT NULL DEFAULT 0,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  harvest_job_id INTEGER NOT NULL,
  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_id, file_name),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE INDEX IF NOT EXISTS idx_harvest_watermark_domain
  ON harvest_watermarks(domain_id);

CREATE TABLE IF NOT EXISTS layer_paste (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  raw_json TEXT NOT NULL,
  source_file TEXT NOT NULL DEFAULT '',
  harvest_job_id INTEGER,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE INDEX IF NOT EXISTS idx_layer_paste_job
  ON layer_paste(domain_id, harvest_job_id);

CREATE TABLE IF NOT EXISTS layer_detail (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  clean_flag TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_summary (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  amount_sum REAL NOT NULL,
  qty_sum INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  UNIQUE(domain_id, biz_day, store_code),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  in_count INTEGER NOT NULL DEFAULT 0,
  out_count INTEGER NOT NULL DEFAULT 0,
  drop_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  paste_count INTEGER NOT NULL,
  detail_count INTEGER NOT NULL,
  summary_line_sum INTEGER NOT NULL,
  key_mismatch INTEGER NOT NULL,
  amount_drift REAL NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);
"""


def init_db() -> None:
    conn = db.connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT COUNT(*) AS c FROM domains").fetchone()
        if int(row["c"]) == 0:
            _seed_domain(conn)
        conn.commit()
        _recover_interrupted_jobs(conn)
    finally:
        conn.close()


def _table_columns(conn, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn) -> None:
    """兼容早期只有单文件夹具、贴源行没有来源标记的库。"""
    domain_columns = _table_columns(conn, "domains")
    if "fixture_dir" not in domain_columns:
        conn.execute("ALTER TABLE domains ADD COLUMN fixture_dir TEXT NOT NULL DEFAULT ''")
    if "fixture_file" not in domain_columns:
        conn.execute("ALTER TABLE domains ADD COLUMN fixture_file TEXT NOT NULL DEFAULT ''")

    paste_columns = _table_columns(conn, "layer_paste")
    if "source_file" not in paste_columns:
        conn.execute(
            "ALTER TABLE layer_paste ADD COLUMN source_file TEXT NOT NULL DEFAULT ''"
        )
    if "harvest_job_id" not in paste_columns:
        conn.execute("ALTER TABLE layer_paste ADD COLUMN harvest_job_id INTEGER")

    for row in conn.execute(
        "SELECT id, name, fixture_file, fixture_dir FROM domains"
    ).fetchall():
        if str(row["fixture_dir"] or "").strip():
            continue
        legacy = str(row["fixture_file"] or "").strip()
        candidate = Path(legacy).stem if legacy else str(row["name"])
        if candidate.endswith("-贴源"):
            candidate = candidate[:-3]
        conn.execute(
            "UPDATE domains SET fixture_dir=? WHERE id=?",
            (candidate or str(row["name"]), int(row["id"])),
        )

    # 旧版启动种子直接写贴源，行上没有来源文件，无法归属到任何切片。
    # 仅在没有采集史时清掉这批可重建的种子行，避免首次采集后出现重复数据。
    has_jobs = int(
        conn.execute("SELECT COUNT(*) AS c FROM harvest_jobs").fetchone()["c"]
    )
    has_watermarks = int(
        conn.execute("SELECT COUNT(*) AS c FROM harvest_watermarks").fetchone()["c"]
    )
    if has_jobs == 0 and has_watermarks == 0:
        conn.execute("DELETE FROM layer_paste WHERE COALESCE(source_file, '') = ''")


def _recover_interrupted_jobs(conn) -> None:
    from app.services import harvest

    harvest.recover_interrupted_jobs(conn)


def _seed_domain(conn) -> None:
    conn.execute(
        """
        INSERT INTO domains(name, biz_key, fixture_file, fixture_dir, description)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "门店日销售",
            "业务日+门店编码",
            "",
            "门店日销售",
            "演示按业务日切片的贴源增量采集、明细清洗与按门店日汇总",
        ),
    )
