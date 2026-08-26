"""Import netdisk-only books into the 544544.xyz book category.

Writes the three tables a book needs to exist and be findable:

  le_cms_article         the row itself
  le_cms_article_data    the (placeholder) description body
  le_cms_article_search  the tokenised search index

Ids are allocated explicitly from MAX(id)+1 so the whole run occupies one
contiguous range; the range is printed and saved, which is the only practical
undo on MyISAM tables that have no transactions.

The default is a dry run.  Nothing is written until --execute is passed.

    python3 tools/import_books.py --input missing_books.tsv            # preview
    python3 tools/import_books.py --input missing_books.tsv --limit 50 --execute
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.site_search_index import search_message  # noqa: E402

COLUMNS = ("key", "rank", "title", "ss", "isbn", "suffix", "filename", "path", "source")
CJK_RE = re.compile(r"[一-鿿]")
LETTER_RE = re.compile(r"[A-Za-z]")
CONTENT_BODY = "<p></p><p></p><p>该书暂无内容介绍。</p><p></p><p></p>"
# What the site tells a reader about where a book comes from.  Deliberately
# does not name the upstream service.
SOURCE_LABEL = "非标准文件检索"
LEGACY_SOURCE_LABEL = "百度网盘群文件库"
TITLE_MAX = 80          # le_cms_article.title is varchar(80)
INTRO_MAX = 500         # le_cms_article.intro is varchar(500)
LOAD_CEILING = 6.0


def load_average() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


def usable_title(title: str) -> bool:
    """Reject rows whose 'title' is really a sequence number or leftover noise."""
    text = (title or "").strip()
    if not text:
        return False
    compact = re.sub(r"[\s_\-—–.()（）#]+", "", text)
    if len(compact) <= 2 or compact.isdigit():
        return False
    return bool(CJK_RE.search(text) or LETTER_RE.search(text))


def clean_title(title: str) -> str:
    text = re.sub(r"\s+", " ", (title or "").strip())
    # Uploader index markers such as "#1000125 " or "#@1000027".
    text = re.sub(r"^#@?\d+\s*", "", text)
    return text.strip(" _-—–.")


def site_title(row: dict) -> str:
    """Build 《书名》_作者_SS_ISBN, the layout the site and its parser expect."""
    name = clean_title(row["title"])
    ss = row["ss"]
    isbn = row["isbn"]
    if ss or isbn:
        title = f"《{name}》_" + f"_{ss}_{isbn}"
    else:
        title = f"《{name}》"
    if len(title) > TITLE_MAX:
        # Trim the book name, never the identifiers: they are the lookup keys.
        overflow = len(title) - TITLE_MAX
        name = name[: max(1, len(name) - overflow)]
        title = (f"《{name}》_" + f"_{ss}_{isbn}") if (ss or isbn) else f"《{name}》"
    return title


def site_intro(row: dict) -> str:
    """Same shape as the site's own rows: escaped text joined by literal <br />."""
    parts = [f"【书名】：《{html.escape(clean_title(row['title']), quote=False)}》"]
    if row["isbn"]:
        parts.append(f"【ISBN】：{html.escape(row['isbn'], quote=False)}")
    if row["ss"]:
        parts.append(f"【SS码】：{html.escape(row['ss'], quote=False)}")
    parts.append(f"【格式】：{html.escape(row['suffix'].lstrip('.').upper(), quote=False)}")
    parts.append(f"【来源】：{SOURCE_LABEL}")
    return "<br />".join(parts)[:INTRO_MAX]


def escape(value: str) -> str:
    """MySQL string literal body (single quotes, backslashes, control chars)."""
    out = []
    for char in value or "":
        if char in ("'", "\\"):
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\x00":
            continue
        else:
            out.append(char)
    return "".join(out)


