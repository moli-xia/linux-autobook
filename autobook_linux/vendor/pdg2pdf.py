#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDG to PDF Converter (超星 PDG 格式转 PDF 工具)

功能特性:
  1. 支持 ZIP 压缩包、文件夹或单个 PDG 文件直接输入
  2. 完整支持超星 00H / 02H 加密与私有 CCITT 2D 游程格式解码，100% 清晰还原无花屏黑条
  3. 标准化无损封装：解码后转为国际标准 ITU-T CCITT Group 4 TIFF，经 img2pdf 极速无损封装进 PDF
  4. 智能页面排序（封面 !00001/cov -> 书名 bok -> 版权 leg -> 前言 fow -> 目录 dir -> 正文 000001.. -> 附录 att -> 封底 !00002/bac）
  5. 自动提取并生成图书元数据（书名、作者、出版信息等）
  6. 自动解析目录大纲（BookContents.dat / 目录数据）并生成 PDF 书签/目录树
  7. 极速性能：500+ 页图书全书转换仅需 2~3 秒

使用方法:
  python pdg2pdf.py <input_path> [-o output.pdf] [--dpi 200]
"""

import argparse
import base64
import io
import os
import re
import struct
import sys
import zipfile
import zlib
from typing import List, Tuple, Dict, Any, Optional

from PIL import Image
import img2pdf
import pikepdf
from tqdm import tqdm

from autobook_linux.pdg_crypto import normalize_legacy_pdg, unsupported_pdg_type

try:
    import wasmtime
    HAS_WASMTIME = True
except ImportError:
    HAS_WASMTIME = False

# 内嵌超星 00H/02H 专用高精度解码器 WASM 模块 (16KB)
PDG_DECODER_WASM_B64 = """
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AgsgAgsEACMACxAAIwAgAGtBcHEiACQAIAALBgAgACQACy0BAX8CQCABQRRJDQAgAC0AAEHIAEcNACAALQABQcgARw0AIAAvABAhAgsgAgsCAAsL8AkCAEGACAvgCQcABAACAAgABAADAAsABAAEAAwABAAFAA4ABAAGAA8ABAAHAAcABQAKAAgABQALABIABQCAABMABQAIABQABQAJABsABQBAAAMABgANAAcABgABAAgABgAMABcABgDAABgABgCABioABgAQACsABgARADQABgAOADUABgAPAAMABwAWAAQABwAXAAgABwAUAAwABwATABMABwAaABcABwAVABgABwAcACQABwAbACcABwASACgABwAYACsABwAZADcABwAAAQIACAAdAAMACAAeAAQACAAtAAUACAAuAAoACAAvAAsACAAwABIACAAhABMACAAiABQACAAjABUACAAkABYACAAlABcACAAmABoACAAfABsACAAgACQACAA1ACUACAA2ACgACAAnACkACAAoACoACAApACsACAAqACwACAArAC0ACAAsADIACAA9ADMACAA+ADQACAA/ADUACAAAADYACABAATcACACAAUoACAA7AEsACAA8AFIACAAxAFMACAAyAFQACAAzAFUACAA0AFgACAA3AFkACAA4AFoACAA5AFsACAA6AGQACADAAWUACAAAAmcACACAAmgACABAApgACQDABZkACQAABpoACQBABpsACQDABswACQDAAs0ACQAAA9IACQBAA9MACQCAA9QACQDAA9UACQAABNYACQBABNcACQCABNgACQDABNkACQAABdoACQBABdsACQCABQgACwAABwwACwBABw0ACwCABxIADADABxMADAAACBQADABACBUADACACBYADADACBcADAAACRwADABACR0ADACACR4ADADACR8ADAAACgIAAgADAAMAAgACAAIAAwABAAMAAwAEAAIABAAGAAMABAAFAAMABQAHAAQABgAJAAUABgAIAAQABwAKAAUABwALAAcABwAMAAQACAANAAcACAAOABgACQAPAAgACgASAA8ACgBAABcACgAQABgACgARADcACgAAAAgACwAABwwACwBABw0ACwCABxcACwAYABgACwAZACgACwAXADcACwAWAGcACwATAGgACwAUAGwACwAVABIADADABxMADAAACBQADABACBUADACACBYADADACBcADAAACRwADABACR0ADACACR4ADADACR8ADAAACiQADAA0ACcADAA3ACgADAA4ACsADAA7ACwADAA8ADMADABAATQADACAATUADADAATcADAA1ADgADAA2AFIADAAyAFMADAAzAFQADAAsAFUADAAtAFYADAAuAFcADAAvAFgADAA5AFkADAA6AFoADAA9AFsADAAAAWQADAAwAGUADAAxAGYADAA+AGcADAA/AGgADAAeAGkADAAfAGoADAAgAGsADAAhAGwADAAoAG0ADAApAMgADACAAMkADADAAMoADAAaAMsADAAbAMwADAAcAM0ADAAdANIADAAiANMADAAjANQADAAkANUADAAlANYADAAmANcADAAnANoADAAqANsADAArAEoADQCAAksADQDAAkwADQAAA00ADQBAA1IADQAABVMADQBABVQADQCABVUADQDABVoADQAABlsADQBABmQADQCABmUADQDABmwADQAAAm0ADQBAAnIADQCAA3MADQDAA3QADQAABHUADQBABHYADQCABHcADQDABABB4RELAosB
"""

_WASM_ENGINE = None
_WASM_MODULE = None


def get_wasm_module():
    """获取或初始化 WASM 解码模块"""
    global _WASM_ENGINE, _WASM_MODULE
    if not HAS_WASMTIME:
        raise RuntimeError("请安装 wasmtime 库以支持超星 PDG 解码: pip install wasmtime")

    if _WASM_MODULE is None:
        _WASM_ENGINE = wasmtime.Engine()
        wasm_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pdg-decoder.wasm')
        if os.path.exists(wasm_file):
            with open(wasm_file, 'rb') as f:
                wasm_bytes = f.read()
        else:
            wasm_bytes = base64.b64decode(PDG_DECODER_WASM_B64.strip())
        _WASM_MODULE = wasmtime.Module(_WASM_ENGINE, wasm_bytes)

    return _WASM_ENGINE, _WASM_MODULE


class PdgDecoderSession:
    """PDG WASM 解码会话"""

    def __init__(self):
        engine, module = get_wasm_module()
        self.engine = engine
        self.store = wasmtime.Store(engine)
        self.memory = None

        def resize_heap(size):
            if self.memory:
                curr = self.memory.data_len(self.store)
                if size > curr:
                    pages = (size - curr + 65535) // 65536
                    self.memory.grow(self.store, pages)
            return 1

        resize_func = wasmtime.Func(
            self.store,
            wasmtime.FuncType([wasmtime.ValType.i32()], [wasmtime.ValType.i32()]),
            resize_heap
        )
        self.instance = wasmtime.Instance(self.store, module, [resize_func])
        exports = self.instance.exports(self.store)
        self.memory = exports['b']
        self.pdg_get_width = exports['d']
        self.pdg_get_height = exports['e']
        self.pdg_get_format = exports['f']
        self.pdg_alloc_bitmap = exports['g']
        self.malloc = exports['h']
        self.pdg_free = exports['i']
        self.free = exports['j']
        self.pdg_decode_file = exports['l']

    def decode_to_standard_tiff(self, raw_bytes: bytes) -> bytes:
        """将 PDG 二进制数据解码为标准 ITU-T CCITT Group 4 TIFF 或保留标准 JPEG/PNG"""
        # 如果本身已是标准图片 (JPEG / PNG / TIFF)
        if raw_bytes.startswith(b'\xff\xd8') or raw_bytes.startswith(b'\x89PNG') or raw_bytes.startswith(b'II\x2a\x00') or raw_bytes.startswith(b'MM\x00\x2a'):
            return raw_bytes

        raw_bytes = normalize_legacy_pdg(raw_bytes)

        # A proprietary "HH" type (0x04, 0x6X, 0xAX, ...) would otherwise reach
        # the WASM decoder and come back as a cryptic "错误码: -3". Name it, so an
        # operator can tell an unsupported format apart from an actual failure.
        unsupported = unsupported_pdg_type(raw_bytes)
        if unsupported is not None:
            raise ValueError(
                f"不支持的超星 PDG 加密类型 0x{unsupported:02X}："
                "该页使用专有加密，当前解码器无法解密（仅支持 00H/02H/03H/11H 及标准 JPEG/PNG/TIFF）"
            )

        data_ptr = self.malloc(self.store, len(raw_bytes))
        bitmap_ptr = None
        try:
            self.memory.write(self.store, raw_bytes, data_ptr)

            width = self.pdg_get_width(self.store, data_ptr, len(raw_bytes))
            height = self.pdg_get_height(self.store, data_ptr, len(raw_bytes))

            if width <= 0 or height <= 0:
                raise ValueError(f"异常图像尺寸: {width}x{height}")

            bitmap_ptr = self.pdg_alloc_bitmap(self.store, width, height)
            row_words = (width >> 5) + (1 if (width & 31) != 0 else 0)
            row_bytes = row_words * 4
            bitmap_size = row_bytes * height

            ret = self.pdg_decode_file(self.store, data_ptr, len(raw_bytes), bitmap_ptr, bitmap_size)
            if ret != 0:
                raise ValueError(f"PDG 解码失败 (错误码: {ret})")

            raw_bitmap = bytes(self.memory.read(self.store, bitmap_ptr, bitmap_ptr + bitmap_size))

            # 反色处理 (PDG 解码输出为前景点，反转后 0=黑/文字, 1=白/背景)
            inv_data = bytes(b ^ 0xFF for b in raw_bitmap)
            img = Image.frombytes('1', (row_bytes * 8, height), inv_data, 'raw', '1;I')
            if img.size != (width, height):
                img = img.crop((0, 0, width, height))

            # 保存为标准 ITU-T CCITT Group 4 TIFF 供 img2pdf 极速无损封装
            buf = io.BytesIO()
            img.save(buf, format='TIFF', compression='group4')
            return buf.getvalue()
        finally:
            if data_ptr is not None:
                self.free(self.store, data_ptr)
            if bitmap_ptr is not None:
                self.pdg_free(self.store, bitmap_ptr)


# ============================================================================
# PDG Page & Sorting
# ============================================================================

class PdgPage:
    """PDG 页面数据对象"""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.data = data
        self.sort_tuple = self._compute_sort_key(filename)
        self.page_type, self.page_num, _ = self.sort_tuple

    @staticmethod
    def _compute_sort_key(filename: str) -> Tuple[int, int, str]:
        """计算超星 PDG 页面的标准阅读排序键"""
        name = os.path.basename(filename).lower().replace('.pdg', '')

        # 封面 (Front Cover)
        if name.startswith('!00001') or name in ('cov001', 'cov1', 'cov', 'cover001', 'cover1'):
            return (10, 1, name)
        if name.startswith('cov') and not name.startswith('cov002'):
            m = re.search(r'\d+', name)
            return (10, int(m.group()) if m else 1, name)

        # 书名页 (Book Title)
        if name.startswith('bok') or name.startswith('title'):
            m = re.search(r'\d+', name)
            return (20, int(m.group()) if m else 1, name)

        # 版权页 (Copyright / Legal)
        if name.startswith('leg') or name.startswith('cpy'):
            m = re.search(r'\d+', name)
            return (30, int(m.group()) if m else 1, name)

        # 前言 / 序言 (Foreword / Preface)
        if name.startswith('fow') or name.startswith('pre') or name.startswith('prf'):
            m = re.search(r'\d+', name)
            return (40, int(m.group()) if m else 1, name)

        # 目录页 (Table of Contents / Directory)
        if name.startswith('dir') or name.startswith('toc') or name.startswith('cat'):
            m = re.search(r'\d+', name)
            return (50, int(m.group()) if m else 1, name)

        # 正文页 (Main Content: 纯数字或 cnt 前缀)
        if name.isdigit():
            return (60, int(name), name)
        if name.startswith('cnt') and name[3:].isdigit():
            return (60, int(name[3:]), name)

        # 附录 / 插页 (Appendix / Insert)
        if name.startswith('att') or name.startswith('app') or name.startswith('ins'):
            m = re.search(r'\d+', name)
            return (70, int(m.group()) if m else 1, name)

        # 封底 (Back Cover)
        if name.startswith('!00002') or name.startswith('bac') or name in ('cov002', 'cov2'):
            m = re.search(r'\d+', name)
            return (80, int(m.group()) if m else 1, name)

        # 其他未分类格式
        m = re.search(r'\d+', name)
        return (65, int(m.group()) if m else 0, name)


# ============================================================================
# Metadata & Bookmarks (TOC) Extraction
# ============================================================================

def parse_bookinfo(data: bytes) -> Dict[str, str]:
    """解析 bookinfo.dat / 书籍元数据"""
    info = {}
    for encoding in ('gb18030', 'gbk', 'utf-8', 'latin1'):
        try:
            text = data.decode(encoding)
            for line in text.splitlines():
                line = line.strip()
                if '=' in line:
                    k, v = line.split('=', 1)
                    info[k.strip()] = v.strip()
            if info:
                break
        except Exception:
            continue
    return info


def parse_bookcontents(data: bytes) -> List[Dict[str, Any]]:
    """解析 BookContents.dat 目录大纲数据（支持 zlib 压缩格式）"""
    decomp = None
    idx = data.find(bytes([0x78, 0x9c]))
    if idx >= 0:
        try:
            decomp = zlib.decompress(data[idx:])
        except Exception:
            pass

    if not decomp:
        decomp = data

    entries = []
    for encoding in ('gb18030', 'gbk', 'utf-8'):
        try:
            text = decomp.decode(encoding)
            for line in text.splitlines():
                line = line.strip()
                if not line or '|' not in line:
                    continue
                parts = line.split('|')
                if len(parts) >= 3:
                    title = parts[0].strip()
                    level_code = parts[1].strip()
                    try:
                        page_num = int(parts[2].strip())
                    except ValueError:
                        page_num = 1
                    page_type = int(parts[4].strip()) if len(parts) > 4 and parts[4].strip().isdigit() else 6
                    raw_len = len(level_code)
                    level = 0 if raw_len <= 4 else (raw_len - 4) // 2

                    if title:
                        entries.append({
                            'title': title,
                            'level': level,
                            'page_num': page_num,
                            'page_type': page_type
                        })
            if entries:
                break
        except Exception:
            continue

    return entries


# ============================================================================
# Core Converter
# ============================================================================

class PdgConverter:
    """PDG 批量转 PDF 转换器"""

    def __init__(self, input_path: str, output_path: Optional[str] = None, dpi: float = 200.0):
        self.input_path = os.path.abspath(input_path)
        self.dpi = dpi
        self.output_path = output_path or self._default_output_path(self.input_path)
        self.metadata: Dict[str, str] = {}
        self.toc_entries: List[Dict[str, Any]] = []

    @staticmethod
    def _default_output_path(input_path: str) -> str:
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        parent_dir = os.path.dirname(input_path)
        return os.path.join(parent_dir, f"{base_name}.pdf")

    def _collect_from_zip(self) -> List[Tuple[str, bytes]]:
        pdg_items = []
        with zipfile.ZipFile(self.input_path, 'r') as zf:
            for info in zf.infolist():
                if info.is_dir() or info.filename.startswith('__MACOSX'):
                    continue
                fname = os.path.basename(info.filename)
                fname_lower = fname.lower()

                if fname_lower.endswith('.pdg'):
                    pdg_items.append((fname, zf.read(info.filename)))
                elif fname_lower in ('bookinfo.dat', 'bookinfo.txt'):
                    self.metadata = parse_bookinfo(zf.read(info.filename))
                elif 'bookcontents' in fname_lower:
                    self.toc_entries = parse_bookcontents(zf.read(info.filename))

        return pdg_items

    def _collect_from_dir(self) -> List[Tuple[str, bytes]]:
        pdg_items = []
        for root, _, files in os.walk(self.input_path):
            for file in files:
                if file.startswith('.'):
                    continue
                fpath = os.path.join(root, file)
                fname_lower = file.lower()
                if fname_lower.endswith('.pdg'):
                    with open(fpath, 'rb') as f:
                        pdg_items.append((file, f.read()))
                elif fname_lower in ('bookinfo.dat', 'bookinfo.txt'):
                    with open(fpath, 'rb') as f:
                        self.metadata = parse_bookinfo(f.read())
                elif 'bookcontents' in fname_lower:
                    with open(fpath, 'rb') as f:
                        self.toc_entries = parse_bookcontents(f.read())

        return pdg_items

    def convert(self) -> str:
        """执行完整转换流程"""
        if not os.path.exists(self.input_path):
            raise FileNotFoundError(f"输入路径不存在: {self.input_path}")

        print(f"📖 正在扫描输入源: {self.input_path}")

        if zipfile.is_zipfile(self.input_path):
            raw_entries = self._collect_from_zip()
        elif os.path.isdir(self.input_path):
            raw_entries = self._collect_from_dir()
        elif self.input_path.lower().endswith('.pdg'):
            with open(self.input_path, 'rb') as f:
                raw_entries = [(os.path.basename(self.input_path), f.read())]
        else:
            raise ValueError(f"不支持的输入格式: {self.input_path}")

        if not raw_entries:
            raise ValueError("未在输入路径中找到任何有效的 .pdg 文件")

        print(f"📦 发现 {len(raw_entries)} 个 PDG 页面文件")
        if self.metadata:
            title = self.metadata.get('书名') or self.metadata.get('title', '')
            author = self.metadata.get('作者') or self.metadata.get('author', '')
            print(f"📋 书籍信息: 《{title}》 | 作者: {author}")

        # 构建页面对象并排序
        parsed_pages: List[PdgPage] = [PdgPage(name, data) for name, data in raw_entries]
        parsed_pages.sort(key=lambda p: p.sort_tuple)

        # 计算正文第一页在 PDF 中的物理索引 (0-indexed)
        body_start_index = 0
        for idx, p in enumerate(parsed_pages):
            if p.sort_tuple[0] >= 60:
                body_start_index = idx
                break

        # 初始化解码会话
        session = PdgDecoderSession()

        # 逐页高精度解码为标准 CCITT G4 TIFF
        image_stream_list: List[bytes] = []
        for p in tqdm(parsed_pages, desc="🖼️ 解码 PDG 并封装标准 TIFF", unit="页"):
            try:
                tiff_bytes = session.decode_to_standard_tiff(p.data)
                image_stream_list.append(tiff_bytes)
            except Exception as e:
                raise RuntimeError(f"页面 {p.filename} 解码失败，已中止生成 PDF: {e}") from e

        # 步骤 1: 使用 img2pdf 极速无损合并生成初始 PDF
        print("⚡ 正在合并生成 PDF (img2pdf 极速无损通道)...")
        layout_fun = img2pdf.get_fixed_dpi_layout_fun((self.dpi, self.dpi))
        pdf_bytes = img2pdf.convert(image_stream_list, layout_fun=layout_fun)

        # 步骤 2: 使用 pikepdf 注入元数据及目录书签 (Outline / Bookmarks)
        print("📑 正在注入元数据与目录大纲书签...")
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        total_pages = len(pdf.pages)

        # 注入元数据
        with pdf.open_metadata() as meta:
            if '书名' in self.metadata or 'title' in self.metadata:
                meta['dc:title'] = self.metadata.get('书名', self.metadata.get('title', ''))
            if '作者' in self.metadata or 'author' in self.metadata:
                meta['dc:creator'] = [self.metadata.get('作者', self.metadata.get('author', ''))]
            if '出版日期' in self.metadata or 'date' in self.metadata:
                meta['dc:date'] = self.metadata.get('出版日期', self.metadata.get('date', ''))

        # 注入书签大纲 (Outlines)
        if self.toc_entries:
            try:
                with pdf.open_outline() as outline:
                    stack = [(0, outline.root)]

                    for entry in self.toc_entries:
                        title = entry['title']
                        level = entry['level']
                        page_num = entry['page_num']
                        page_type = entry.get('page_type', 6)

                        if page_type == 6:  # 正文
                            target_page_idx = body_start_index + (page_num - 1)
                        elif page_type == 5:  # 目录
                            target_page_idx = max(0, body_start_index - 1)
                        elif page_type == 4:  # 前言
                            target_page_idx = max(0, body_start_index - 2)
                        elif page_type == 1:  # 封面
                            target_page_idx = 0
                        else:
                            target_page_idx = min(page_num - 1, total_pages - 1)

                        target_page_idx = max(0, min(target_page_idx, total_pages - 1))
                        item = pikepdf.OutlineItem(title, target_page_idx)

                        while len(stack) > 1 and stack[-1][0] >= level + 1:
                            stack.pop()

                        stack[-1][1].append(item)
                        stack.append((level + 1, item.children))

                print(f"✅ 成功注入 {len(self.toc_entries)} 条目录大纲书签")
            except Exception as e:
                print(f"⚠️ 书签生成提示: {e}")

        # 保存输出
        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)
        pdf.save(self.output_path)
        pdf.close()

        file_size_mb = os.path.getsize(self.output_path) / (1024 * 1024)
        print(f"\n🎉 转换完成: {self.output_path}")
        print(f"📊 总页数: {total_pages} 页 | 文件大小: {file_size_mb:.2f} MB")
        return self.output_path


# ============================================================================
# CLI Command Line Interface
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="PDG to PDF 转换工具 (超星 PDG 格式)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  pdg2pdf book.zip                       # ZIP 压缩包转 PDF
  pdg2pdf book.zip -o output.pdf         # 指定输出路径
  pdg2pdf ./book_folder/                 # 文件夹转 PDF
  pdg2pdf 000001.pdg                     # 单页 PDG 转换
  pdg2pdf book.zip --dpi 300             # 自定义 DPI 分辨率
"""
    )

    parser.add_argument("input", help="输入路径 (ZIP 压缩包 / 文件夹 / 单个 PDG 文件)")
    parser.add_argument("-o", "--output", help="输出 PDF 文件路径 (默认同名 .pdf)")
    parser.add_argument("--dpi", type=float, default=200.0, help="PDF 渲染分辨率 DPI (默认: 200)")

    args = parser.parse_args()

    try:
        converter = PdgConverter(args.input, args.output, dpi=args.dpi)
        converter.convert()
    except KeyboardInterrupt:
        print("\n❌ 用户取消操作")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 转换失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
