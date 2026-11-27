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
import urllib.parse
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

    def test_serial_number_prefix(self):
        # 「编号_书名」：编号不是书名，下划线后面也不是作者
        cases = {
            '1006_小琳': ('小琳', '', False),
            '1006 小琳': ('小琳', '', False),
            '1006-小琳（完结）': ('小琳', '', False),
            '1006.小琳': ('小琳', '', False),
            '1006、小琳': ('小琳', '', False),
            '１００６＿小琳': ('小琳', '', False),
            '0012_小琳_续集': ('小琳_续集', '', False),
            '1006_《小琳》作者：张三': ('小琳', '张三', True),
            '3.5亿的人生': ('3.5亿的人生', '', False),
            '1984': ('1984', '', False),
        }
        for name, (title, author, sure) in cases.items():
            t, a, _, s = tp.parse_filename(name)
            self.assertEqual((t, a, s), (title, author, sure), name)

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


class TestReplacer(TempDirCase):
    def test_semantics(self):
        r = tp.Replacer({'唐三': '张三', '唐三十六': '唐三十六'})  # 恒等规则保护更长的词
        self.assertEqual(r('唐三看了唐三十六一眼'), '张三看了唐三十六一眼')
        self.assertEqual(tp.Replacer({'甲': '乙', '乙': '甲'})('甲打了乙'), '乙打了甲')  # 互换
        self.assertEqual(tp.Replacer({'A': 'B', 'B': 'C'})('AB'), 'BC')  # 不连锁
        self.assertEqual(tp.Replacer({'a.b': 'x'})('a.b acb'), 'x acb')  # 字面量，不是正则
        self.assertFalse(tp.Replacer({}))
        self.assertEqual(tp.Replacer({})('原样'), '原样')

    def test_clean_rule(self):
        self.assertEqual(tp.clean_rule(' 萧炎 ', ' 李明 '), ('萧炎', '李明'))
        self.assertEqual(tp.clean_rule('广告', ''), ('广告', ''))
        for src, dst in [('', 'x'), ('  ', 'x'), ('a\nb', 'x'), ('a', 'b\rc'), ('长' * 51, 'x'), ('a', '长' * 51)]:
            with self.assertRaises(ValueError):
                tp.clean_rule(src, dst)

    def test_download_stream_does_not_miss_names_across_chunks(self):
        head = '第1章 开始\n'.encode('utf-8')
        pad = b'a' * ((1 << 18) - 3 - len(head))  # 让「萧炎」正好跨过第一个 256KB 读块的边界
        self.write('lib/x.txt', head + pad + '萧炎\n萧炎走了。\n'.encode('utf-8') * 3)
        lib = Library(os.path.join(self.dir, 'db.sqlite'), os.path.join(self.dir, 'lib'))
        lib.scan()
        book = lib.book(1)
        out = b''.join(lib.iter_utf8(book, lib.toc(book), tp.Replacer({'萧炎': '李明'}))).decode('utf-8')
        self.assertEqual((out.count('李明'), out.count('萧炎')), (6, 0))
        self.assertTrue(out.startswith('第1章 开始\naaa'))
        lib.conn.close()


