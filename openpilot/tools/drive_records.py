#!/usr/bin/env python3
"""Local-only driving data index, completeness report, pinning and export."""

import argparse
import json
from pathlib import Path

from openpilot.common.hardware.hw import Paths
from openpilot.system.loggerd.drive_store import export_route, list_routes, manifest_for, scan, set_pin, storage_status, store_root


def main(argv=None):
  parser = argparse.ArgumentParser(description='行车数据留存与完整性检查（本地，不连接硬件）')
  parser.add_argument('--root', type=Path, default=Path(Paths.log_root()))
  sub = parser.add_subparsers(dest='command', required=True)
  index = sub.add_parser('scan', help='增量检查全量 rlog，只扫描新增或变化的分段')
  index.add_argument('--route')
  index.add_argument('--limit', type=int)
  sub.add_parser('list', help='快速读取已有索引；不解压日志')
  sub.add_parser('status', help='磁盘余量、保留路线和自动索引状态')
  show = sub.add_parser('show', help='行程完整性、时间点、消息覆盖和文件位置')
  show.add_argument('route')
  show.add_argument('--events', action='store_true')
  pin = sub.add_parser('pin', help='保留整趟行程，自动清理会跳过')
  pin.add_argument('route')
  pin.add_argument('--note', default='')
  unpin = sub.add_parser('unpin', help='取消保留，不立即删除任何数据')
  unpin.add_argument('route')
  export = sub.add_parser('export', help='导出日志和身份快照，逐文件核对 SHA-256')
  export.add_argument('route')
  export.add_argument('--output', type=Path, required=True)
  export.add_argument('--include-video', action='store_true')
  args = parser.parse_args(argv)
  args.root = args.root.resolve()
  if args.command == 'scan':
    if args.limit is not None and args.limit < 0:
      parser.error('--limit 必须大于等于 0')
    result = scan(args.root, args.route, args.limit)
  elif args.command == 'list':
    result = list_routes(args.root)
  elif args.command == 'status':
    result = storage_status(args.root)
  elif args.command == 'show':
    result = manifest_for(args.root, args.route)
    if args.events:
      result = {'route': args.route, 'events': result['events']}
  elif args.command in ('pin', 'unpin'):
    result = set_pin(args.root, args.route, args.command == 'pin', getattr(args, 'note', ''))
  else:
    result = export_route(args.root, args.route, args.output, args.include_video)
  print(json.dumps({'indexRoot': str(store_root(args.root)), 'result': result}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
  main()
