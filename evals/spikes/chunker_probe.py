"""切块器对照探针：同一批页文本，只换切分算法/参数，比接缝率。

输入取库里的 document_pages（C# 已抽好的页文本），PDF 解析这一变量被隔离，
测出来的差异只来自切块本身。基线：C# 439 块 / 接缝 7.29%。

用法：python evals/spikes/chunker_probe.py [模式名]
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from chonkie import RecursiveChunker, SentenceChunker
from llama_index.core import Document
from llama_index.core.node_parser import SentenceSplitter

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "evals" / "fixtures" / "agentworkbench.db"
NOW = "2026-11-02T00:00:00"
ZH = ["。", "！", "？", "；", "\n"]

# llama 模式用的中文分句：保留分隔符，句末标点后切开。
_ZH_SENT_RE = re.compile(r"(?<=[。！？；\n])")


def _zh_sentences(text: str) -> list[str]:
    return [s for s in _ZH_SENT_RE.split(text) if s]


def _chars(text: str) -> list[str]:
    """字符计数（与 chonkie 的 tokenizer="character" 同口径：chunk_size 按字符算）。"""
    return list(text)


def _llama(splitter: SentenceSplitter) -> Callable[[str], list[str]]:
    return lambda text: [
        node.get_content() for node in splitter.get_nodes_from_documents([Document(text=text)])
    ]


def _splitter(chunker: SentenceChunker | RecursiveChunker) -> Callable[[str], list[str]]:
    return lambda text: [ch.text for ch in chunker.chunk(text)]


def _mk(mode: str) -> Callable[[str], list[str]]:
    if mode == "sentence_default":
        return _splitter(
            SentenceChunker(
                tokenizer="character",
                chunk_size=800,
                chunk_overlap=120,
                min_sentences_per_chunk=1,
            )
        )
    if mode == "sentence_zh":
        return _splitter(
            SentenceChunker(
                tokenizer="character",
                chunk_size=800,
                chunk_overlap=120,
                min_sentences_per_chunk=1,
                delim=ZH,
            )
        )
    if mode == "sentence_zh_ov0":
        return _splitter(
            SentenceChunker(
                tokenizer="character",
                chunk_size=800,
                chunk_overlap=0,
                min_sentences_per_chunk=1,
                delim=ZH,
            )
        )
    if mode == "sentence_zh_ov0_s1000":
        return _splitter(
            SentenceChunker(
                tokenizer="character",
                chunk_size=1000,
                chunk_overlap=0,
                min_sentences_per_chunk=1,
                delim=ZH,
            )
        )
    if mode == "recursive":
        return _splitter(
            RecursiveChunker(tokenizer="character", chunk_size=800, min_characters_per_chunk=24)
        )
    if mode == "current":
        from agentworkbench.ingest.parsing.chunker import chunk_pages

        return lambda text: [d.content for d in chunk_pages([text])]
    if mode == "llama_default":
        return _llama(SentenceSplitter(chunk_size=800, chunk_overlap=120, tokenizer=_chars))
    if mode == "llama_zh":
        return _llama(
            SentenceSplitter(
                chunk_size=800,
                chunk_overlap=120,
                tokenizer=_chars,
                chunking_tokenizer_fn=_zh_sentences,
            )
        )
    if mode == "llama_zh_ov0":
        return _llama(
            SentenceSplitter(
                chunk_size=800,
                chunk_overlap=0,
                tokenizer=_chars,
                chunking_tokenizer_fn=_zh_sentences,
            )
        )
    if mode == "llama_zh_ov0_s1000":
        return _llama(
            SentenceSplitter(
                chunk_size=1000,
                chunk_overlap=0,
                tokenizer=_chars,
                chunking_tokenizer_fn=_zh_sentences,
            )
        )
    raise SystemExit(f"未知模式：{mode}")


def rebuild(dst: Path, mode: str) -> int:
    shutil.copy(SRC, dst)
    conn = sqlite3.connect(dst)
    pages = conn.execute(
        "select document_id, page_no, raw_text from document_pages order by document_id, page_no"
    ).fetchall()
    by_doc: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for doc_id, page_no, text in pages:
        by_doc[doc_id].append((page_no, text))
    split = _mk(mode)

    conn.execute("delete from chunks")
    rows: list[tuple[object, ...]] = []
    for doc_id, plist in by_doc.items():
        seq = 0
        for page_no, text in plist:
            if not text.strip():
                continue
            for piece in split(text):
                content = piece.strip()
                if not content:
                    continue
                rows.append((doc_id, seq, page_no, page_no, None, content, len(content), NOW))
                seq += 1
    conn.executemany(
        "insert into chunks(document_id,seq,page_from,page_to,heading,content,"
        "char_count,created_at) values(?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def main() -> int:
    modes = (
        [sys.argv[1]]
        if len(sys.argv) > 1
        else ["llama_default", "llama_zh", "llama_zh_ov0", "llama_zh_ov0_s1000"]
    )
    script = ROOT / "evals" / "self" / "sample_chunks.py"
    for mode in modes:
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "probe.db"
            n = rebuild(dst, mode)
            out = subprocess.run(
                [sys.executable, str(script), "--db", str(dst), "--sample", "0"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            ).stdout
            summary = [
                line
                for line in out.splitlines()
                if line.startswith("块总数")
                or line.startswith("疑似切断")
                or line.startswith("按主类别")
            ]
            print(f"=== {mode} ===")
            print(f"块数 {n}")
            for line in summary:
                print(" ", line)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
