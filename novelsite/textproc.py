"""文本处理：编码识别、元数据提取、章节切分。

设计要点：
- 章节索引只存「字节偏移」，阅读时 seek + read 一个章节，不把正文复制进数据库。
- 解析按 1MB 块流式进行，峰值内存与文件大小无关。
- 解码使用 surrogateescape / surrogatepass，保证「解码→再编码」字节长度严格一致，
  因此即使文件里有坏字节，计算出的偏移也不会漂移。
"""

import codecs
import os
import re

PARSER_VERSION = 1

# ---------------------------------------------------------------- 编码识别

_COMMON_S = ('的一是不了在人有我他这个们中来上大为和国地到以说时要就出也得里后自会着那下看去之过'
             '家学对可她子心你生能而么面道还样年如没于起当好头小所发成只然把天都事想作开手知用经现进')
_COMMON_T = ('這個們來為國說時裡後會著過學對麼還樣沒於當頭發成現進開經')
_COMMON_RE = re.compile('[' + ''.join(sorted(set(_COMMON_S + _COMMON_T))) + ']')
_CJK_RE = re.compile('[一-鿿]')

_BOMS = (
    (codecs.BOM_UTF8, 'utf-8'),
    (codecs.BOM_UTF16_LE, 'utf-16-le'),
    (codecs.BOM_UTF16_BE, 'utf-16-be'),
)


def sniff_bom(head):
    for bom, enc in _BOMS:
        if head.startswith(bom):
            return enc, len(bom)
    return None, 0


def _decodes(sample, enc):
    try:
        codecs.getincrementaldecoder(enc)('strict').decode(sample, False)
        return True
    except UnicodeDecodeError:
        return False


def _score(sample, enc):
    text = codecs.getincrementaldecoder(enc)('replace').decode(sample, False)
    n = len(text) or 1
    bad = text.count('�')
    cjk = len(_CJK_RE.findall(text))
    common = len(_COMMON_RE.findall(text))
    # 常用字占比：真实中文约 0.3~0.45，错码解出的「乱码汉字」通常 < 0.05
    return (common / cjk if cjk else 0.0) - 5.0 * bad / n


def detect_encoding(sample):
    """返回 (encoding, bom_len)。encoding 为 '' 表示样本是纯 ASCII，无法判断。"""
    enc, bom = sniff_bom(sample)
    if enc:
        return enc, bom
    if sample.isascii():
        return '', 0
    if b'\x00' not in sample:
        if _decodes(sample, 'utf-8'):
            return 'utf-8', 0
        # 不走「谁能严格解码选谁」的捷径：UTF-8 文件只要夹一个坏字节，GB18030 往往也能
        # “成功”解出一堆乱码汉字。统一按常用字占比与坏字符率打分更稳。
        candidates = ('utf-8', 'gb18030', 'cp950')
    else:  # 文本文件里出现 NUL，大概率是无 BOM 的 UTF-16
        candidates = ('utf-16-le', 'utf-16-be', 'utf-8', 'gb18030')
    best = max(candidates, key=lambda e: _score(sample, e))
    return best, 0


def errors_for(enc):
    return 'surrogatepass' if enc.startswith('utf-16') else 'surrogateescape'


def decode_display(data, enc):
    return data.decode(enc or 'gb18030', 'replace')


