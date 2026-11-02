"""HTTP 层：路由、页面渲染、鉴权。只用标准库。

安全要点：
- 所有动态内容经 esc() 转义；CSP 禁止内联脚本，即使漏转义也难以执行 XSS。
- 文件只能通过数据库 id 访问，路径从不来自用户输入，不存在目录穿越。
- POST 校验 Origin；Cookie 为 HttpOnly + SameSite=Lax，HMAC 签名，改密码即全部失效。
- 未设密码时只允许 localhost 访问并校验 Host 头（防 DNS rebinding）。
"""

import gzip
import hashlib
import hmac
import html
import json
import math
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from . import textproc as tp

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
STATIC_TYPES = {'.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                '.svg': 'image/svg+xml'}
COOKIE = 'ns_auth'
COOKIE_DAYS = 30
PER_PAGE = 30
MAX_BODY = 64 * 1024
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
LOOPBACK_NAMES = ('localhost', '127.0.0.1', '::1')


# ---------------------------------------------------------------- 小工具

def esc(s):
    return html.escape('' if s is None else str(s), quote=True)


def qs(**kw):
    return urlencode({k: v for k, v in kw.items() if v is not None and v != ''})


def fmt_size(n):
    if n >= 1 << 30:
        return '%.1f GB' % (n / (1 << 30))
    if n >= 1 << 20:
        return '%.1f MB' % (n / (1 << 20))
    return '%d KB' % max(1, n // 1024)


def fmt_words(n):
    if not n:
        return ''
    return '%.1f 万字' % (n / 10000) if n >= 10000 else '%d 字' % n


def fmt_time(ts):
    if not ts:
        return ''
    d = time.time() - float(ts)
    if d < 60:
        return '刚刚'
    if d < 3600:
        return '%d 分钟前' % (d // 60)
    if d < 86400:
        return '%d 小时前' % (d // 3600)
    if d < 86400 * 30:
        return '%d 天前' % (d // 86400)
    return time.strftime('%Y-%m-%d', time.localtime(float(ts)))


def pct(byte_off, size):
    return min(100, int(byte_off * 100 / size)) if size else 0


def safe_next(target):
    """只允许站内相对路径，防开放重定向。"""
    if not target or not target.startswith('/') or target.startswith('//') or target.startswith('/\\'):
        return '/'
    return target


def host_of(host_header):
    h = (host_header or '').strip().lower()
    if h.startswith('['):
        return h[1:h.find(']')] if ']' in h else h
    return h.rsplit(':', 1)[0] if h.count(':') == 1 else h


class Response:
    __slots__ = ('status', 'body', 'ctype', 'headers', 'stream', 'length')

    def __init__(self, body=b'', status=200, ctype='text/html; charset=utf-8', headers=None,
                 stream=None, length=None):
        self.status = status
        self.body = body.encode('utf-8') if isinstance(body, str) else body
        self.ctype = ctype
        self.headers = headers or []
        self.stream = stream
        self.length = length


def redirect(url, status=303):
    return Response(b'', status, headers=[('Location', url)])


class Request:
    def __init__(self, handler, method, path, query, body):
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self._form = None

    def arg(self, k, default=''):
        return self.query.get(k, default)

    def int_arg(self, k, default=1, lo=1, hi=10 ** 7):
        try:
            return min(hi, max(lo, int(self.query.get(k, default))))
        except ValueError:
            return default

    @property
    def form(self):
        if self._form is None:
            try:
                raw = parse_qs(self.body.decode('utf-8', 'replace'), keep_blank_values=True, max_num_fields=50)
            except ValueError:
                raw = {}
            self._form = {k: v[0] for k, v in raw.items()}
        return self._form

    def header(self, k, default=''):
        return self.handler.headers.get(k, default)

    def cookie(self, name):
        for part in self.header('Cookie').split(';'):
            k, _, v = part.strip().partition('=')
            if k == name:
                return v
        return ''


# ---------------------------------------------------------------- 应用

ROUTES = [
    ('GET', r'/', 'home'),
    ('GET', r'/search', 'search'),
    ('GET', r'/browse', 'browse'),
    ('GET', r'/shelf', 'shelf'),
    ('GET', r'/history', 'history'),
    ('GET', r'/random', 'random'),
    ('GET', r'/book/(\d{1,10})', 'book'),
    ('GET', r'/book/(\d{1,10})/find', 'find'),
    ('GET', r'/read/(\d{1,10})', 'resume'),
    ('GET', r'/read/(\d{1,10})/(\d{1,7})', 'read'),
    ('GET', r'/dl/(\d{1,10})', 'download'),
    ('GET', r'/manage', 'manage'),
    ('GET', r'/login', 'login_form'),
    ('GET', r'/static/([\w.-]{1,64})', 'static'),
    ('GET', r'/robots\.txt', 'robots'),
    ('POST', r'/login', 'login'),
    ('POST', r'/logout', 'logout'),
    ('POST', r'/shelf', 'shelf_toggle'),
    ('POST', r'/history/clear', 'history_clear'),
    ('POST', r'/api/progress', 'api_progress'),
    ('POST', r'/manage/scan', 'manage_scan'),
]
PUBLIC = {'login_form', 'login', 'static', 'robots'}


class App:
    def __init__(self, lib, password='', name='书库', host_check=False, quiet=False):
        self.lib = lib
        self.name = name
        self.password = password
        self.host_check = host_check
        self.quiet = quiet
        self._key = b''
        if password:
            self._key = hmac.new(lib.secret().encode(), hashlib.sha256(password.encode()).digest(),
                                 hashlib.sha256).digest()
        self._fails = deque()
        self._fail_lock = threading.Lock()
        self.static_files = {}
        for fn in sorted(os.listdir(STATIC_DIR)):
            ext = os.path.splitext(fn)[1]
            if ext in STATIC_TYPES:
                with open(os.path.join(STATIC_DIR, fn), 'rb') as f:
                    self.static_files[fn] = (f.read(), STATIC_TYPES[ext])
        self.ver = hashlib.sha256(b''.join(v[0] for v in self.static_files.values())).hexdigest()[:10]
        self.routes = [(m, re.compile(p + r'\Z'), name) for m, p, name in ROUTES]

    # ------------------------------------------------------------ 分发
    def dispatch(self, h, method):
        if self.host_check and host_of(h.headers.get('Host')) not in LOOPBACK_NAMES:
            return self.error(403, '未设置密码时只允许通过 localhost / 127.0.0.1 访问。')
        url = urlsplit(h.path)
        try:
            query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True,
                                                  max_num_fields=50).items()}
        except ValueError:
            return self.error(400, '请求参数过多')
        body = b''
        if method == 'POST':
            if h.headers.get('Transfer-Encoding'):
                h.close_connection = True
                return self.error(411, '需要 Content-Length')
            try:
                n = int(h.headers.get('Content-Length') or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                h.close_connection = True
                return self.error(413, '请求体过大')
            body = h.rfile.read(n) if n else b''
            origin = h.headers.get('Origin')
            if origin is not None:
                # 反向代理可能改写 Host，因此也接受 X-Forwarded-Host（浏览器跨站请求无法伪造该头）
                hosts = {(h.headers.get(k) or '').lower() for k in ('Host', 'X-Forwarded-Host')}
                if urlsplit(origin).netloc.lower() not in hosts - {''}:
                    return self.error(403, '跨站请求被拒绝')
        req = Request(h, method, url.path, query, body)
        want = 'GET' if method == 'HEAD' else method
        allowed = False
        for m, rx, name in self.routes:
            mt = rx.match(url.path)
            if not mt:
                continue
            if m != want:
                allowed = True
                continue
            if name not in PUBLIC and not self.authed(req):
                if want == 'GET':
                    return redirect('/login?' + qs(next=h.path))
                return self.error(401, '未登录或登录已过期')
            return getattr(self, name)(req, *mt.groups())
        return self.error(405, '方法不允许') if allowed else self.error(404, '页面不存在')

    # ------------------------------------------------------------ 鉴权
    def make_token(self):
        exp = str(int(time.time()) + COOKIE_DAYS * 86400)
        return exp + '.' + hmac.new(self._key, exp.encode(), hashlib.sha256).hexdigest()

    def authed(self, req):
        if not self.password:
            return True
        exp, _, sig = req.cookie(COOKIE).partition('.')
        if not (exp.isascii() and exp.isdigit()) or int(exp) < time.time():
            return False
        good = hmac.new(self._key, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.encode(), good.encode())

    def _cookie(self, req, value, max_age):
        secure = '; Secure' if req.header('X-Forwarded-Proto').lower() == 'https' else ''
        return ('Set-Cookie', '%s=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax%s'
                % (COOKIE, value, max_age, secure))

    # ------------------------------------------------------------ 页面骨架
    def layout(self, title, body, q='', bare=False, cls='', refresh=0):
        head_extra = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ''
        top = '' if bare else (
            '<header class="top"><div class="wrap">'
            '<a class="brand" href="/">%s</a>'
            '<form class="sbox" action="/search" role="search">'
            '<input type="search" name="q" value="%s" placeholder="书名 / 作者" aria-label="搜索">'
            '<button type="submit">搜索</button></form>'
            '<nav><a href="/shelf">书架</a><a href="/history">最近</a><a href="/browse">分类</a>'
            '<a href="/random">随机</a><a href="/manage">管理</a>'
            '<button type="button" id="theme" title="切换配色：默认 / 纸张 / 护眼 / 夜间">配色</button></nav>'
            '</div></header>' % (esc(self.name), esc(q)))
        page = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta name="robots" content="noindex,nofollow">%s<title>%s</title>'
                '<link rel="stylesheet" href="/static/style.css?v=%s">'
                '<script src="/static/app.js?v=%s"></script></head>'
                '<body class="%s">%s<main class="wrap">%s</main></body></html>'
                % (head_extra, esc(title), self.ver, self.ver, cls, top, body))
        return page

    def page(self, title, body, status=200, cache='private, no-cache', **kw):
        return Response(self.layout(title, body, **kw), status, headers=[('Cache-Control', cache)])

    def error(self, status, msg):
        body = '<div class="empty"><h1>%d</h1><p>%s</p><p><a href="/">返回首页</a></p></div>' % (status, esc(msg))
        return Response(self.layout('出错了', body), status)

    def book_li(self, r, show_cat=True):
        meta = []
        if r['author']:
            meta.append('<a href="/search?%s">%s</a>' % (esc(qs(author=r['author'])), esc(r['author'])))
        if show_cat and r['category']:
            meta.append('<a href="/browse?%s">%s</a>' % (esc(qs(dir=r['category'])), esc(r['category'])))
        meta.append(fmt_words(r['words']) or fmt_size(r['size']))
        intro = ' '.join((r['intro'] or '').split())
        return ('<li><a class="t" href="/book/%d">%s</a><div class="m">%s</div>%s</li>'
                % (r['id'], esc(r['title']), ' · '.join(meta),
                   '<p class="i">%s</p>' % esc(intro) if intro else ''))

    def book_list(self, rows, show_cat=True):
        if not rows:
            return '<p class="empty">没有找到书。</p>'
        return '<ul class="books">%s</ul>' % ''.join(self.book_li(r, show_cat) for r in rows)

    def pager(self, total, page, per, base):
        pages = max(1, (total + per - 1) // per)
        if pages <= 1:
            return ''

        def link(p, label):
            return '<a href="?%s">%s</a>' % (esc(qs(**dict(base, page=p if p > 1 else None))), label)

        parts = []
        if page > 1:
            parts += [link(1, '首页'), link(page - 1, '上一页')]
        parts.append('<span>%d / %d</span>' % (page, pages))
        if page < pages:
            parts += [link(page + 1, '下一页'), link(pages, '末页')]
        hidden = ''.join('<input type="hidden" name="%s" value="%s">' % (esc(k), esc(v))
                         for k, v in base.items() if v not in (None, ''))
        parts.append('<form class="jump">%s<input type="number" name="page" min="1" max="%d" value="%d" '
                     'aria-label="页码"><button type="submit">跳转</button></form>' % (hidden, pages, page))
        return '<nav class="pager">%s</nav>' % ''.join(parts)

    def progress_li(self, r, shelf=False):
        if r['updated']:
            info = '读到 %s · %d%% · %s' % (esc(r['ch_title']), pct(r['byte_off'], r['size']),
                                          fmt_time(r['updated']))
            act = '<a class="btn pri" href="/read/%d">继续阅读</a>' % r['id']
        else:
            info = '未开始'
            act = '<a class="btn pri" href="/read/%d">开始阅读</a>' % r['id']
        extra = ''
        if shelf:
            extra = ('<form method="post" action="/shelf"><input type="hidden" name="id" value="%d">'
                     '<input type="hidden" name="on" value="0"><input type="hidden" name="back" value="/shelf">'
                     '<button type="submit" class="btn">移出</button></form>' % r['id'])
        return ('<li><div><a class="t" href="/book/%d">%s</a><div class="m">%s%s</div></div>'
                '<div class="acts">%s%s</div></li>'
                % (r['id'], esc(r['title']), esc(r['author']) + ' · ' if r['author'] else '', info, act, extra))

    def scan_notice(self):
        s = self.lib.scan_state
        if not s.get('running'):
            return ''
        return ('<p class="notice">正在扫描书库：%s，已发现 %d 本，已处理 %d / %d。<a href="/manage">查看</a></p>'
                % (esc(s['phase']), s['found'], s['done'], s['total']))

    # ------------------------------------------------------------ 浏览类页面
    def home(self, req):
        lib = self.lib
        total = lib.one('SELECT COUNT(*) FROM books')[0]
        parts = [self.scan_notice(),
                 '<form class="bigsearch" action="/search" role="search">'
                 '<input type="search" name="q" placeholder="在 %d 本书中搜索书名或作者" aria-label="搜索" autofocus>'
                 '<button type="submit">搜索</button></form>' % total]
        recent = lib.recent(8)
        if recent:
            parts.append('<section><h2>继续阅读<a class="more" href="/history">全部</a></h2>'
                         '<ul class="plist">%s</ul></section>' % ''.join(self.progress_li(r) for r in recent))
        cats = sorted(lib.subdirs(''), key=lambda c: -c[1])
        if cats:
            parts.append('<section><h2>分类<a class="more" href="/browse">全部</a></h2><div class="tags">%s</div>'
                         '</section>' % ''.join('<a href="/browse?%s">%s<small>%d</small></a>'
                                                % (esc(qs(dir=c)), esc(c), n) for c, n in cats[:60]))
        _, rows = lib.search(sort='added', per=12)
        parts.append('<section><h2>最近入库<a class="more" href="/search?sort=added">全部书籍</a></h2>%s</section>'
                     % self.book_list(rows))
        if not total and not lib.scan_state.get('running'):
            parts.append('<p class="empty">书库还是空的。到 <a href="/manage">管理</a> 页面扫描书库。</p>')
        return self.page(self.name, ''.join(parts))

    def search(self, req):
        q = req.arg('q').strip()[:100]
        author = req.query.get('author')
        cat = req.arg('cat').strip() or None
        sort = req.arg('sort')
        page = req.int_arg('page')
        total, rows = self.lib.search(q, author=author, cat=cat, sort=sort, page=page, per=PER_PAGE)
        if author is not None:
            heading = '作者：%s' % esc(author or '（未知）')
        elif q:
            heading = '搜索：%s' % esc(q)
        else:
            heading = '全部书籍'
        if cat:
            heading += ' <small>（%s）</small>' % esc(cat)
        base = {'q': q, 'author': author, 'cat': cat, 'sort': sort}
        sorts = [('', '相关度' if q else '最近入库'), ('title', '书名'), ('author', '作者'), ('size', '大小')]
        if not q:
            sorts[0] = ('added', '最近入库')
        sort_html = ''.join('<a href="?%s"%s>%s</a>' % (esc(qs(**dict(base, sort=k, page=None))),
                                                         ' class="on"' if sort == k or (not sort and i == 0) else '',
                                                         label)
                            for i, (k, label) in enumerate(sorts))
        body = ('<h1>%s</h1><div class="bar"><span>共 %d 本</span><span class="sorts">%s</span></div>%s%s'
                % (heading, total, sort_html, self.book_list(rows), self.pager(total, page, PER_PAGE, base)))
        return self.page('%s - %s' % (q or author or '全部书籍', self.name), body, q=q)

    def browse(self, req):
        d = req.arg('dir').strip('/')
        page = req.int_arg('page')
        crumbs = ['<a href="/browse">全部分类</a>']
        acc = []
        for part in d.split('/') if d else []:
            acc.append(part)
            crumbs.append('<a href="/browse?%s">%s</a>' % (esc(qs(dir='/'.join(acc))), esc(part)))
        subs = self.lib.subdirs(d)
        parts = ['<nav class="crumbs">%s</nav>' % ' / '.join(crumbs)]
        if subs:
            parts.append('<div class="tags">%s</div>' % ''.join(
                '<a href="/browse?%s">%s<small>%d</small></a>' % (esc(qs(dir=(d + '/' + c) if d else c)), esc(c), n)
                for c, n in subs[:2000]))
        total, rows = self.lib.search(exact_dir=d, sort=req.arg('sort') or 'title', page=page, per=PER_PAGE)
        if d:
            parts.append('<form class="find" action="/search"><input type="hidden" name="cat" value="%s">'
                         '<input type="search" name="q" placeholder="在此分类（含子目录）中搜索" aria-label="分类内搜索">'
                         '<button type="submit">搜索</button></form>' % esc(d))
        if total or not subs:
            parts.append('<h2>本目录下 %d 本</h2>%s%s' % (total, self.book_list(rows, show_cat=False),
                                                     self.pager(total, page, PER_PAGE, {'dir': d})))
        return self.page('%s - %s' % (d or '分类', self.name), ''.join(parts))

    def shelf(self, req):
        rows = self.lib.shelf()
        body = '<h1>书架</h1>' + ('<ul class="plist">%s</ul>' % ''.join(self.progress_li(r, True) for r in rows)
                                 if rows else '<p class="empty">书架是空的。在书籍页面点「加入书架」。</p>')
        return self.page('书架 - ' + self.name, body)

    def history(self, req):
        rows = self.lib.recent(200)
        body = ['<h1>最近阅读</h1>']
        if rows:
            body.append('<ul class="plist">%s</ul>' % ''.join(self.progress_li(r) for r in rows))
            body.append('<form method="post" action="/history/clear" class="right"><button type="submit" class="btn">'
                        '清空记录（书架中的书保留进度）</button></form>')
        else:
            body.append('<p class="empty">还没有阅读记录。</p>')
        return self.page('最近阅读 - ' + self.name, ''.join(body))

    def history_clear(self, req):
        self.lib.clear_history()
        return redirect('/history')

    def random(self, req):
        bid = self.lib.random_id()
        return redirect('/book/%d' % bid if bid else '/')

    # ------------------------------------------------------------ 书籍与阅读
    def _load(self, book_id):
        book = self.lib.book(int(book_id))
        if book is None:
            return None, None, self.error(404, '书不存在')
        try:
            toc = self.lib.toc(book)
        except OSError:
            return book, None, self.error(404, '文件不存在，可能已被移动或删除。请到「管理」重新扫描。')
        return book, toc, None

    def book(self, req, book_id):
        book, toc, err = self._load(book_id)
        if err:
            return err
        bid = book['id']
        prog = self.lib.progress(bid)
        chapters, vols = toc['chapters'], toc['vols']
        meta = []
        if book['author']:
            meta.append('作者：<a href="/search?%s">%s</a>' % (esc(qs(author=book['author'])), esc(book['author'])))
        if book['category']:
            meta.append('分类：<a href="/browse?%s">%s</a>' % (esc(qs(dir=book['category'])), esc(book['category'])))
        meta += [fmt_words(toc['words']), '%d 章' % len(chapters), fmt_size(book['size']), toc['enc'].upper()]
        if prog:
            n = self.lib.chapter_at(toc, prog['byte_off']) + 1
            main_btn = ('<a class="btn pri" href="/read/%d">继续阅读：%s</a>'
                        % (bid, esc(chapters[n - 1][0])))
        else:
            main_btn = '<a class="btn pri" href="/read/%d/1">开始阅读</a>' % bid
            n = 0
        on = self.lib.on_shelf(bid)
        shelf_btn = ('<form method="post" action="/shelf"><input type="hidden" name="id" value="%d">'
                     '<input type="hidden" name="on" value="%d"><input type="hidden" name="back" value="/book/%d">'
                     '<button type="submit" class="btn">%s</button></form>'
                     % (bid, 0 if on else 1, bid, '移出书架' if on else '加入书架'))
        intro = ''.join('<p>%s</p>' % esc(l) for l in (book['intro'] or '').split('\n') if l.strip())
        items, cur_vol = [], None
        for i, (title, _, _, vol) in enumerate(chapters):
            if vol != cur_vol:
                cur_vol = vol
                if vol >= 0:
                    items.append('<li class="vol">%s</li>' % esc(vols[vol]))
            items.append('<li%s><a href="/read/%d/%d">%s</a></li>'
                         % (' class="cur"' if i + 1 == n else '', bid, i + 1, esc(title)))
        body = ('<article class="detail"><h1>%s</h1><div class="m">%s</div>%s'
                '<div class="acts">%s%s<a class="btn" href="/dl/%d">下载 TXT</a></div>'
                '<form class="find" action="/book/%d/find"><input type="search" name="q" placeholder="书内搜索" '
                'aria-label="书内搜索" required><button type="submit">搜索</button></form>'
                '<div class="path">%s</div></article>'
                '<h2 id="toc">目录</h2><ol class="toc">%s</ol>'
                % (esc(book['title']), ' · '.join(m for m in meta if m),
                   '<div class="intro">%s</div>' % intro if intro else '', main_btn, shelf_btn, bid, bid,
                   esc(tp.bytes_display(bytes(book['path']))), ''.join(items)))
        return self.page('%s - %s' % (book['title'], self.name), body)

    def resume(self, req, book_id):
        book, toc, err = self._load(book_id)
        if err:
            return err
        prog = self.lib.progress(book['id'])
        if not prog:
            return redirect('/read/%d/1' % book['id'])
        n = self.lib.chapter_at(toc, prog['byte_off']) + 1
        return redirect('/read/%d/%d#pos=%.3f' % (book['id'], n, prog['pos']))

    def read(self, req, book_id, num):
        book, toc, err = self._load(book_id)
        if err:
            return err
        bid, n, chapters = book['id'], int(num), toc['chapters']
        if not 1 <= n <= len(chapters):
            return redirect('/book/%d#toc' % bid)
        title, _, _, vol = chapters[n - 1]
        paras = tp.chapter_paragraphs(self.lib.chapter_text(book, toc, n - 1), title)
        hl = req.arg('hl')[:50]
        if hl:
            rx = re.compile('(%s)' % re.escape(hl), re.I)
            text = ''.join('<p>%s</p>' % ''.join('<mark>%s</mark>' % esc(s) if i % 2 else esc(s)
                                                 for i, s in enumerate(rx.split(p))) for p in paras)
        else:
            text = ''.join('<p>%s</p>' % esc(p) for p in paras)
        prev_url = '/read/%d/%d' % (bid, n - 1) if n > 1 else ''
        next_url = '/read/%d/%d' % (bid, n + 1) if n < len(chapters) else ''
        nav = ('<nav class="rnav">%s<a href="/book/%d#toc">目录</a>%s</nav>'
               % ('<a href="%s" rel="prev">上一章</a>' % prev_url if prev_url else '<span>已是第一章</span>', bid,
                  '<a href="%s" rel="next">下一章</a>' % next_url if next_url else '<span>已是最后一章</span>'))
        vol_html = '<div class="vol">%s</div>' % esc(toc['vols'][vol]) if vol >= 0 else ''
        body = ('<div class="rbar"><a class="back" href="/book/%d">‹ %s</a><span class="ct">%d / %d</span>'
                '<button type="button" id="set" aria-label="阅读设置" aria-expanded="false">Aa</button></div>'
                '<div id="panel" class="panel" hidden>'
                '<div class="row"><span>配色</span><button type="button" data-k="theme" data-v="">默认</button>'
                '<button type="button" data-k="theme" data-v="paper">纸张</button>'
                '<button type="button" data-k="theme" data-v="green">护眼</button>'
                '<button type="button" data-k="theme" data-v="dark">夜间</button></div>'
                '<div class="row"><span>字号</span><button type="button" data-k="fs" data-v="-1">A−</button>'
                '<button type="button" data-k="fs" data-v="1">A+</button></div>'
                '<div class="row"><span>行距</span><button type="button" data-k="lh" data-v="-0.1">紧</button>'
                '<button type="button" data-k="lh" data-v="0.1">松</button></div>'
                '<div class="row"><span>版心</span><button type="button" data-k="w" data-v="-2">窄</button>'
                '<button type="button" data-k="w" data-v="2">宽</button></div>'
                '<div class="row"><span>字体</span><button type="button" data-k="font" data-v="serif">宋体</button>'
                '<button type="button" data-k="font" data-v="sans">黑体</button>'
                '<button type="button" data-k="reset">重置</button></div></div>'
                '<article id="text" class="text" data-book="%d" data-ch="%d" data-prev="%s" data-next="%s">'
                '%s<h1>%s</h1>%s</article>%s%s'
                % (bid, esc(book['title']), n, len(chapters), bid, n, prev_url, next_url, vol_html, esc(title),
                   text or '<p class="empty">（本章无内容）</p>', nav,
                   '<link rel="prefetch" href="%s">' % next_url if next_url else ''))
        # 章节页只取决于文件内容（进度由 JS 另行上报），允许短时缓存，让「预取下一章」真正生效
        return self.page('%s - %s' % (title, book['title']), body, bare=True, cls='reading',
                         cache='private, max-age=300')

    def find(self, req, book_id):
        book, toc, err = self._load(book_id)
        if err:
            return err
        q = req.arg('q').strip()[:50]
        if not q:
            return redirect('/book/%d' % book['id'])
        hits, more = self.lib.find_in_book(book, toc, q)
        items = ''.join('<li><a href="/read/%d/%d?%s">%s</a><p>…%s<mark>%s</mark>%s…</p></li>'
                        % (book['id'], i + 1, esc(qs(hl=q)), esc(t), esc(a), esc(m), esc(b))
                        for i, t, a, m, b in hits)
        body = ('<nav class="crumbs"><a href="/book/%d">%s</a> / 书内搜索</nav>'
                '<form class="find" action="/book/%d/find"><input type="search" name="q" value="%s" aria-label="书内搜索">'
                '<button type="submit">搜索</button></form><h2>「%s」共 %s%d 处</h2>%s'
                % (book['id'], esc(book['title']), book['id'], esc(q), esc(q), '至少 ' if more else '', len(hits),
                   '<ol class="hits">%s</ol>' % items if items else '<p class="empty">没有找到。</p>'))
        return self.page('书内搜索 - %s' % book['title'], body)

    def download(self, req, book_id):
        book, toc, err = self._load(book_id)
        if err:
            return err
        name = book['title'] + ('_' + book['author'] if book['author'] else '') + '.txt'
        ascii_name = re.sub(r'[^A-Za-z0-9._-]', '_', name)
        disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quote(name, safe=''))
        headers = [('Content-Disposition', disp), ('Cache-Control', 'private, no-cache')]
        ctype = 'text/plain; charset=utf-8'
        if toc['enc'] == 'utf-8' and toc['bom'] == 0:  # 原本就是 UTF-8：直接按原文件发送
            path = self.lib.abspath(book)
            size = os.path.getsize(path)

            def raw():
                with open(path, 'rb') as f:
                    while True:
                        chunk = f.read(1 << 18)
                        if not chunk:
                            return
                        yield chunk
            return Response(status=200, ctype=ctype, headers=headers, stream=raw(), length=size)
        return Response(status=200, ctype=ctype, headers=headers, stream=self.lib.iter_utf8(book, toc))

    # ------------------------------------------------------------ 写操作
    def shelf_toggle(self, req):
        f = req.form
        try:
            bid = int(f.get('id', ''))
        except ValueError:
            return self.error(400, '参数错误')
        if self.lib.book(bid) is None:
            return self.error(404, '书不存在')
        self.lib.set_shelf(bid, f.get('on') == '1')
        return redirect(safe_next(f.get('back')))

    def api_progress(self, req):
        try:
            data = json.loads(req.body.decode('utf-8'))
            bid, n, pos = int(data['b']), int(data['c']), float(data.get('p', 0))
        except (ValueError, KeyError, TypeError, AttributeError):
            return Response(b'{"ok":false}', 400, 'application/json')
        book = self.lib.book(bid)
        if book is None:
            return Response(b'{"ok":false}', 404, 'application/json')
        try:
            toc = self.lib.toc(book)
        except OSError:
            return Response(b'{"ok":false}', 404, 'application/json')
        if not 1 <= n <= len(toc['chapters']):
            return Response(b'{"ok":false}', 400, 'application/json')
        pos = min(1.0, max(0.0, pos)) if math.isfinite(pos) else 0.0
        self.lib.save_progress(book, toc, n - 1, pos)
        return Response(b'{"ok":true}', 200, 'application/json')

    # ------------------------------------------------------------ 管理与登录
    def manage(self, req):
        s, st = self.lib.stats(), self.lib.scan_state
        if st.get('running'):
            scan = ('<p class="notice">扫描中：%s · 已发现 %d 本 · 已处理 %d / %d · 新增 %d</p>'
                    % (esc(st['phase']), st['found'], st['done'], st['total'], st['added']))
        elif st.get('finished'):
            scan = ('<p>上次扫描：%s，用时 %.1f 秒。发现 %d 本，新增 %d，更新 %d，移动 %d，移除 %d。</p>'
                    % (fmt_time(st['finished']), st['finished'] - st['started'], st['found'], st['added'],
                       st['updated'], st['moved'], st['removed']))
        else:
            scan = '<p>本次运行尚未扫描。</p>'
        if st.get('error'):
            scan += '<p class="notice warn">%s</p>' % esc(st['error'])
        logout = ('<form method="post" action="/logout"><button type="submit" class="btn">退出登录</button></form>'
                  if self.password else '')
        body = ('<h1>管理</h1><dl class="kv"><dt>书库目录</dt><dd>%s</dd><dt>书籍</dt><dd>%d 本 · %s</dd>'
                '<dt>已解析目录</dt><dd>%d 本（打开过的书才会解析）</dd><dt>数据库</dt><dd>%s · %s</dd>'
                '<dt>上次扫描完成</dt><dd>%s</dd></dl><h2>扫描书库</h2>%s'
                '<form method="post" action="/manage/scan"><button type="submit" class="btn pri"%s>重新扫描</button>'
                '</form><p class="m">增量扫描：只处理新增、改动、移动和删除的文件。</p>%s'
                % (esc(self.lib.root), s['books'], fmt_size(s['bytes']), s['parsed'], esc(self.lib.db_path),
                   fmt_size(s['dbsize']), fmt_time(s['last_scan']) or '从未', scan,
                   ' disabled' if st.get('running') else '', logout))
        return self.page('管理 - ' + self.name, body, refresh=2 if st.get('running') else 0)

    def manage_scan(self, req):
        threading.Thread(target=self.lib.scan, daemon=True, name='scan').start()
        time.sleep(0.2)
        return redirect('/manage')

    def login_form(self, req, msg=''):
        if not self.password:
            return redirect('/')
        body = ('<form class="login" method="post" action="/login"><h1>%s</h1>%s'
                '<input type="hidden" name="next" value="%s">'
                '<input type="password" name="password" placeholder="密码" aria-label="密码" autofocus required>'
                '<button type="submit" class="btn pri">登录</button></form>'
                % (esc(self.name), '<p class="notice warn">%s</p>' % esc(msg) if msg else '',
                   esc(safe_next(req.arg('next') or req.form.get('next')))))
        return Response(self.layout('登录 - ' + self.name, body, bare=True), 401 if msg else 200)

    def login(self, req):
        if not self.password:
            return redirect('/')
        now = time.time()
        with self._fail_lock:
            while self._fails and now - self._fails[0] > 300:
                self._fails.popleft()
            locked = len(self._fails) >= 5
        if locked:
            return self.login_form(req, '尝试次数过多，请 5 分钟后再试。')
        given = req.form.get('password', '')
        if hmac.compare_digest(hashlib.sha256(given.encode()).digest(),
                               hashlib.sha256(self.password.encode()).digest()):
            with self._fail_lock:
                self._fails.clear()
            return Response(b'', 303, headers=[('Location', safe_next(req.form.get('next'))),
                                               self._cookie(req, self.make_token(), COOKIE_DAYS * 86400)])
        with self._fail_lock:
            self._fails.append(now)
        time.sleep(1)
        return self.login_form(req, '密码错误。')

    def logout(self, req):
        return Response(b'', 303, headers=[('Location', '/login'), self._cookie(req, '', 0)])

    def static(self, req, name):
        f = self.static_files.get(name)
        if f is None:
            return self.error(404, '文件不存在')
        return Response(f[0], 200, f[1], headers=[('Cache-Control', 'public, max-age=31536000, immutable')])

    def robots(self, req):
        return Response(b'User-agent: *\nDisallow: /\n', 200, 'text/plain; charset=utf-8')


# ---------------------------------------------------------------- HTTP 服务器

class Handler(BaseHTTPRequestHandler):
    server_version = 'novelsite'
    sys_version = ''
    protocol_version = 'HTTP/1.1'
    timeout = 60

    def do_GET(self):
        self._handle('GET')

    def do_HEAD(self):
        self._handle('HEAD')

    def do_POST(self):
        self._handle('POST')

    def _handle(self, method):
        app = self.server.app
        try:
            resp = app.dispatch(self, method)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            resp = app.error(500, '服务器内部错误')
        try:
            self._send(resp, method == 'HEAD')
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send(self, r, head_only):
        body = r.body
        gz = (r.stream is None and len(body) > 1024 and r.ctype.startswith(('text/', 'application/json'))
              and 'gzip' in (self.headers.get('Accept-Encoding') or ''))
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(r.status)
        self.send_header('Content-Type', r.ctype)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', CSP)
        if r.ctype.startswith('text/'):
            self.send_header('Vary', 'Accept-Encoding')
        if gz:
            self.send_header('Content-Encoding', 'gzip')
        for k, v in r.headers:
            self.send_header(k, v)
        if r.stream is not None:
            if r.length is not None:
                self.send_header('Content-Length', str(r.length))
            else:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            try:
                if not head_only:
                    for chunk in r.stream:
                        self.wfile.write(chunk)
            finally:
                r.stream.close()
            return
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        if not self.server.app.quiet:
            sys.stderr.write('%s [%s] %s\n' % (self.address_string(), self.log_date_time_string(), fmt % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def make_server(app, host, port):
    import socket
    cls = Server
    if ':' in host:
        cls = type('Server6', (Server,), {'address_family': socket.AF_INET6})
    srv = cls((host, port), Handler)
    srv.app = app
    return srv
