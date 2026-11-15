from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="tiercraft-test-")
os.environ["DATA_DIR"] = str(Path(_TMP) / "data")
os.environ["FIXTURES_DIR"] = str(Path(_TMP) / "fixtures")

from app import db, seed  # noqa: E402


@pytest.fixture()
def env():
    """每个用例独占一套数据目录与夹具目录。"""
    root = Path(tempfile.mkdtemp(prefix="tiercraft-case-"))
    data = root / "data"
    fixtures = root / "fixtures"
    data.mkdir()
    fixtures.mkdir()

    original_data, original_fixtures = db.DATA_DIR, db.FIXTURES_DIR
    db.DATA_DIR = data
    db.FIXTURES_DIR = fixtures
    try:
        seed.init_db()
        yield {"root": root, "data": data, "fixtures": fixtures, "db": db}
    finally:
        db.DATA_DIR = original_data
        db.FIXTURES_DIR = original_fixtures


def write_slice(fixtures: Path, domain_dir: str, name: str, rows: list[dict] | None = None,
                raw: str | None = None) -> Path:
    directory = fixtures / domain_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    lines = [json.dumps(r, ensure_ascii=False) for r in (rows or [])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def row(day: str, store: str, amount: float = 10.0, name: str = "城东店") -> dict:
    return {
        "业务日": day,
        "门店编码": store,
        "门店名称": name,
        "品类": "饮料",
        "金额": amount,
        "件数": 1,
    }
