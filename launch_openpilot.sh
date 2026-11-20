#!/usr/bin/env bash

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd -P)"
cd "$DIR" || exit 1
# Bash tracks the directory internally; do not export shell-maintained PWD to
# children that may change cwd themselves.
unset PWD

# Desktop sessions share Panda, cameras and messaging sockets. Keep a lock
# across the entire launch so a second terminal cannot replace publishers.
if [ ! -f /AGNOS ] && [ "$(uname -s)" = Linux ]; then
  # Older bench services did not take the shared lock. Refuse to replace their
  # camera publishers or contend for their WebRTC port during the transition.
  if systemctl --user is-active --quiet cplink-bench-video.service 2>/dev/null; then
    printf 'sunnypilot: 手机视频台架仍在运行，请先停止 cplink-bench-video.service 再启动正式行车栈。\n' >&2
    exit 1
  fi
  lock_file="${XDG_RUNTIME_DIR:-/tmp}/sunnypilot-${UID}.lock"
  # A failed redirect leaves fd 9 closed, so flock would fail with EBADF and be
  # misreported as another instance holding the lock.
  if ! exec 9>"$lock_file"; then
    printf 'sunnypilot: 无法打开锁文件 %s，请检查 XDG_RUNTIME_DIR 或目录权限。\n' "$lock_file" >&2
    exit 1
  fi
  if ! flock -n 9; then
    printf 'sunnypilot 或视频台架已在运行，请先关闭现有实例再重启。\n' >&2
    exit 1
  fi
fi

exec "$DIR/launch_chffrplus.sh"
