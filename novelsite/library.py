"""书库：SQLite 存储、目录扫描、章节索引缓存。

资源策略：
- 扫描阶段只 stat 文件 + 读前 4KB（提取作者/简介），十万本也只是几十 MB 的数据库。
- 章节目录在「第一次打开某本书」时才解析，压缩后存入 toc 表；没读过的书不占空间。
- 全文检索刻意不做：十万本书的全文索引会和原文一样大。只做书名/作者检索 + 单本书内搜索。
"""

import bisect
import codecs
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import zlib
from collections import OrderedDict

from . import textproc as tp

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY,
  path BLOB NOT NULL UNIQUE,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL,
  mtime INTEGER NOT NULL,
  added INTEGER NOT NULL,
  encoding TEXT NOT NULL DEFAULT '',
  words INTEGER NOT NULL DEFAULT 0,
  chapters INTEGER NOT NULL DEFAULT 0,
  intro TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS books_title ON books(title);
CREATE INDEX IF NOT EXISTS books_author ON books(author);
CREATE INDEX IF NOT EXISTS books_category ON books(category);
CREATE INDEX IF NOT EXISTS books_added ON books(added);
CREATE TABLE IF NOT EXISTS toc (
  book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
  sig TEXT NOT NULL,
  data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS shelf (
  book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
  added INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
  book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
  chapter INTEGER NOT NULL,
  byte_off INTEGER NOT NULL,
  pos REAL NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  updated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS progress_updated ON progress(updated);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

LIST_COLS = 'id, title, author, category, size, words, chapters, intro'
SORTS = {
    'added': 'added DESC, id DESC',
    'title': 'title, id',
    'author': 'author, title, id',
    'size': 'size DESC, id',
}
TOC_CACHE = 32


def connect(path):
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA)
    return conn


def like_escape(s):
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def walk_txt(root_b):
    """遍历目录（跟随符号链接但防环），产出 (相对路径, 绝对路径, size, mtime)，路径均为 bytes。"""
    prefix = len(os.path.join(root_b, b''))
    stack, seen = [root_b], set()
    while stack:
        d = stack.pop()
        try:
            st = os.stat(d)
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name.startswith(b'.'):
                    continue
                try:
                    if e.is_dir():
                        stack.append(e.path)
                        continue
                    if not e.name.lower().endswith(b'.txt') or not e.is_file():
                        continue
                    s = e.stat()
                except OSError:
                    continue
                yield e.path[prefix:], e.path, s.st_size, int(s.st_mtime)


def file_meta(rel, full):
    """由路径与文件头得到 (title, author, category, encoding, intro)。"""
    name = os.path.basename(rel)
    title, author, tag, sure = tp.parse_filename(tp.bytes_display(name[:-4]))
    reldir = os.path.dirname(rel)
    parts = [tp.bytes_display(p) for p in reldir.split(os.sep.encode()) if p] if reldir else []
    category = '/'.join(parts) or tag
    try:
        enc, head = tp.read_head(full)
    except OSError:
        enc, head = '', ''
    head_author, intro = tp.extract_head_meta(head)
    if not sure:
        author = head_author or author
    return (tp.clean_str(title), tp.clean_str(author), tp.clean_str(category), enc, tp.clean_str(intro))


class Library:
    def __init__(self, db_path, root):
        self.db_path = db_path
        self.root = os.path.abspath(root)
        self.root_b = os.fsencode(self.root)
        self.conn = connect(db_path)
        self.lock = threading.RLock()
        self._toc = OrderedDict()
        self._toc_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self.scan_state = {'running': False}

    # ------------------------------------------------------------ 基础查询
    def query(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def execute(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).rowcount

    def meta_get(self, k, default=None):
        r = self.one('SELECT v FROM meta WHERE k=?', (k,))
        return r[0] if r else default

    def meta_set(self, k, v):
        self.execute('INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)', (k, str(v)))

    def secret(self):
        s = self.meta_get('secret')
        if not s:
            s = secrets.token_hex(32)
            self.meta_set('secret', s)
        return s

    def book(self, book_id):
        return self.one('SELECT * FROM books WHERE id=?', (book_id,))

    def abspath(self, book):
        return os.path.join(self.root_b, bytes(book['path']))

    def stats(self):
        r = self.one('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM books')
        parsed = self.one('SELECT COUNT(*) FROM toc')[0]
        try:
            dbsize = sum(os.path.getsize(self.db_path + s) for s in ('', '-wal') if os.path.exists(self.db_path + s))
        except OSError:
            dbsize = 0
        return {'books': r[0], 'bytes': r[1], 'parsed': parsed, 'dbsize': dbsize,
                'last_scan': self.meta_get('last_scan')}

    # ------------------------------------------------------------ 检索与浏览
    def search(self, q='', author=None, cat=None, exact_dir=None, sort='', page=1, per=30):
        where, args = [], []
        for kw in q.split()[:6]:
            like = '%' + like_escape(kw) + '%'
            where.append("(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\')")
            args += [like, like]
        if author is not None:
            where.append('author = ?')
            args.append(author)
        if cat:
            where.append("(category = ? OR category LIKE ? ESCAPE '\\')")
            args += [cat, like_escape(cat) + '/%']
        if exact_dir is not None:
            where.append('category = ?')
            args.append(exact_dir)
        w = (' WHERE ' + ' AND '.join(where)) if where else ''
        total = self.one('SELECT COUNT(*) FROM books' + w, args)[0]
        oargs = []
        if sort in SORTS:
            order = SORTS[sort]
        elif q.strip():  # 默认按相关度：完全匹配 > 前缀匹配 > 书名越短越靠前
            kw = q.split()[0]
            order = "(title = ?) DESC, (title LIKE ? ESCAPE '\\') DESC, length(title), id"
            oargs = [kw, like_escape(kw) + '%']
        else:
            order = SORTS['added']
        rows = self.query('SELECT %s FROM books%s ORDER BY %s LIMIT ? OFFSET ?' % (LIST_COLS, w, order),
                          args + oargs + [per, (page - 1) * per])
        return total, rows

    def subdirs(self, prefix):
        if prefix:
            rows = self.query("SELECT category, COUNT(*) FROM books WHERE category LIKE ? ESCAPE '\\' "
                              'GROUP BY category', (like_escape(prefix) + '/%',))
            plen = len(prefix) + 1
        else:
            rows = self.query("SELECT category, COUNT(*) FROM books WHERE category != '' GROUP BY category")
            plen = 0
        agg = {}
        for cat, n in rows:
            child = cat[plen:].split('/', 1)[0]
            agg[child] = agg.get(child, 0) + n
        return sorted(agg.items())

    def random_id(self):
        r = self.one('SELECT MAX(id) FROM books')
        if not r or not r[0]:
            return None
        r = self.one('SELECT id FROM books WHERE id >= ? ORDER BY id LIMIT 1',
                     (secrets.randbelow(r[0]) + 1,))
        return r[0] if r else None

    def recent(self, limit=20):
        return self.query(
            'SELECT b.id, b.title, b.author, b.size, p.chapter, p.byte_off, p.pos, p.title AS ch_title, '
            'p.updated FROM progress p JOIN books b ON b.id = p.book_id ORDER BY p.updated DESC LIMIT ?',
            (limit,))

    def shelf(self):
        return self.query(
            'SELECT b.id, b.title, b.author, b.size, b.chapters, p.chapter, p.byte_off, p.pos, '
            'p.title AS ch_title, p.updated FROM shelf s JOIN books b ON b.id = s.book_id '
            'LEFT JOIN progress p ON p.book_id = s.book_id ORDER BY COALESCE(p.updated, s.added) DESC')

    def on_shelf(self, book_id):
        return self.one('SELECT 1 FROM shelf WHERE book_id=?', (book_id,)) is not None

    def set_shelf(self, book_id, on):
        if on:
            self.execute('INSERT OR IGNORE INTO shelf(book_id, added) VALUES (?, ?)', (book_id, int(time.time())))
        else:
            self.execute('DELETE FROM shelf WHERE book_id=?', (book_id,))

    def progress(self, book_id):
        return self.one('SELECT * FROM progress WHERE book_id=?', (book_id,))

    def save_progress(self, book, toc, idx, pos):
        title, start = toc['chapters'][idx][0], toc['chapters'][idx][1]
        self.execute('INSERT OR REPLACE INTO progress(book_id, chapter, byte_off, pos, title, updated) '
                     'VALUES (?, ?, ?, ?, ?, ?)', (book['id'], idx, start, pos, title, int(time.time())))

    def clear_history(self):
        self.execute('DELETE FROM progress WHERE book_id NOT IN (SELECT book_id FROM shelf)')

    # ------------------------------------------------------------ 章节索引
    def toc(self, book):
        """返回解析好的目录（带内存 LRU 与持久缓存）。文件不存在时抛 OSError。"""
        path = self.abspath(book)
        st = os.stat(path)
        sig = '%d:%d:%d' % (tp.PARSER_VERSION, st.st_size, int(st.st_mtime))
        bid = book['id']
        with self._toc_lock:
            hit = self._toc.get(bid)
            if hit is not None and hit['sig'] == sig:
                self._toc.move_to_end(bid)
                return hit
            row = self.one('SELECT sig, data FROM toc WHERE book_id=?', (bid,))
            if row is not None and row['sig'] == sig:
                t = json.loads(zlib.decompress(row['data']).decode('utf-8'))
            else:
                t = tp.parse_book(path, book['encoding'])
                blob = zlib.compress(json.dumps(t, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), 6)
                with self.lock:
                    self.conn.execute('INSERT OR REPLACE INTO toc(book_id, sig, data) VALUES (?, ?, ?)',
                                      (bid, sig, blob))
                    self.conn.execute('UPDATE books SET encoding=?, words=?, chapters=?, size=?, mtime=? '
                                      'WHERE id=?', (t['enc'], t['words'], len(t['chapters']),
                                                     st.st_size, int(st.st_mtime), bid))
            t['sig'] = sig
            t['starts'] = [c[1] for c in t['chapters']]
            self._toc[bid] = t
            while len(self._toc) > TOC_CACHE:
                self._toc.popitem(last=False)
            return t

    @staticmethod
    def chapter_at(toc, byte_off):
        i = bisect.bisect_right(toc['starts'], byte_off) - 1
        return max(0, min(i, len(toc['chapters']) - 1))

    def chapter_text(self, book, toc, idx):
        _, start, end, _ = toc['chapters'][idx]
        with open(self.abspath(book), 'rb') as f:
            f.seek(start)
            data = f.read(end - start)
        return tp.decode_display(data, toc['enc'])

    def find_in_book(self, book, toc, q, limit=200):
        pat = re.compile(re.escape(q), re.I)
        hits = []
        with open(self.abspath(book), 'rb') as f:
            for i, (title, start, end, _) in enumerate(toc['chapters']):
                f.seek(start)
                text = tp.decode_display(f.read(end - start), toc['enc'])
                for m in pat.finditer(text):
                    a, b = max(0, m.start() - 30), m.end() + 30
                    hits.append((i, title, ' '.join(text[a:m.start()].split()), m.group(0),
                                 ' '.join(text[m.end():b].split())))
                    if len(hits) >= limit:
                        return hits, True
        return hits, False

    def iter_utf8(self, book, toc):
        """把原文件流式转成 UTF-8（下载用）。"""
        enc = toc['enc']
        with open(self.abspath(book), 'rb') as f:
            f.seek(toc['bom'])
            dec = codecs.getincrementaldecoder(enc)('replace')
            while True:
                data = f.read(1 << 18)
                if not data:
                    tail = dec.decode(b'', True)
                    if tail:
                        yield tail.encode('utf-8')
                    return
                yield dec.decode(data).encode('utf-8')

    # ------------------------------------------------------------ 扫描
    def scan(self, prune=False, log=None):
        """增量扫描书库。另开连接，可在后台线程运行。返回 False 表示已有扫描在进行。"""
        if not self._scan_lock.acquire(blocking=False):
            return False
        st = {'running': True, 'phase': '遍历目录', 'found': 0, 'done': 0, 'total': 0, 'added': 0,
              'updated': 0, 'moved': 0, 'removed': 0, 'started': time.time(), 'finished': None, 'error': ''}
        self.scan_state = st
        conn = None
        try:
            if not os.path.isdir(self.root):
                raise RuntimeError('书库目录不存在：%s' % self.root)
            conn = connect(self.db_path)
            existing = {bytes(r[1]): (r[0], r[2], r[3])
                        for r in conn.execute('SELECT id, path, size, mtime FROM books')}
            seen, new, changed = set(), [], []
            for rel, full, size, mtime in walk_txt(self.root_b):
                seen.add(rel)
                st['found'] += 1
                e = existing.get(rel)
                if e is None:
                    new.append((rel, full, size, mtime))
                elif e[1] != size or e[2] != mtime:
                    changed.append((e[0], rel, full, size, mtime))
            missing = {rel: v for rel, v in existing.items() if rel not in seen}
            by_sig = {}
            for rel, (_, size, mtime) in missing.items():
                by_sig.setdefault((size, mtime), []).append(rel)

            st['phase'], st['total'] = '读取元数据', len(new) + len(changed)
            now = int(time.time())
            conn.execute('BEGIN')
            batch = 0
            for rel, full, size, mtime in new:
                title, author, cat, enc, intro = file_meta(rel, full)
                cands = by_sig.get((size, mtime))
                if cands:  # 同大小同修改时间的书消失了又出现在别处：视为移动/改名，保留书架与进度
                    bid = missing.pop(cands.pop())[0]
                    conn.execute('UPDATE books SET path=?, title=?, author=?, category=? WHERE id=?',
                                 (rel, title, author, cat, bid))
                    st['moved'] += 1
                else:
                    conn.execute('INSERT INTO books(path, title, author, category, size, mtime, added, '
                                 'encoding, intro) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                 (rel, title, author, cat, size, mtime, now, enc, intro))
                    st['added'] += 1
                st['done'] += 1
                batch += 1
                if batch >= 500:
                    conn.execute('COMMIT')
                    conn.execute('BEGIN')
                    batch = 0
            for bid, rel, full, size, mtime in changed:
                title, author, cat, enc, intro = file_meta(rel, full)
                conn.execute('UPDATE books SET title=?, author=?, category=?, size=?, mtime=?, encoding=?, '
                             'intro=?, words=0, chapters=0 WHERE id=?',
                             (title, author, cat, size, mtime, enc, intro, bid))
                conn.execute('DELETE FROM toc WHERE book_id=?', (bid,))
                st['updated'] += 1
                st['done'] += 1
            if missing:
                if not prune and len(missing) > 100 and len(missing) > len(existing) // 2:
                    st['error'] = ('有 %d 本书找不到了（超过原有一半），疑似书库未挂载，已跳过删除。'
                                   '确认无误请执行 scan --prune。' % len(missing))
                else:
                    conn.executemany('DELETE FROM books WHERE id=?', [(v[0],) for v in missing.values()])
                    st['removed'] = len(missing)
            conn.execute('INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)', ('last_scan', str(now)))
            conn.execute('COMMIT')
            with self._toc_lock:
                self._toc.clear()
        except Exception as ex:  # noqa: BLE001 —— 扫描失败不能拖垮服务
            if conn is not None and conn.in_transaction:
                conn.execute('ROLLBACK')
            st['error'] = '扫描失败：%s' % ex
        finally:
            st['running'] = False
            st['finished'] = time.time()
            if conn is not None:
                conn.close()
            self._scan_lock.release()
            if log:
                log(st)
        return True
