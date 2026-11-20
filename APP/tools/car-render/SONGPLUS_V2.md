# 宋 PLUS 修复版 V2 导入

当前用户指定来源为 `3D 示例23宋plus-修复版V2.zip`，实际 GLB 名称为
`2024 比亚迪宋PLUS DM-i（修复版）`。导入脚本固定核验本次审计的原始 GLB：

```text
SHA-256  4b2ffab4cc240ad6b8d0d958da9038db5a75530480cd6c12c063d42b6cec0b87
大小     22497496 字节
几何     51 meshes / 118 primitives / 538436 顶点 / 179890 三角形
```

## 重复导入

在仓库目录执行（脚本也支持从其他工作目录调用）：

```powershell
python tools/car-render/import_songplus_v2.py 'D:/微信聊天记录/xwechat_files/wxid_2lm1478piinl22_e108/msg/file/2026-10/3D 示例23宋plus-修复版V2.zip'
python tools/car-render/test_import_songplus_v2.py
node --test tools/car-render/export_car_node.test.cjs tools/car-render/car_format_v6.test.cjs tools/car-render/songplus_asset.test.cjs
```

仅提取、校验并准备 GLB，可在第一条命令后加 `--extract-only`，这不会调用
Node 导出器，也不会修改 APP 的 `car.bin`、来源清单或许可证。`--output <路径>`
可指定其他 CARM 文件；成功后来源清单与许可证写入该输出文件的同一目录。

脚本只按唯一文件名读取 ZIP 内 `car-model.js` 和 `LICENSE-car.txt`，静态提取
base64 字面量，不执行包内 JavaScript，不将 ZIP 成员路径解压到文件系统。
原始 GLB 保存在 `build/songplus-v2-input.glb`。修正后的导出副本为
`build/songplus-v2-export.glb`，二进制几何和图片块保持原样，仅修改两处配置引用：

- `mats.turnL`：不存在的 `turn_LFL` → 实际左后视镜灯材质 `turn_L`。
- `mats.turnR`：不存在的 `turn_LFR` → 实际右后视镜灯材质 `turn_R`。

源网格证据：左后视镜灯在 primitive Mesh #10，X 为 `+0.86118..+1.01187m`；
右后视镜灯在 #21，X 为 `-1.01187..-0.86118m`。两者分别随对应前门运动；
后转向灯 `turn_tail_L/R` 原引用正确。编号按 Three.js Mesh 遍历顺序，
不能误当成 glTF node 序号。

导出命令为 `node tools/car-render/export_car_node.js <导出副本> <输出> --v6`。
导出先写临时文件，确认 CARM v6 且存在网格后再替换目标。成功后生成
`car-model-source.json`，记录 ZIP、源 GLB、导出副本及 CARM 的 SHA-256，
以及别名改动、车型、作者声明和导入行为；失败不把旧资产标记为新模型。

## 显示适配边界

CARM v6 携带 UV、base-color 图片、显式颜色/透明材质标记、原始平滑法线及门盖配置；
大型网格拆成每组最多 65535 顶点，以适配原生 GLES 的 16 位索引。
玻璃按 `glas`、`roof_glass`、`glass_Alph` 的配置识别；车灯中的 `glass` 字样不
等同于车窗。车漆保留 APP 当前白色，网页的默认红色仅为网页展示偏好。

原包设置 `lens=true`，因此 `mats.tail` 中的**全部**材质都按透明灯罩导出，
alpha 为 `0.7`；范围同时包括 `red_glass` 和源 GLB 中标记为不透明的
`Tail_Light_Emis`，不能仅凭源 `alphaMode` 跳过后者。否则该层会遮住位于其后方的
独立转向灯网格。前灯罩仍按原包约定使用 `0.22`，车窗使用 `0.78`。

转向灯适配约定：`turnL` / `turnR` 保持独立灯角色，由现有左右转向状态与闪烁相位
驱动其发光；这一发光应独立于反射底色及纹理的黑色区域，避免已亮灯被底色采样
乘成黑色。它不等同于整条尾灯或尾灯贴图闪橙色，也不新增控制车辆的命令。
此处说明适配要求，实际实现与亮灭画面的验证结果以本轮验证报告为准。

材质在现有 Blinn-Phong 渲染器内近似表现，未完整实现 glTF PBR 的金属粗糙度、
法线和遮蔽贴图。转向、制动灯显示沿用现有遥测；用户已选择前照灯默认点亮展示，
`head_lens` 使用 v6 角色 8，以独立白色发光层和车前两侧渐变照明呈现。
这是模型展示效果，不代表实车大灯开关状态，不新增或伪造遥测；后雾灯仍不启用。
充电口保留静止几何，引擎盖没有遥测时保持关闭。

原包没有独立制动灯或倒车灯材质角色。APP 在没有独立制动灯组时，复用已标注
尾灯组显示制动增强（正常尾灯低亮、`brakeLights=true` 高亮），属于 APP 的状态
可视化适配，不宣称原包包含单独的制动灯网格；倒车状态继续切换相机视角。

## CARM v6 布局

全局 `CARM` 魔数、版本、组数及 UTF-8 名称表沿用旧格式。每组头 72 字节，
前 36 字节与 v5 一致：材质序号、顶点数、索引数、索引宽度标记、rig/axis/angle/
lampRole、pivot XYZ、positionScale。新增字段依次为 RGBA float4（offset 36）、
spec/gloss float（52/56）、纹理序号 int（60，-1 为无纹理）、flags（64）、
alphaCutoff float（68）。flags 的 bit0 为车窗、bit1 为 BLEND、bit2 为 MASK。
lampRole 的 0–7 保持原定义，v6 增加 8=前照灯；v4/v5 仍只接受 0–7。

组头后依次存位置 int16×3、法线 int8×3、UV float32×2、索引 uint16。
所有组之后是共享图片表：图片数 uint32，每张长度 uint32 后接原始 PNG/JPEG。
UV 保留超出 `[0,1]` 的值；本模型全部实际图片为 2 的幂次尺寸，按 REPEAT 和
mipmap 采样。v3–v5 读取兼容保留；旧模型专用去重不作用于 v6 的贴花和层叠材质。

## 来源声明

GLB 声明作者为 **Ddiaz Design**，许可为 **CC-BY-NC-SA-4.0（不可商用）**。
原包 `LICENSE-car.txt` 的名称与来源链接却指向 **2016 Exotic Rides W70**，
与实际 GLB 车型不一致。导入成功后的 `car-model-LICENSE.txt` 在说明这一差异后
原样保留许可证文本，不将该模板标题或链接冒充为宋 PLUS 的已核实来源。

本说明只描述导入路径与渲染适配；构建和设备画面验证结果应以本轮验证报告为准。
