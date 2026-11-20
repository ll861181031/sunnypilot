# 自车车模工具

当前原生 3D 场景使用用户提供的**宋 PLUS 修复版 V2**，由
`import_songplus_v2.py` 转为 `app/src/main/assets/car.bin`（CARM v6）。
导入、来源和材质适配说明见 [SONGPLUS_V2.md](SONGPLUS_V2.md)。

下文为历史静态 PNG 出图流程，不是当前原生 3D 场景的加载路径。

## 历史：自车 3D 车模离线渲染

驾驶卡（`ui/components/DriveViewCard.kt`）里的自车自 8.3.0 起不再用 Canvas 手绘，
改用这里的离线渲染图：

| 产物 | 用在 | 说明 |
| --- | --- | --- |
| `app/src/main/res/drawable-nodpi/car_byd_rear.png` | D / N 挡（后视相机） | 车尾 3/4 视角，透明底，带地面投影 |
| `app/src/main/res/drawable-nodpi/car_byd_side.png` | P 挡（左侧视相机） | 正侧视 |
| `app/src/main/res/drawable-nodpi/car_byd_top.png` | R 挡（俯视相机） | 正俯视 |

车是**比亚迪宋 PLUS DM-i 冠军版 110KM 旗舰 PLUS**，漆色用卡片设置里的「午夜黑」
（`#0f1013`，metalness 0.32、roughness 0.42、clearcoat 1.0）。原始 GLB 是微信里
那份「时光灰/日蚀蓝」车模包，本仓库不存二进制车模，只存渲染结果和这套生成脚本。

## 怎么重新出图

1. 把 GLB 放到本目录，命名 `model.glb`（`three.min.js`、`three-addons.js`、
   `meshopt-decoder.js` 已在仓库里，来自 3D 卡片包，MIT）。
2. 用无头 Chrome 逐视角截图（Windows 路径自行替换）：

```powershell
$chrome='C:\Program Files\Google\Chrome\Application\chrome.exe'
$dir='D:\Desktop\openpilot\_navipilot_full\tools\car-render'
$ud=Join-Path $env:TEMP ("carshot_"+[guid]::NewGuid().ToString("N").Substring(0,8))
& $chrome --headless=new --hide-scrollbars --allow-file-access-from-files `
  --enable-unsafe-swiftshader --use-angle=swiftshader --no-first-run `
  "--user-data-dir=$ud" --default-background-color=00000000 `
  --window-size=1800,1200 --virtual-time-budget=25000 `
  "--screenshot=$dir\out_rear.png" `
  "file:///D:/Desktop/openpilot/_navipilot_full/tools/car-render/index.html?view=rear"
```

`view` 取 `rear` / `side` / `top`；`window-size` 分别用 `1800,1200`、`2000,1100`、`1500,1500`。
3. 用 Pillow 按 alpha 裁边、最长边缩到 1200，存进 `res/drawable-nodpi/`：

```python
from PIL import Image
im = Image.open("out_rear.png").convert("RGBA")
im = im.crop(im.split()[3].getbbox())
im.thumbnail((1200, 1200), Image.LANCZOS)
im.save("app/src/main/res/drawable-nodpi/car_byd_rear.png", optimize=True)
```

## 几个坑（都踩过了）

- **车模带 `EXT_meshopt_compression`**，必须给 `GLTFLoader` 挂 `MeshoptDecoder`，
  否则加载直接失败。量化（`KHR_mesh_quantization`）three 原生支持。
- **three r149 不做颜色管理**：`new THREE.Color('#0f1013')` 会被当成线性值，
  黑漆照出来是灰的。必须先 `convertSRGBToLinear()`（`index.html` 里的 `sRGB()`）。
- **`Box3.setFromObject` 不看可见性**：车底的电池仓/机舱零件会把包围盒撑到地面以下，
  取景和贴地要以轮子（`M_Ext_Wheel`）的最低点为准，尺寸用真车参数（长 4.774m、高 1.68m）。
- 俯视相机的画面纵向是**车长**不是车高，`spanH` 要用 `hl`，否则镜头会怼到车顶上。

## 换车型 / 换颜色

改 URL 参数即可：`paint=1a44c4`（电光蓝）、`ff0000`（红）、`e6e4df`（珍珠白）……
色值与 3D 卡片 `settings.js` 里的车漆预设一致。渲染完记得同步 CLAUDE.md 的版本说明。
