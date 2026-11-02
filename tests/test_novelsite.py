"""运行：python3 -m unittest discover -s tests -v （在仓库根目录下）"""

import codecs
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from novelsite import textproc as tp  # noqa: E402
from novelsite.library import Library  # noqa: E402
from novelsite.web import App, make_server  # noqa: E402

PROSE = '　　他看着远处的山峰，心中不由得生出一股豪情。这一路走来，经历了太多的风风雨雨，可是他从来没有想过放弃。'
TRAD = '　　這個時候，他們還沒有發現後面的人。為了國家，他說：「我來了。」學會了國術。'


def make_novel(n=5, vols=0, toc_top=False, dup=False, prose=PROSE):
    out = ['书名：测试\n作者：张三\n内容简介：\n　　一个关于成长的故事。\n']
    if toc_top:
        out.append(''.join('第%d章 标题%d\n' % (i, i) for i in range(1, n + 1)))
    for i in range(1, n + 1):
        if vols and (i - 1) % (n // vols) == 0:
            out.append('第%d卷 卷%d\n' % ((i - 1) // (n // vols) + 1, i))
        t = '第%d章 标题%d\n' % (i, i)
        out.append(t + (t if dup else '') + (prose + str(i) + '\n') * 3)
    return ''.join(out)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='novelsite-test-')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return path


class TestEncoding(unittest.TestCase):
    def test_detect(self):
        text = make_novel(3)
        self.assertEqual(tp.detect_encoding(text.encode('utf-8')), ('utf-8', 0))
        self.assertEqual(tp.detect_encoding(text.encode('gb18030')), ('gb18030', 0))
        self.assertEqual(tp.detect_encoding((TRAD * 20).encode('cp950')), ('cp950', 0))
        self.assertEqual(tp.detect_encoding(codecs.BOM_UTF8 + text.encode('utf-8')), ('utf-8', 3))
        self.assertEqual(tp.detect_encoding(codecs.BOM_UTF16_LE + text.encode('utf-16-le')), ('utf-16-le', 2))
        self.assertEqual(tp.detect_encoding(codecs.BOM_UTF16_BE + text.encode('utf-16-be')), ('utf-16-be', 2))
        self.assertEqual(tp.detect_encoding(text.encode('utf-16-le'))[0], 'utf-16-le')
        self.assertEqual(tp.detect_encoding(b'hello world\n'), ('', 0))

    def test_utf8_with_a_bad_byte_is_still_utf8(self):
        data = make_novel(5).encode('utf-8')
        self.assertEqual(tp.detect_encoding(data[:600] + b'\xff' + data[600:])[0], 'utf-8')

    def test_truncated_sample_does_not_break_detection(self):
        data = make_novel(5).encode('utf-8')
        self.assertEqual(tp.detect_encoding(data[:1001])[0], 'utf-8')  # 截断在多字节字符中间

    def test_bytes_filename(self):
        self.assertEqual(tp.bytes_display('斗破苍穹'.encode('gbk')), '斗破苍穹')
        self.assertEqual(tp.bytes_display('斗破苍穹'.encode('utf-8')), '斗破苍穹')


class TestFilename(unittest.TestCase):
    def test_cases(self):
        cases = {
            '《斗破苍穹》（校对版全本）作者：天蚕土豆': ('斗破苍穹', '天蚕土豆', True),
            '斗破苍穹 作者：天蚕土豆': ('斗破苍穹', '天蚕土豆', True),
            '斗破苍穹(作者:天蚕土豆)': ('斗破苍穹', '天蚕土豆', True),
            '斗破苍穹_天蚕土豆': ('斗破苍穹', '天蚕土豆', False),
            '[玄幻]斗破苍穹(精校版)': ('斗破苍穹', '', False),
            '我的作者大人': ('我的作者大人', '', False),
            'The Hobbit by J.R.R. Tolkien': ('The Hobbit', 'J.R.R. Tolkien', True),
            '三体全集': ('三体', '', False),
        }
        for name, (title, author, sure) in cases.items():
            t, a, _, s = tp.parse_filename(name)
            self.assertEqual((t, a, s), (title, author, sure), name)
        self.assertEqual(tp.parse_filename('[玄幻]斗破苍穹')[2], '玄幻')

    def test_head_meta(self):
        author, intro = tp.extract_head_meta(make_novel(2))
        self.assertEqual(author, '张三')
        self.assertEqual(intro, '一个关于成长的故事。')
        self.assertEqual(tp.extract_head_meta('【作者】李四\n正文')[0], '李四')


class TestChapters(TempDirCase):
    def read_all(self, path, book):
        with open(path, 'rb') as f:
            out = []
            for title, start, end, _ in book['chapters']:
                f.seek(start)
                out.append((title, f.read(end - start).decode(book['enc'])))
            return out

    def assert_no_text_lost(self, chapters, text):
        joined = '\n'.join(t for _, t in chapters)
        for i in range(1, 6):
            self.assertIn(PROSE.strip() + str(i), joined)
        self.assertIn('一个关于成长的故事。', joined)

    def test_encodings_and_line_endings(self):
        text = make_novel(5, vols=1)
        variants = {
            'u8.txt': text.encode('utf-8'),
            'gb.txt': text.encode('gb18030'),
            'bom.txt': codecs.BOM_UTF8 + text.encode('utf-8'),
            'u16le.txt': codecs.BOM_UTF16_LE + text.replace('\n', '\r\n').encode('utf-16-le'),
            'u16be.txt': codecs.BOM_UTF16_BE + text.encode('utf-16-be'),
            'crlf.txt': text.replace('\n', '\r\n').encode('gb18030'),
            'cr.txt': text.replace('\n', '\r').encode('gb18030'),
        }
        for name, data in variants.items():
            with self.subTest(name=name):
                path = self.write(name, data)
                book = tp.parse_book(path)
                titles = [c[0] for c in book['chapters']]
                self.assertEqual(titles, ['前言'] + ['第%d章 标题%d' % (i, i) for i in range(1, 6)])
                self.assertEqual(book['vols'], ['第1卷 卷1'])
                chapters = self.read_all(path, book)
                self.assertTrue(chapters[3][1].strip().startswith(PROSE.strip() + '3'))
                self.assert_no_text_lost(chapters, text)

    def test_big5(self):
        text = '書名：天龍\n作者：金庸\n' + ''.join('第%d章 回目\n%s\n' % (i, TRAD) for i in range(1, 6))
        path = self.write('b5.txt', text.encode('cp950'))
        book = tp.parse_book(path)
        self.assertEqual(book['enc'], 'cp950')
        self.assertEqual(len(book['chapters']), 6)

    def test_toc_at_top_is_merged_into_preface(self):
        text = make_novel(5, toc_top=True)
        path = self.write('toc.txt', text.encode('utf-8'))
        book = tp.parse_book(path)
        self.assertEqual(len(book['chapters']), 6)  # 前言 + 5 章，而不是 11 章
        self.assert_no_text_lost(self.read_all(path, book), text)

    def test_duplicated_title_line(self):
        path = self.write('dup.txt', make_novel(4, dup=True).encode('utf-8'))
        book = tp.parse_book(path)
        self.assertEqual(len(book['chapters']), 5)
        title, body = self.read_all(path, book)[2]
        paras = tp.chapter_paragraphs(body, title)
        self.assertTrue(paras[0].startswith(PROSE.strip()[:5]))  # 重复的标题行被去掉

    def test_no_headings_and_oversized_chapter_are_paged(self):
        body = (PROSE + '\n') * 4000
        path = self.write('plain.txt', body.encode('utf-8'))
        book = tp.parse_book(path)
        self.assertGreater(len(book['chapters']), 3)
        self.assertEqual(book['chapters'][0][0], '第1页')
        size = os.path.getsize(path)
        ch = book['chapters']
        self.assertEqual(ch[0][1], 0)
        self.assertEqual(ch[-1][2], size)
        for a, b in zip(ch, ch[1:]):
            self.assertEqual(a[2], b[1])  # 分页首尾相接，不丢字节

    def test_false_positive_mid_prose_is_not_heading(self):
        text = '第1章 开始\n%s\n他说第2章写完了。\n' % PROSE
        path = self.write('fp.txt', text.encode('utf-8'))
        self.assertEqual(len(tp.parse_book(path)['chapters']), 1)


class TestWeb(TempDirCase):
    password = ''

    def setUp(self):
        super().setUp()
        lib_dir = os.path.join(self.dir, 'lib')
        self.write('lib/玄幻/《测试之书》作者：张三.txt', make_novel(6).encode('gb18030'))
        self.write('lib/<b onmouseover=alert(1)>.txt', make_novel(2).encode('utf-8'))
        self.lib = Library(os.path.join(self.dir, 'db.sqlite'), lib_dir)
        self.lib.scan()
        self.app = App(self.lib, password=self.password, host_check=not self.password, quiet=True)
        self.srv = make_server(self.app, '127.0.0.1', 0)
        self.base = 'http://127.0.0.1:%d' % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.lib.conn.close()
        super().tearDown()

    def req(self, path, data=None, headers=None, method=None):
        r = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, resp.read().decode('utf-8', 'replace'), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', 'replace'), e.headers

    def book_id(self, title):
        return self.lib.one('SELECT id FROM books WHERE title=?', (title,))[0]


class TestWebNoPassword(TestWeb):
    def test_pages(self):
        bid = self.book_id('测试之书')
        for path in ['/', '/search?q=测试', '/browse', '/shelf', '/history',
                     '/manage', '/book/%d' % bid, '/read/%d/2' % bid, '/static/style.css', '/robots.txt']:
            path = urllib.request.quote(path, safe='/?=&')
            status, _, _ = self.req(path)
            self.assertEqual(status, 200, path)
        status, body, headers = self.req('/read/%d/2' % bid)
        self.assertIn('第1章 标题1', body)
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(self.req('/book/99999')[0], 404)
        self.assertEqual(self.req('/read/%d/999' % bid)[0], 303)

    def test_xss_escaped(self):
        _, body, _ = self.req('/')
        self.assertNotIn('<b onmouseover', body)
        self.assertIn('&lt;b onmouseover=alert(1)&gt;', body)

    def test_progress_and_resume(self):
        bid = self.book_id('测试之书')
        payload = json.dumps({'b': bid, 'c': 3, 'p': 0.5}).encode()
        self.assertEqual(self.req('/api/progress', payload, {'Content-Type': 'application/json'})[0], 200)
        status, _, headers = self.req('/read/%d' % bid)
        self.assertEqual(status, 303)
        self.assertTrue(headers['Location'].endswith('/read/%d/3#pos=0.500' % bid))
        self.assertEqual(self.req('/api/progress', b'{"b":1,"c":999}')[0], 400)
        self.assertEqual(self.req('/api/progress', b'not json')[0], 400)

    def test_shelf_csrf_and_open_redirect(self):
        bid = self.book_id('测试之书')
        form = ('id=%d&on=1&back=//evil.example' % bid).encode()
        self.assertEqual(self.req('/shelf', form, {'Origin': 'http://evil.example'})[0], 403)
        status, _, headers = self.req('/shelf', form, {'Origin': self.base})
        self.assertEqual(status, 303)
        self.assertTrue(headers['Location'].endswith('/') and 'evil' not in headers['Location'])
        self.assertTrue(self.lib.on_shelf(bid))

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.req('/', headers={'Host': 'attacker.example'})[0], 403)

    def test_download_transcodes_to_utf8(self):
        bid = self.book_id('测试之书')
        status, body, headers = self.req('/dl/%d' % bid)
        self.assertEqual(status, 200)
        self.assertIn('第6章 标题6', body)
        self.assertIn("filename*=UTF-8''", headers['Content-Disposition'])

    def test_find_in_book(self):
        bid = self.book_id('测试之书')
        _, body, _ = self.req('/book/%d/find?q=%s' % (bid, urllib.request.quote('豪情')))
        self.assertIn('共 18 处', body)


class TestWebPassword(TestWeb):
    password = 'pa55'

    def test_login_flow(self):
        status, _, headers = self.req('/manage')
        self.assertEqual(status, 303)
        self.assertIn('/login?next=', headers['Location'])
        self.assertEqual(self.req('/api/progress', b'{}')[0], 401)
        self.assertEqual(self.req('/static/app.js')[0], 200)
        status, _, headers = self.req('/login', b'password=pa55&next=/manage')
        self.assertEqual(status, 303)
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertEqual(self.req('/manage', headers={'Cookie': cookie})[0], 200)
        self.assertEqual(self.req('/manage', headers={'Cookie': 'ns_auth=99999999999.00'})[0], 303)

    def test_wrong_password(self):
        self.assertEqual(self.req('/login', b'password=nope')[0], 401)


if __name__ == '__main__':
    unittest.main()
