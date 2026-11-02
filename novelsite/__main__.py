"""命令行入口：python3 -m novelsite {serve,scan} --library 目录"""

import argparse
import ipaddress
import os
import sys
import threading

from . import __version__
from .library import Library
from .web import App, make_server


def _is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _password(args):
    if args.password_file:
        with open(args.password_file, encoding='utf-8') as f:
            return f.read().strip()
    return os.environ.get('NOVEL_PASSWORD', '')


def _log_scan(st):
    if st.get('error'):
        print('[scan] ' + st['error'], file=sys.stderr)
    print('[scan] 发现 %d 本，新增 %d，更新 %d，移动 %d，移除 %d，用时 %.1f 秒'
          % (st['found'], st['added'], st['updated'], st['moved'], st['removed'],
             (st['finished'] or 0) - st['started']), file=sys.stderr)


def cmd_serve(args):
    pw = _password(args)
    loop = _is_loopback(args.host)
    if not pw and not loop and not args.insecure_no_password:
        sys.exit('拒绝启动：监听 %s 但未设置密码。\n'
                 '请设置环境变量 NOVEL_PASSWORD 或使用 --password-file；'
                 '若确定只在可信内网使用，可加 --insecure-no-password。' % args.host)
    lib = Library(args.db, args.library)
    app = App(lib, password=pw, name=args.name, host_check=(not pw and loop), quiet=args.quiet)
    srv = make_server(app, args.host, args.port)
    if not args.no_scan:
        threading.Thread(target=lib.scan, kwargs={'log': _log_scan}, daemon=True, name='scan').start()
    shown = '[%s]' % args.host if ':' in args.host else args.host
    print('novelsite %s  http://%s:%d/\n  书库：%s\n  数据库：%s\n  密码：%s'
          % (__version__, shown, args.port, lib.root, os.path.abspath(args.db), '已启用' if pw else '未启用'),
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def cmd_scan(args):
    lib = Library(args.db, args.library)
    t = threading.Thread(target=lib.scan, kwargs={'prune': args.prune}, daemon=True)
    t.start()
    tty = sys.stderr.isatty()
    while t.is_alive():
        t.join(1)
        st = lib.scan_state
        if tty and st.get('running'):
            sys.stderr.write('\r[scan] %s：已发现 %d，已处理 %d / %d   '
                             % (st['phase'], st['found'], st['done'], st['total']))
    if tty:
        sys.stderr.write('\n')
    _log_scan(lib.scan_state)
    return 1 if lib.scan_state.get('error') else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='novelsite', description='极简个人 TXT 小说站（零第三方依赖）')
    sub = p.add_subparsers(dest='cmd')
    sub.required = True

    def common(sp):
        sp.add_argument('--library', '-l', required=True, help='TXT 小说所在目录')
        sp.add_argument('--db', default='novelsite.db', help='数据库文件（默认 ./novelsite.db）')

    s = sub.add_parser('serve', help='启动网站（启动时在后台做一次增量扫描）')
    common(s)
    s.add_argument('--host', default='127.0.0.1', help='监听地址（默认 127.0.0.1）')
    s.add_argument('--port', '-p', type=int, default=8080)
    s.add_argument('--name', default='书库', help='站点名称')
    s.add_argument('--password-file', help='从文件读取登录密码（也可用环境变量 NOVEL_PASSWORD）')
    s.add_argument('--no-scan', action='store_true', help='启动时不扫描书库')
    s.add_argument('--quiet', '-q', action='store_true', help='不输出访问日志')
    s.add_argument('--insecure-no-password', action='store_true', help='允许在非本机地址上无密码运行')
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser('scan', help='只扫描书库然后退出')
    common(s)
    s.add_argument('--prune', action='store_true', help='即使缺失文件超过一半也照常从库中移除')
    s.set_defaults(func=cmd_scan)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(main())