def read_rows(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(COLUMNS):
                parts += [""] * (len(COLUMNS) - len(parts))
            yield dict(zip(COLUMNS, parts))


def mysql(args, statement: str, capture: bool = False) -> str:
    """Run one statement, feeding it on stdin.

    A batched multi-row INSERT is far longer than ARG_MAX, so it can never be
    passed with -e.
    """
    command = ["mysql", f"-u{args.user}", f"-p{args.password}", args.database, "-N", "-B"]
    result = subprocess.run(
        command, input=statement.encode("utf-8"), capture_output=True, timeout=args.timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[:400])
    return result.stdout.decode("utf-8", "replace").strip() if capture else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="把网盘独有的图书导入站点")
    parser.add_argument("--input", required=True)
    parser.add_argument("--database", default="544544_xyz")
    parser.add_argument("--user", default="544544_xyz")
    parser.add_argument("--password", default="251024")
    parser.add_argument("--cid", type=int, default=1)
    parser.add_argument("--uid", type=int, default=1)
    parser.add_argument("--author", default="admin")
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--pause", type=float, default=0.25)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--limit", type=int, default=0, help="只导入前 N 条，用于试运行")
    parser.add_argument("--skip", type=int, default=0,
                        help="跳过前 N 条可导入记录（续跑试运行之后的部分）")
    parser.add_argument("--execute", action="store_true", help="真正写入；缺省只预览")
    parser.add_argument("--state", default="/root/import_state.json")
    args = parser.parse_args()

    path = Path(args.input)
    kept: list[dict] = []
    skipped = 0
    passed_over = 0
    for row in read_rows(path):
        if not usable_title(row["title"]):
            skipped += 1
            continue
        if passed_over < args.skip:
            passed_over += 1
            continue
        kept.append(row)
        if args.limit and len(kept) >= args.limit:
            break

    print(f"输入 {path}")
    print(f"  可导入 {len(kept):,} 条，跳过标题不可用 {skipped:,} 条"
          + (f"，跳过已导入 {passed_over:,} 条" if passed_over else ""))
    if not kept:
        return 0

    if not args.execute:
        print("\n（预览模式，未写入数据库。加 --execute 才会真正导入）\n")
        print("将要写入的样例：")
        for row in kept[:5]:
            title = site_title(row)
            print(f"  title = {title}")
            print(f"  intro = {site_intro(row)[:90]}")
            print(f"  search= {search_message(title)[:90]}")
            print()
        by_suffix: dict[str, int] = {}
        for row in kept:
            by_suffix[row["suffix"]] = by_suffix.get(row["suffix"], 0) + 1
        print("按格式：" + "  ".join(f"{k}={v:,}" for k, v in sorted(by_suffix.items(), key=lambda i: -i[1])[:10]))
        return 0

    start_id = int(mysql(args, f"SELECT MAX(id) FROM le_cms_article", capture=True) or 0) + 1
    now = int(time.time())
    print(f"\n开始导入：id 从 {start_id:,} 起，共 {len(kept):,} 条")
    state = {"start_id": start_id, "count": len(kept), "started": now, "input": str(path)}
    Path(args.state).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    inserted = 0
    for offset in range(0, len(kept), args.batch):
        chunk = kept[offset: offset + args.batch]
        if load_average() > LOAD_CEILING:
            print(f"  负载偏高，暂停 15 秒", flush=True)
            time.sleep(15)

        article_values = []
        data_values = []
        search_values = []
        for index, row in enumerate(chunk):
            row_id = start_id + offset + index
            title = site_title(row)
            article_values.append(
                f"({row_id},{args.cid},'{escape(title)}','','','{escape(site_intro(row))}','',"
                f"{args.uid},'{escape(args.author)}','',{now},{now},0,0,0,0,0,'','','','','')"
            )
            data_values.append(f"({row_id},'{escape(CONTENT_BODY)}')")
            search_values.append(f"({row_id},'{escape(search_message(title))}')")

        mysql(args, "INSERT INTO le_cms_article "
                    "(id,cid,title,alias,tags,intro,pic,uid,author,source,dateline,lasttime,ip,"
                    "imagenum,filenum,iscomment,comments,flags,seo_title,seo_keywords,seo_description,jumpurl) "
                    "VALUES " + ",".join(article_values))
        mysql(args, "INSERT INTO le_cms_article_data (id,content) VALUES " + ",".join(data_values))
        mysql(args, "INSERT INTO le_cms_article_search (id,message) VALUES " + ",".join(search_values))
        inserted += len(chunk)
        if (offset // args.batch) % 10 == 0 or inserted == len(kept):
            print(f"  已导入 {inserted:,}/{len(kept):,}  负载={load_average():.2f}", flush=True)
        time.sleep(args.pause)

    end_id = start_id + len(kept) - 1
    mysql(args, f"UPDATE le_category SET count=count+{inserted} WHERE cid={args.cid}")
    state.update({"end_id": end_id, "inserted": inserted, "finished": int(time.time())})
    Path(args.state).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    print(f"\n完成：写入 {inserted:,} 条，id 范围 {start_id:,}–{end_id:,}")
    print(f"如需回滚：DELETE FROM le_cms_article WHERE id BETWEEN {start_id} AND {end_id}; "
          f"（le_cms_article_data / le_cms_article_search 同范围，并把 le_category.count 减回去）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
