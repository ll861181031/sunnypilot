# 本地内核补丁：uvcvideo USB2 等时带宽

## 为什么

两块 USB2 相机（Realtek `0bda:5161`、DECXIN `1bcf:2cd1`）在 MJPEG
1920x1080 下把 `dwMaxPayloadTransferSize` 声明成 **3072 / 3060 B/interval**，
占满 USB2 高速的等时预算（3×1024×8000 = **24.576 MB/s**）。
补丁按 VID:PID 限流，与镜头角色无关；2026-11-04 镜头调换后 **DECXIN 是主摄、
Realtek 是广角**（之前相反），两路的采集帧率仍是主摄 30、广角 30。
两路同时开时第二路 `VIDIOC_REQBUFS` 返回 `ENOSPC`，广角 0 fps。

实测实际帧是**固定大小**的：长焦 ~243 KiB/帧（7.1 MB/s @30fps）、
广角 ~255 KiB/帧（4.9 MB/s @20fps），声明值严重超配。

补丁在 `uvc_fixup_video_ctrl()` 里对这两块相机的**压缩格式**把 payload 限到：

- 长焦 `0bda:5161` → **1536** B/interval（12.3 MB/s）
- 广角 `1bcf:2cd1` → **1024** B/interval（8.2 MB/s）

合计 20.5 MB/s < 24.576，驱动会选较小的 alternate setting。

## 文件

- `uvcvideo-usb2-bandwidth.patch`：相对 Linux 7.0 `drivers/media/usb/uvc/uvc_video.c`
  的补丁（本机内核 `7.0.0-38-generic` 的 Ubuntu 源码）。

## 已安装状态（2026-11-04）

- 源码：`/usr/src/uvcvideo-1.0/`（已打补丁，含 `dkms.conf`）
- DKMS：`uvcvideo/1.0`，`dkms status` 显示 installed
- 签名：`/var/lib/shim-signed/mok/MOK.{priv,der}`（已 enrolled，Secure Boot 可加载）
- 已加载：`modinfo -n uvcvideo` → `/lib/modules/7.0.0-38-generic/updates/dkms/uvcvideo.ko.zst`

验证（会打印 capping 日志）：

```bash
sudo dmesg | grep 'Capping compressed payload'
#  uvcvideo 1-2:1.1: Capping compressed payload 3072 -> 1536 ...
#  uvcvideo 1-1.1:1.1: Capping compressed payload 3060 -> 1024 ...
```

## 重新安装 / 换内核

DKMS 会在内核升级时自动重编（`AUTOINSTALL=yes`）。如果从源码重来：

```bash
# 1) 取与运行内核匹配的源码（7.0.0-38 的 Ubuntu 源码；apt 的 diff.gz 可能 404，
#    但 orig.tar.gz 足够，uvc 驱动在 orig 里）
mkdir -p /tmp/kernel-src && cd /tmp/kernel-src
apt-get source linux-hwe-7.0 || true
tar -xzf linux-hwe-7.0_7.0.0.orig.tar.gz linux-7.0/drivers/media/usb/uvc

# 2) 放到 DKMS 源码目录并打补丁（补丁路径是 a/uvc_video.c，所以用 -p1 在 uvc 目录里打）
sudo rm -rf /usr/src/uvcvideo-1.0
sudo mkdir -p /usr/src/uvcvideo-1.0
sudo cp -a linux-7.0/drivers/media/usb/uvc/. /usr/src/uvcvideo-1.0/
cd /usr/src/uvcvideo-1.0
sudo patch -p1 < /data/sunnypilot/tools/kernel/uvcvideo-usb2-bandwidth.patch

# 3) dkms.conf
sudo tee /usr/src/uvcvideo-1.0/dkms.conf >/dev/null <<'EOF'
PACKAGE_NAME="uvcvideo"
PACKAGE_VERSION="1.0"
BUILT_MODULE_NAME[0]="uvcvideo"
DEST_MODULE_LOCATION[0]="/updates/dkms"
AUTOINSTALL="yes"
MAKE[0]="make -C ${kernel_source_dir} M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build modules"
CLEAN="make -C ${kernel_source_dir} M=${dkms_tree}/${PACKAGE_NAME}/${PACKAGE_VERSION}/build clean"
EOF

sudo dkms add /usr/src/uvcvideo-1.0
sudo dkms build -k $(uname -r) uvcvideo/1.0
sudo dkms install -k $(uname -r) uvcvideo/1.0 --force
sudo depmod -a
sudo modprobe -r uvcvideo && sudo modprobe uvcvideo
```

DKMS 默认用 `/var/lib/shim-signed/mok/MOK.{priv,der}` 签名（见
`/etc/dkms/framework.conf`，备份 `.bak-20261104`）。

## 回滚

```bash
sudo dkms remove uvcvideo/1.0 --all
sudo depmod -a
sudo modprobe -r uvcvideo && sudo modprobe uvcvideo
```

回滚后广角 1080p20 会重新变成 0 fps；把 `wide-camera.conf` 的
`WIDE_WEBCAM_WIDTH/HEIGHT` 改回 `1280x720`，或整体退回单摄。

## 风险

- 补丁只对 **MJPEG 1920x1080 帧**生效（`frame->wWidth/Height` 判断）；720p/5MP 等其它
  分辨率不受影响，仍然使用相机自己的声明值。
- 这是**本机补丁**，不是上游代码；内核升级后需要 DKMS 重编（自动），失败时回滚。
- 如果相机在某些场景输出超过新上限（实测 240 帧/路最大 256 KiB，未超），
  会丢帧/截断；dmesg 出现 `USB isochronous frame lost` 时先回滚。
- 补丁按 VID:PID 硬编码，只对这两块相机生效。