def bytes_display(raw):
    """文件名可能是 GBK 字节（Windows 压缩包解到 Linux 常见），尽量还原成可读字符串。"""
    for enc in ('utf-8', 'gb18030', 'cp950'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode('utf-8', 'replace')


_SURROGATE = re.compile('[\ud800-\udfff]')


def clean_str(s):
    """去掉孤立代理字符（来自 surrogateescape），否则写 SQLite / JSON 会失败。"""
    return _SURROGATE.sub('\ufffd', s)


# ---------------------------------------------------------------- 文件名 / 文件头元数据

_TAIL_WORDS = re.compile(r'[\s_\-—·.]*(?:全本|完本|完结|全集|精校版?|校对版?|精排版?|txt|TXT|Txt)$')
_LEAD_BRACKET = re.compile(r'^\s*[\[【(（〔［]([^\]】)）〕］]{0,30})[\]】)）〕］]\s*')
_TAIL_BRACKET = re.compile(r'\s*[\[【(（〔［][^\]】)）〕］]{0,30}[\]】)）〕］]\s*$')
# 「作者」前必须有分隔符/括号，或后面跟冒号，避免把《我的作者大人》拆坏
_AUTHOR_MARK = re.compile(r'(?:[\s_\-—]+[\[【(（]?|[\[【(（])\s*作者\s*[:：]?\s*(?=\S)|作者\s*[:：]\s*(?=\S)')
_AUTHOR_CUT = re.compile(r'[\s()（）\[\]【】<>《》,，。;；|/\\]')


def clean_author(s):
    s = s.strip().lstrip(':：').strip()
    if s.isascii():  # 英文名里的空格是名字的一部分
        s = re.split(r'[()\[\]<>,;|/\\]|\s{2,}', s, maxsplit=1)[0]
    else:
        s = _AUTHOR_CUT.split(s, 1)[0]
    return s.strip()[:30]


def _strip_decor(s):
    tag = ''
    while True:
        m = _LEAD_BRACKET.match(s)
        if not m or m.end() >= len(s):
            break
        tag = tag or m.group(1).strip()
        s = s[m.end():]
    while True:
        t = _TAIL_BRACKET.sub('', s)
        t = _TAIL_WORDS.sub('', t)
        if t == s or not t.strip():
            break
        s = t
    return s.strip(), tag


def parse_filename(stem):
    """从文件名（不含扩展名）猜测 (书名, 作者, 标签, 作者是否来自明确标记)。"""
    s = stem.strip()
    author = ''
    m = _AUTHOR_MARK.search(s)
    if m and m.start() > 0:
        author = clean_author(s[m.end():])
        s = s[:m.start()]
    else:
        m = re.search(r'\s+by\s+(\S.*)$', s, re.I)
        if m:
            author = clean_author(m.group(1))
            s = s[:m.start()]
    tag, guessed = '', False
    t = re.search(r'《([^》]+)》', s)
    if t:
        title = t.group(1).strip()
        lead = _LEAD_BRACKET.match(s)
        if lead and lead.start() < t.start():
            tag = lead.group(1).strip()
    else:
        title, tag = _strip_decor(s)
        if not author and '_' in title:
            left, right = title.rsplit('_', 1)
            if left.strip() and 1 <= len(right.strip()) <= 16:
                title, author, guessed = left.strip(), clean_author(right), True
    sure = bool(author) and not guessed
    title = title.strip(' _-—·.') or stem.strip()
    return title, author, tag, sure


_HEAD_AUTHOR = re.compile(r'作\s*者\s*(?:[】\]]\s*[:：]?|[:：])\s*(\S[^\n]{0,39})')
_HEAD_INTRO = re.compile(r'(?:内容简介|作品简介|内容介绍|书籍简介|作品介绍|简\s*介|文\s*案)\s*[】\]]?\s*[:：]?\s*\n?')
_JUNK = re.compile(r'https?://|www\.|\.com|\.net|\.org|下[载載]|整理|收藏|[书書]名|作\s*者|[简簡]\s*介|[声聲]明|版[权權]|本站|手[机機]')
INTRO_LEN = 200


def extract_head_meta(text):
    """从文件开头若干 KB 文本中提取 (作者, 简介)。"""
    text = text.replace('\r', '\n')[:6000]
    author = ''
    m = _HEAD_AUTHOR.search(text[:3000])
    if m:
        author = clean_author(m.group(1))
    intro = ''
    m = _HEAD_INTRO.search(text[:3000])
    if m:
        rest = text[m.end():]
        h = HEAD_RE.search(rest)
        if h:
            rest = rest[:h.start()]
        intro = _collapse(rest)
    if not intro:
        lines = []
        for line in text.split('\n'):
            line = line.strip()
            if not line or _JUNK.search(line) or HEAD_RE.match(line):
                continue
            lines.append(line)
            if sum(map(len, lines)) > INTRO_LEN:
                break
        intro = _collapse('\n'.join(lines))
    if len(intro) > INTRO_LEN:
        intro = intro[:INTRO_LEN].rstrip() + '…'
    return author, intro


def _collapse(s):
    lines = [l.strip() for l in s.split('\n')]
    return '\n'.join(l for l in lines if l).strip()


# ---------------------------------------------------------------- 章节识别

_NUM = '[0-9０-９零〇○一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]{1,9}'
_SEP = '[ \t　:：、.．·_\\-—]'
HEAD_RE = re.compile(
    r'^[ \t　﻿]*(?:正文[ \t　]*|VIP[ \t　]*(?:章节|卷)?[ \t　]*)?'
    r'(?:'
    r'(?P<vol>第' + _NUM + r'[卷部集篇]|卷' + _NUM + r'|[上中下]卷)'
    r'|(?P<chap>第' + _NUM + r'[章节回话幕]|[Cc]hapter[ \t]*\d{1,5}'
    r'|序章|序言|序幕|楔子|引子|尾声|后记|终章|大结局|完本感言|番外)'
    r')'
    r'(?P<rest>' + _SEP + r'[^\n]{0,40}|[^\s，。！？,!?：:][^\n]{0,39})?'
    r'[ \t　]*$',
    re.M)

BLOCK = 1 << 20
SPLIT_OVER = 120 * 1024   # 单章超过此字节数则拆页
SPLIT_PART = 60 * 1024


def _nl(enc):
    if enc == 'utf-16-le':
        return (b'\n\x00', b'\r\x00'), 2
    if enc == 'utf-16-be':
        return (b'\x00\n', b'\x00\r'), 2
    return (b'\n', b'\r'), 1


def _rfind_nl(buf, enc):
    """返回 buf 中最后一个换行符之后的位置；找不到返回 -1。"""
    seps, width = _nl(enc)
    best = -1
    for sep in seps:
        i = buf.rfind(sep)
        while width == 2 and i > 0 and i % 2:
            i = buf.rfind(sep, 0, i + 1)
        if i >= 0 and i % width == 0:
            best = max(best, i + width)
    return best


def _find_nl(buf, enc):
    seps, width = _nl(enc)
    best = -1
    for sep in seps:
        i = buf.find(sep)
        while width == 2 and i >= 0 and i % 2:
            i = buf.find(sep, i + 1)
        if i >= 0 and (best < 0 or i + width < best):
            best = i + width
    return best


def iter_blocks(f, start, end, enc, block=BLOCK):
    """产出 (offset, bytes)，覆盖 [start, end)，除最后一块外均在换行处断开。"""
    width = _nl(enc)[1]
    f.seek(start)
    pos, carry = start, b''
    while pos < end:
        want = min(block, end - pos - len(carry))
        data = f.read(want) if want > 0 else b''
        buf = carry + data
        if not buf:
            return
        if not data or pos + len(buf) >= end:
            yield pos, buf
            return
        cut = _rfind_nl(buf, enc)
        if cut <= 0:
            if len(buf) < 8 * block:
                carry = buf
                continue
            cut = len(buf) - len(buf) % width  # 极端情况：8MB 内无换行，强行断开
        yield pos, buf[:cut]
        pos += cut
        carry = buf[cut:]


def _nonws(s):
    return sum(map(len, s.split()))


def _norm_title(t):
    return re.sub(r'[\s　:：、.．·_\-—]+', '', t)


def scan_headings(f, start, end, enc):
    """流式扫描。返回 (segments, words)。segment = [kind, title, line_start, body_start, body_end, nonws]"""
    errors = errors_for(enc)
    segs = [['pre', '', start, start, None, 0]]
    words = 0
    for off, buf in iter_blocks(f, start, end, enc):
        try:
            text = buf.decode(enc, errors)
        except UnicodeDecodeError:
            text = buf.decode(enc, 'replace')
        text = text.replace('\r', '\n')  # 等长替换，兼容 CR-only 与 CRLF，且不影响字节偏移
        pos_c, pos_b = 0, off
        for m in HEAD_RE.finditer(text):
            before = text[pos_c:m.start()]
            n = _nonws(before)
            segs[-1][5] += n
            words += n
            line_start = pos_b + len(before.encode(enc, errors))
            line_end_c = m.end() + 1 if m.end() < len(text) and text[m.end()] == '\n' else m.end()
            body_start = line_start + len(text[m.start():line_end_c].encode(enc, errors))
            segs[-1][4] = line_start
            kind = 'vol' if m.group('vol') else 'chap'
            title = clean_str(re.sub(r'\s+', ' ', m.group(0).strip(' \t\u3000\ufeff')))
            segs.append([kind, title, line_start, body_start, None, 0])
            pos_c, pos_b = line_end_c, body_start
        n = _nonws(text[pos_c:])
        segs[-1][5] += n
        words += n
    segs[-1][4] = end
    return segs, words


def build_chapters(segs):
    """把原始分段整理成章节列表 [(title, start, end, vol_index)] 与卷名列表。"""
    chapters, vols = [], []
    vol = -1
    # 去重：标题与下一个标题相同且自身无正文（常见于「标题行重复一次」）
    cleaned = []
    for i, s in enumerate(segs):
        nxt = segs[i + 1] if i + 1 < len(segs) else None
        if (s[0] != 'pre' and s[5] == 0 and nxt is not None and nxt[0] != 'pre'
                and _norm_title(nxt[1]) == _norm_title(s[1])):
            nxt[2], nxt[3] = s[2], s[3]  # 让下一个（重复的）标题行并入正文，渲染时再去掉
            continue
        cleaned.append(s)
    for kind, title, line_start, body_start, body_end, nonws in cleaned:
        if kind == 'pre':
            if nonws > 0:
                chapters.append(['前言', body_start, body_end, -1])
        elif kind == 'vol':
            vols.append(title)
            vol = len(vols) - 1
            if nonws > 0:
                chapters.append([title, body_start, body_end, vol])
        else:
            if nonws == 0:  # 空章节（如文首目录）并入上一章，文字不丢
                if chapters:
                    chapters[-1][2] = body_end
                else:
                    chapters.append(['前言', line_start, body_end, vol])
            else:
                chapters.append([title, body_start, body_end, vol])
    return chapters, vols


def split_large(f, chapters, enc, has_heads):
    out = []
    width = _nl(enc)[1]
    page_no = 0
    for title, start, end, vol in chapters:
        size = end - start
        if size <= SPLIT_OVER:
            out.append([title, start, end, vol])
            continue
        parts = max(2, round(size / SPLIT_PART))
        step = size // parts
        cuts = [start]
        for k in range(1, parts):
            approx = start + k * step
            approx -= (approx - start) % width
            f.seek(approx)
            probe = f.read(8192)
            i = _find_nl(probe, enc)
            cut = approx + i if i > 0 else approx
            if cuts[-1] < cut < end:
                cuts.append(cut)
        cuts.append(end)
        n = len(cuts) - 1
        for k in range(n):
            page_no += 1
            name = '第%d页' % page_no if not has_heads else '%s（%d/%d）' % (title, k + 1, n)
            out.append([name, cuts[k], cuts[k + 1], vol])
    return out


def parse_book(path, enc_hint=''):
    """解析整本书，返回 dict(enc, bom, words, chapters, vols)。"""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        sample = f.read(65536)
        enc, bom = sniff_bom(sample)
        if not enc:
            enc, bom = detect_encoding(sample)
            if not enc and size > len(sample):  # 开头全是 ASCII，再取中段样本
                f.seek(size // 2)
                mid = f.read(65536)
                i = mid.find(b'\n')
                enc, _ = detect_encoding(mid[i + 1:] if i >= 0 else mid)
            enc = enc or enc_hint or 'utf-8'
        segs, words = scan_headings(f, bom, size, enc)
        has_heads = len(segs) > 1
        chapters, vols = build_chapters(segs) if has_heads else ([], [])
        if not chapters:
            chapters = [['正文', bom, size, -1]]
        chapters = split_large(f, chapters, enc, has_heads)
    return {'enc': enc, 'bom': bom, 'words': words, 'chapters': chapters, 'vols': vols}


def read_head(path, n=4096):
    """读取文件头，返回 (encoding, 文本)。用于扫描阶段提取元数据。"""
    with open(path, 'rb') as f:
        data = f.read(n)
    enc, bom = detect_encoding(data)
    return enc, decode_display(data[bom:], enc)


def chapter_paragraphs(text, title):
    """正文 → 段落列表；若首段与标题重复则去掉。"""
    lines = [l.strip() for l in text.replace('\r', '\n').split('\n')]
    paras = [l for l in lines if l]
    if paras and _norm_title(paras[0]) == _norm_title(title):
        paras = paras[1:]
    return paras