class TestScanMeta(TempDirCase):
    def test_numbered_file_and_refresh_of_old_metadata(self):
        self.write('lib/言情/1006_小琳.txt', make_novel(2).encode('utf-8'))  # 文件头有「作者：张三」
        self.write('lib/言情/1007_小雨.txt', '第1章 开始\n正文。\n'.encode('utf-8'))  # 文件头没有作者
        db, root = os.path.join(self.dir, 'db.sqlite'), os.path.join(self.dir, 'lib')
        lib = Library(db, root)
        lib.scan()
        rows = {r['title']: (r['author'], r['category']) for r in lib.query('SELECT * FROM books')}
        self.assertEqual(rows, {'小琳': ('张三', '言情'), '小雨': ('', '言情')})
        # 模拟旧版本留下的错误元数据：书名是编号、作者是书名，且没有记录元数据版本
        bid = lib.one("SELECT id FROM books WHERE title='小琳'")[0]
        lib.execute("UPDATE books SET title='1006', author='小琳' WHERE id=?", (bid,))
        lib.execute("DELETE FROM meta WHERE k='meta_version'")
        lib.set_shelf(bid, True)
        lib.scan()
        self.assertEqual(lib.scan_state['refreshed'], 2)
        row = lib.book(bid)  # 同一个 id：书架、进度、替换规则都保留
        self.assertEqual((row['title'], row['author']), ('小琳', '张三'))
        self.assertTrue(lib.on_shelf(bid))
        lib.scan()  # 版本已记录，之后的扫描不再重复刷新
        self.assertEqual(lib.scan_state['refreshed'], 0)
        lib.conn.close()


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

    # ---- 内容替换
    def add_rule(self, bid, src, dst, scope='book', origin=True):
        form = urllib.parse.urlencode({'book': bid, 'scope': scope, 'src': src, 'dst': dst}).encode()
        return self.req('/rules', form, {'Origin': self.base} if origin else {})

    def get(self, path):
        return self.req(urllib.request.quote(path, safe='/?=&#'))

    def test_rules_apply_everywhere_the_reader_looks(self):
        bid = self.book_id('测试之书')
        other = self.lib.one('SELECT id FROM books WHERE id != ?', (bid,))[0]
        _, body, _ = self.get('/book/%d/rule?src=豪情&dst=壮志' % bid)
        self.assertIn('共 18 处', body)
        self.assertEqual(self.add_rule(bid, '豪情', '壮志', origin=False)[0], 303)
        self.assertEqual(self.add_rule(bid, '远处', '<b>远方</b>', scope='all')[0], 303)
        _, body, _ = self.get('/read/%d/2' % bid)
        self.assertIn('壮志', body)
        self.assertNotIn('豪情', body)
        self.assertIn('&lt;b&gt;远方&lt;/b&gt;', body)  # 先替换、后转义
        self.assertNotIn('<b>远方', body)
        self.assertIn('/read/%d/3?r=2' % bid, body)  # 链接带规则版本号，绕开章节页缓存
        _, body, _ = self.get('/read/%d/2' % other)  # 全站规则作用于其他书，本书专属规则不会
        self.assertIn('&lt;b&gt;远方', body)
        self.assertIn('豪情', body)
        self.assertIn('共 18 处', self.get('/book/%d/find?q=壮志' % bid)[1])  # 看到什么就搜什么
        self.assertIn('没有找到', self.get('/book/%d/find?q=豪情' % bid)[1])
        self.assertIn('壮志', self.get('/dl/%d' % bid)[1])
        self.assertIn('豪情', self.get('/dl/%d?raw=1' % bid)[1])
        self.assertIn('本书 1 条 · 全站 1 条', self.get('/book/%d' % bid)[1])
        # 全站同名规则：本书仍以专属规则为准
        self.assertIn('以专属规则为准', self.get('/book/%d/rule?src=豪情&dst=X&scope=all' % bid)[1])
        # 删除后恢复原文
        for r in self.lib.query('SELECT id FROM rules'):
            self.assertEqual(self.req('/rules/delete', ('id=%d&back=/manage' % r['id']).encode())[0], 303)
        _, body, _ = self.get('/read/%d/2' % bid)
        self.assertIn('豪情', body)
        self.assertIn('/read/%d/3?r=4' % bid, body)

    def test_protect_rule_delete_rule_and_titles(self):
        text = ('第1章 萧炎出山\n萧炎看着树林动了起来。\n本章完，请访问www.example.com\n林动拔剑。\n'
                '第2章 再会\n林动与萧炎再会。\n')
        self.write('lib/x/林动传.txt', text.encode('utf-8'))
        self.lib.scan()
        bid = self.book_id('林动传')
        _, body, _ = self.get('/book/%d/rule?src=林动&dst=李明' % bid)
        self.assertIn('共 3 处', body)  # 含「树林动」的误伤
        self.add_rule(bid, '树林动', '树林动')  # 保护规则
        self.assertIn('共 2 处', self.get('/book/%d/rule?src=林动&dst=李明' % bid)[1])
        self.add_rule(bid, '林动', '李明')
        self.add_rule(bid, '萧炎', '王五')
        self.add_rule(bid, '本章完，请访问www.example.com', '')  # 留空即删除，整段消失
        _, body, _ = self.get('/read/%d/1' % bid)
        self.assertIn('<h1>第1章 王五出山</h1>', body)
        self.assertIn('树林动了起来', body)
        self.assertIn('<p>李明拔剑。</p>', body)
        self.assertNotIn('本章完', body)
        self.assertNotIn('<p></p>', body)
        self.assertIn('第1章 王五出山', self.get('/book/%d' % bid)[1])
        self.req('/api/progress', json.dumps({'b': bid, 'c': 1, 'p': 0}).encode(), {'Content-Type': 'application/json'})
        self.assertIn('读到 第1章 王五出山', self.get('/history')[1])

    def test_rule_validation_and_csrf(self):
        bid = self.book_id('测试之书')
        self.assertEqual(self.add_rule(bid, '', 'x')[0], 400)
        self.assertEqual(self.add_rule(bid, 'a\nb', 'x')[0], 400)
        self.assertEqual(self.add_rule(99999, 'a', 'x')[0], 404)
        form = urllib.parse.urlencode({'book': bid, 'src': 'a', 'dst': 'b'}).encode()
        self.assertEqual(self.req('/rules', form, {'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.lib.one('SELECT COUNT(*) FROM rules')[0], 0)
        self.assertEqual(self.req('/rules/delete', b'id=1&back=//evil.example')[0], 303)


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
