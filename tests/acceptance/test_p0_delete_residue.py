"""D6 / 删除残留：删完之后，任何入口都不该再摸到那份内容。

「删了」的判据不是 `DELETE` 没报错，而是**再数一遍等于 0**——`chunks_fts` 与
`vec_chunks_vec` 都没有外键，删不掉不抛异常，只留下检索还能命中的幽灵。
D4 已经在单元层用变异证明过补偿逻辑（`ingest-delete` 区），这一组补的是
**走真实 HTTP + 真实 FTS5/vec0** 的黑盒视角：用户点了删除之后，界面上会发生什么。

四个入口分别验：

1. 六张表里都不再有这份文档的行（含两处索引）。
2. 磁盘上的原始文件被删掉（备份之外不留副本）。
3. 检索不再命中（FTS 与向量两条路）。
4. 详情接口 404（列表里也不出现）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from harness import TEAM_A_SECRET, TwoTeams, build_two_teams

DOOMED = "doc-a"
ONLY_IN_DOOMED = "波特率"
"""只出现在团队甲那份资料里的词。用它检索，命中就等于「幽灵还在」。"""


@pytest.fixture
def teams(tmp_path: Path) -> Iterator[TwoTeams]:
    yield build_two_teams(tmp_path)


def _chunk_ids(conn: sqlite3.Connection, doc_id: str) -> list[int]:
    rows = conn.execute("select id from chunks where document_id = ?", (doc_id,)).fetchall()
    return [int(row[0]) for row in rows]


def _search(teams: TwoTeams, query: str) -> list[dict[str, object]]:
    response = teams.client_a.get("/api/knowledge/search", params={"q": query})
    assert response.status_code == 200, response.text
    body: list[dict[str, object]] = response.json()
    return body


def test_the_document_is_indexed_and_findable_before_delete(teams: TwoTeams) -> None:
    """正控：删之前必须搜得到。没有这一条，下面的「搜不到」可能本来就搜不到。"""
    hits = _search(teams, ONLY_IN_DOOMED)
    assert any(hit["documentId"] == DOOMED for hit in hits), "夹具没把资料索引进去"


def test_delete_clears_every_row_that_mentions_the_document(teams: TwoTeams) -> None:
    """六张表逐一归零，包括两处没有外键的索引。"""
    with closing(teams.open_conn()) as conn:
        chunk_ids = _chunk_ids(conn, DOOMED)
    assert chunk_ids, "夹具没有产生块，删除验不到索引残留"

    response = teams.client_a.delete(f"/api/documents/{DOOMED}")
    assert response.status_code == 204, response.text

    with closing(teams.open_conn()) as conn:
        counts = {
            "documents": conn.execute(
                "select count(*) from documents where id = ?", (DOOMED,)
            ).fetchone()[0],
            "document_pages": conn.execute(
                "select count(*) from document_pages where document_id = ?", (DOOMED,)
            ).fetchone()[0],
            "chunks": conn.execute(
                "select count(*) from chunks where document_id = ?", (DOOMED,)
            ).fetchone()[0],
            "chunks_fts": conn.execute(
                "select count(*) from chunks_fts where document_id = ?", (DOOMED,)
            ).fetchone()[0],
            "chunks_vec": conn.execute(
                "select count(*) from chunks_vec where DocumentId = ?", (DOOMED,)
            ).fetchone()[0],
        }
        placeholders = ",".join("?" for _ in chunk_ids)
        counts["vec_chunks_vec"] = conn.execute(
            f"select count(*) from vec_chunks_vec where ChunkId in ({placeholders})",  # noqa: S608
            chunk_ids,
        ).fetchone()[0]

    leftover = {name: count for name, count in counts.items() if count}
    assert not leftover, f"删除后还有残留：{leftover}（幽灵：列表看不见、检索还命中）"


def test_delete_removes_the_stored_file(teams: TwoTeams) -> None:
    """磁盘上那份原始文件也删掉——库里没了、目录里还留着，等于没删。"""
    stored = teams.deps.storage.root / "seed" / f"{DOOMED}.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(TEAM_A_SECRET.encode("utf-8"))
    assert stored.exists()

    response = teams.client_a.delete(f"/api/documents/{DOOMED}")
    assert response.status_code == 204, response.text
    assert not stored.exists(), "库里的行删了，磁盘上的原文还在"


def test_deleted_content_is_not_searchable_through_either_path(teams: TwoTeams) -> None:
    """FTS 与向量两条检索路都不再命中，且详情 404、列表不含它。"""
    assert _search(teams, ONLY_IN_DOOMED), "正控失败：删之前该搜得到"
    assert teams.client_a.delete(f"/api/documents/{DOOMED}").status_code == 204

    assert _search(teams, ONLY_IN_DOOMED) == [], "删完还能搜到 → 幽灵"

    detail = teams.client_a.get(f"/api/documents/{DOOMED}")
    assert detail.status_code == 404

    listing = teams.client_a.get("/api/documents")
    assert listing.status_code == 200
    ids = {str(item["id"]) for item in listing.json()}
    assert DOOMED not in ids
